"""Replay several regulator variants over the same log windows and print one table, with the live log first.

    python3 -I tools/bench/compare.py LOGS... --window 'A..B' [--window ...] [--exclude A..B]
        [--set key=value ...] --variant 'NAME:ref=GITREF,key=value,...' [--variant 'NAME:path=FILE,...']
        [--settings-from-log] [--plant plant.json] [--json]

A variant names its regulator with ref= (git ref) or path= (a regulator.py); without either it uses the working
tree's. Other keys are Settings overrides, applied over --set. One plant serves every variant: the saved one, or
the fit on all windows.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logs  # noqa: E402
import plant as plantmod  # noqa: E402
import replay  # noqa: E402
import score  # noqa: E402

COLUMNS = (("cost_w", "cost W", 7, 1), ("import_w", "imp W", 6, 1), ("export_w", "exp W", 6, 1),
           ("abs_error_w", "|e| W", 6, 1), ("commands_per_min", "cmd/min", 7, 2), ("turn_rounds", "turns", 5, 0))


def parse_variant(s: str) -> dict:
    name, sep, spec = s.partition(":")
    if not sep:
        name, spec = s, ""
    v = {"name": name, "ref": None, "path": None, "set": {}}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        k, eq, val = item.partition("=")
        if not eq:
            raise SystemExit(f"variant item wants key=value: {item!r}")
        if k in ("ref", "path"):
            v[k] = val
        else:
            v["set"][k] = val
    return v


def run(rows_by_window: dict[str, list[dict]], variants: list[dict], pl: plantmod.Plant, base_set: dict,
        cfgs: logs.CfgTimeline | None, kwargs: dict) -> dict:
    """{window: {'live': scorecard, variant name: scorecard}}, plus an 'all windows' entry over every window."""
    out: dict[str, dict] = {}
    mods = {}
    for v in variants:
        key = (v["ref"], v["path"])
        if key not in mods:
            mods[key] = replay.load_regulator(v["path"], v["ref"])
    joined: dict[str, list[dict]] = {"live": []}
    for w, rows in rows_by_window.items():
        out[w] = {"live": score.score(rows)}
        joined["live"] += rows
        for v in variants:
            sim, warnings = replay.replay(mods[(v["ref"], v["path"])], rows, pl, {**base_set, **v["set"]}, cfgs,
                                          **kwargs)
            for msg in warnings:
                print(f"{v['name']}: {msg}", file=sys.stderr)
            out[w][v["name"]] = score.score(sim)
            joined.setdefault(v["name"], []).extend(sim)
    if len(rows_by_window) > 1:
        out["all windows"] = {k: score.score(rows) for k, rows in joined.items()}
    return out


def print_table(res: dict) -> None:
    head = f"{'window':33s} {'variant':14s} {'hours':>6s} " + " ".join(f"{h:>{n}s}" for _, h, n, _ in COLUMNS)
    top = "  top causes (cost Wh)"
    print(head + top)
    for w, by in res.items():
        for name, sc in by.items():
            m = sc["all"]
            cells = " ".join(f"{m[k]:{n}.{d}f}" if isinstance(m[k], float) and not math.isnan(m[k]) else f"{m[k]:>{n}}"
                             for k, _, n, d in COLUMNS)
            causes = sorted(score.CAUSES, key=lambda c: -sc["budget"][c]["all"]["cost_wh"])[:3]
            tops = " ".join(f"{c} {sc['budget'][c]['all']['cost_wh']:.0f}" for c in causes)
            print(f"{w:33s} {name:14s} {m['hours']:6.2f} {cells}  {tops}")
            for reg in score.REGIMES:
                r = sc[reg]
                if r["samples"]:
                    cells = " ".join(f"{r[k]:{n}.{d}f}" if isinstance(r[k], float) and not math.isnan(r[k])
                                     else f"{r[k]:>{n}}" for k, _, n, d in COLUMNS)
                    print(f"{'':33s} {'  ' + reg:14s} {r['hours']:6.2f} {cells}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Compare regulator variants on the same log windows.")
    p.add_argument("logs", nargs="+")
    p.add_argument("--window", type=logs.parse_span, action="append", default=[], help="'A..B', repeatable")
    p.add_argument("--exclude", type=logs.parse_span, action="append", default=[])
    p.add_argument("--solar-entity", default=logs.EAST_SUFFIX, help="as in logs.py")
    logs.add_loss_args(p)
    p.add_argument("--set", action="append", default=[], help="Settings for every variant, key=value")
    p.add_argument("--variant", action="append", default=[], help="'NAME:ref=GITREF,key=value,...'")
    replay.add_variant_args(p)
    a = p.parse_args(argv)
    if not a.window:
        raise SystemExit("give at least one --window")
    variants = [parse_variant(v) for v in a.variant]
    t0 = min(w[0] for w in a.window)
    t1 = max(w[1] for w in a.window)
    recs, bad = logs.load(a.logs, t0, t1, a.solar_entity)
    if bad:
        print(f"{bad} bad lines skipped", file=sys.stderr)
    loss = logs.loss_from_args(a)
    label = lambda w: f"{logs.fmt_time(w[0])}..{logs.fmt_time(w[1])}"
    rows_by_window = {label(w): logs.samples(recs, w[0], w[1], a.exclude, a.solar_entity, loss)
                      for w in a.window}
    empty = [w for w, rows in rows_by_window.items() if not rows]
    if empty:
        raise SystemExit(f"no regulating samples in {', '.join(empty)}")
    if a.plant:
        pl = plantmod.Plant.load(a.plant)
    else:
        allrows = [r for rows in rows_by_window.values() for r in rows]
        pl = plantmod.fit(allrows, logs.commands(recs))[0]
    cfgs = logs.CfgTimeline(recs) if a.settings_from_log else None
    res = run(rows_by_window, variants, pl, replay.parse_sets(a.set), cfgs, replay.variant_kwargs(a))
    if a.json:
        print(json.dumps({"plant": dataclasses.asdict(pl), "variants": variants, "results": res}, indent=1))
    else:
        print("plant: " + json.dumps(dataclasses.asdict(pl)))
        print_table(res)


if __name__ == "__main__":
    main()
