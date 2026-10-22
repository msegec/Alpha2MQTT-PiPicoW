"""Replay recorded net demand through a regulator against the inverter model, and score it.

    python3 -I tools/bench/replay.py LOGS... [--from A --to B --exclude A..B]
        [--regulator PATH | --ref GITREF] [--set key=value ...] [--settings-from-log]
        [--plant plant.json] [--max-charge-w 4000] [--max-discharge-w 4000] [--json]

Each regulating sample of the log gives the house demand (grid_w + battery less the battery loss, logs.Loss), the
target (grid_w - error_w) and the SOC. The simulated grid is demand + loss(simulated battery) - simulated
battery. The policy is REGULATE with the CLI limits and the logged target offset. Each command is acked 0.1 s
later as written the way it asked (power only or block), unless --fail-every or --timeout-every say otherwise. A regulator whose update_grid takes solar_w (or soc) gets the
sample's solar (or SOC); one with update_load gets the logged load_w when there is one. See facts/bench.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import inspect
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logs  # noqa: E402
import plant as plantmod  # noqa: E402
import score  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
REGULATOR = "Home Assistant/custom_components/alpha2mqtt_controller/regulator.py"
ACK_S = 0.1  # a response comes this long after its command
OK, FAIL, TIMEOUT = "ok", "fail", "timeout"  # what happens to a published command: written, refused, no response
_names = itertools.count()


def load_regulator(path: str | None = None, ref: str | None = None, repo: Path = REPO):
    """The regulator module at a path, or at a git ref of the repo (extracted with git show to a temp dir)."""
    if ref is not None:
        src = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{REGULATOR}"], check=True,
                             capture_output=True).stdout
        d = Path(tempfile.mkdtemp(prefix="bench-reg-"))
        path = str(d / "regulator.py")
        Path(path).write_bytes(src)
    path = path or str(repo / REGULATOR)
    name = f"bench_regulator_{next(_names)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def coerce(mod, values: dict) -> tuple[dict, list[str]]:
    """Settings values this module knows, cast to the type of each default; and the names it does not know."""
    fields = {f.name: f for f in dataclasses.fields(mod.Settings)}
    defaults = mod.Settings()
    out, unknown = {}, []
    for k, v in values.items():
        if k not in fields:
            unknown.append(k)
            continue
        kind = type(getattr(defaults, k))
        if kind is bool:
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "on", "yes")
        else:
            out[k] = kind(round(float(v))) if kind is int else kind(float(v)) if kind is float else v
    return out, unknown


def parse_sets(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"--set wants key=value, got {it!r}")
        out[k.strip()] = v.strip()
    return out


def every(fail_every: int = 0, timeout_every: int = 0):
    """An outcome for the n-th command published (from 0): every fail_every-th is refused, every timeout_every-th
    gets no response; 0 is never. Refused wins when both fall on one command."""
    def outcome(n: int) -> str:
        if fail_every > 0 and (n + 1) % fail_every == 0:
            return FAIL
        if timeout_every > 0 and (n + 1) % timeout_every == 0:
            return TIMEOUT
        return OK
    return outcome


def replay(mod, rows: list[dict], plant: plantmod.Plant, overrides: dict | None = None,
           cfgs: logs.CfgTimeline | None = None, max_charge_w: float = 4000.0, max_discharge_w: float = 4000.0,
           discharge_soc: int = 10, charge_soc: int = 100, loss: logs.Loss = logs.Loss(), ack_s: float = ACK_S,
           outcome=None) -> tuple[list[dict], list[str]]:
    """Simulated rows in the shape score.score reads, and warnings. overrides apply over the cfg in force
    (cfgs, when given) or the module's defaults. loss must be the one the rows' demand was taken with.
    outcome(n) says what happens to the n-th command published (OK by default), as the controller sees it: OK is
    written and acked ack_s later; FAIL is not written and its response calls fail(); TIMEOUT is not written, has
    no response, and calls fail() once ack_timeout_s has passed. Only the newest command's response counts."""
    outcome = outcome or (lambda n: OK)
    overrides = overrides or {}
    warnings: list[str] = []
    base = {}
    if cfgs is not None and rows:
        base = cfgs.at(rows[0]["t"]) or {}
    vals, unknown = coerce(mod, {**base, **overrides})
    warnings += [f"setting {k} unknown to this regulator, ignored" for k in unknown if k in overrides]
    reg = mod.Regulator(mod.Settings(**vals))
    s = reg.settings
    params = inspect.signature(reg.update_grid).parameters
    takes_solar = "solar_w" in params
    takes_soc = "soc" in params
    has_ack = hasattr(reg, "ack")
    ack_power = has_ack and "power_write" in inspect.signature(reg.ack).parameters
    has_fail = hasattr(reg, "fail")
    ack_timeout_s = getattr(s, "ack_timeout_s", 10.0)
    has_load = hasattr(reg, "update_load")
    pol_fields = {f.name for f in dataclasses.fields(mod.Policy)}
    cur_cfg = base
    sim = None
    responses: list[tuple[float, object, bool]] = []  # (due, command, written), in time order
    last: list | None = None  # [command, time sent, answered] of the newest command published
    published = 0

    def newest(cmd) -> bool:
        return last is not None and cmd is last[0] and getattr(reg, "sent", cmd) is cmd

    def respond(now: float) -> None:
        while responses and responses[0][0] <= now:
            due, cmd, ok = responses.pop(0)
            if not newest(cmd):
                continue
            last[2] = True
            if not ok:
                if has_fail:
                    reg.fail()
            elif has_ack:
                pw = bool(getattr(cmd, "power_only", False))
                reg.ack(due, power_write=pw) if ack_power else reg.ack(due)

    def check_ack(now: float) -> None:
        if last is not None and not last[2] and newest(last[0]) and now - last[1] > ack_timeout_s:
            last[2] = True
            if has_fail:
                reg.fail()

    def publish(now: float, cmd) -> float | None:
        nonlocal last, published
        result = outcome(published)
        published += 1
        last = [cmd, now, False]
        signed = None if cmd.kind == "normal" else float(cmd.signed_w)
        if result == OK:
            sim.command(now, signed)
        if result != TIMEOUT:
            responses.append((now + ack_s, cmd, result == OK))
        return signed

    out = []
    for r in rows:
        t = r["t"]
        if cfgs is not None:
            c = cfgs.at(t)
            if c is not None and c is not cur_cfg:
                cur_cfg = c
                for k, v in coerce(mod, {**c, **overrides})[0].items():
                    setattr(s, k, v)
        if sim is None or r["brk"]:
            sim = plantmod.Sim(plant, r["battery"], None if sim is None else sim.aim)
            sim.t = t
        b = sim.advance(t)
        respond(t)
        check_ack(t)
        grid = r["demand"] + loss(b) - b
        kw = {"soc": r["soc"]} if takes_soc else {}
        if takes_solar:
            kw["solar_w"] = r["solar"]
        reg.update_grid(t, grid, b, **kw)
        if has_load and r["load_w"] is not None:
            reg.update_load(t, r["load_w"])
        logged_target = r["cfg_target"] if r["cfg_target"] is not None else s.target_w
        offset = r["target"] - logged_target
        pk = {"max_charge_w": max_charge_w, "max_discharge_w": max_discharge_w, "charge_soc": charge_soc,
              "discharge_soc": discharge_soc, "target_offset_w": offset}
        policy = mod.Policy(mod.REGULATE, **{k: v for k, v in pk.items() if k in pol_fields})
        cmd = reg.step(t, policy)
        signed = None if cmd is None else publish(t, cmd)
        out.append({"t": t, "seq": r["seq"], "brk": r["brk"], "grid_w": grid, "battery": b, "soc": r["soc"],
                    "target": s.target_w + offset, "error_w": grid - (s.target_w + offset),
                    "request_w": reg.request_w, "output_w": reg.output_w, "sent": signed,
                    "sent_normal": cmd is not None and cmd.kind == "normal", "solar": r["solar"],
                    "cmds": [] if cmd is None else [signed],
                    "demand": r["demand"], "deadband_w": s.deadband_w if hasattr(s, "deadband_w") else None})
    return out, warnings


def add_variant_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--plant", help="saved plant JSON; default: fit on the loaded window")
    p.add_argument("--settings-from-log", action="store_true", help="start from the cfg in force at each sample")
    p.add_argument("--max-charge-w", type=float, default=4000.0)
    p.add_argument("--max-discharge-w", type=float, default=4000.0)
    p.add_argument("--discharge-soc", type=int, default=10)
    p.add_argument("--ack-s", type=float, default=ACK_S, help="seconds from a command to its response")
    p.add_argument("--fail-every", type=int, default=0, help="refuse every Nth command (0 never)")
    p.add_argument("--timeout-every", type=int, default=0, help="leave every Nth command unanswered (0 never)")
    p.add_argument("--json", action="store_true")


def variant_kwargs(a) -> dict:
    """replay() keyword arguments from add_variant_args and logs.add_window_args options."""
    return {"max_charge_w": a.max_charge_w, "max_discharge_w": a.max_discharge_w, "discharge_soc": a.discharge_soc,
            "loss": logs.loss_from_args(a), "ack_s": a.ack_s, "outcome": every(a.fail_every, a.timeout_every)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Replay logged demand through a regulator against the inverter model.")
    logs.add_window_args(p)
    add_variant_args(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--regulator", help="path to a regulator.py (default: the working tree's)")
    g.add_argument("--ref", help="git ref to take regulator.py from")
    p.add_argument("--set", action="append", default=[], help="Settings field=value, repeatable")
    a = p.parse_args(argv)
    recs, bad = logs.load(a.logs, a.t0, a.t1, a.solar_entity)
    rows = logs.samples(recs, a.t0, a.t1, a.exclude, a.solar_entity, logs.loss_from_args(a))
    if not rows:
        raise SystemExit("no regulating samples in the window")
    pl = plantmod.Plant.load(a.plant) if a.plant else plantmod.fit(rows, logs.commands(recs))[0]
    mod = load_regulator(a.regulator, a.ref)
    sim, warnings = replay(mod, rows, pl, parse_sets(a.set), logs.CfgTimeline(recs) if a.settings_from_log else None,
                           **variant_kwargs(a))
    for w in warnings:
        print(w, file=sys.stderr)
    sc = score.score(sim)
    if a.json:
        print(json.dumps({"plant": dataclasses.asdict(pl), "bad_lines": bad, "score": sc}, indent=1))
    else:
        score.print_table(sc)


if __name__ == "__main__":
    main()
