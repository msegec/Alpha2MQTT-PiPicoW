"""Scorecard for a live log window or a replay: cost, error, commands, turn rounds and the loss budget by cause,
split by night, clear day and cloudy day.

    python3 -I tools/bench/score.py LOGS... [--from A --to B --exclude A..B] [--json]

Definitions are in facts/bench.md; the constants below own the numbers.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logs  # noqa: E402

# cost = IMPORT_WEIGHT x import + export, both of the error from target, not of the grid flow. 4 is the ratio the
# regulator's ki_import_x comment gives (import costs about four times what export earns); no tariff is in the repo.
# It sets the ranking of variants that trade import for export; the plain totals are in import_w and export_w.
IMPORT_WEIGHT = 4.0
MAX_DT_S = 5.0  # a sample counts for the time to the next one, at most this
NIGHT_SOLAR_W = 50.0  # solar under this is night
# Hourly solar variability over this is a cloudy hour. Chosen once: it split clear and broken cloud hours on the
# development logs. It moves rows between the clear and cloudy columns only; the "all" totals do not depend on it.
CLOUDY = 0.05
VAR_FLOOR_W = 300.0  # variability is relative to the hour's mean solar, at least this
JUMP_W = 500.0  # demand move between consecutive samples that is a spike or a step
BACK = 0.3  # a spike comes back within this share of the jump
SPIKE_ROWS, STEP_ROWS, BACK_ROWS = 6, 15, 3
RAMP_W_S = 30.0  # demand moving faster than this over 3 samples is a ramp
# offset needs |error| over the row's deadband_w (the cfg in force, or the replayed setting), at least OFFSET_MIN_W:
# a loop at rest inside its deadband is noise. A row without one takes DEADBAND_W, the regulator's default.
DEADBAND_W = 20.0
OFFSET_MIN_W = 5.0
CAUSES = ("capped", "spike", "step", "ramp", "offset", "noise")
REGIMES = ("night", "clear", "cloudy")


def regimes(rows: list[dict]) -> list[str]:
    """Night when solar < NIGHT_SOLAR_W. Day samples take their local clock hour's class: from the 1-minute means
    of day solar in that hour, variability = mean |m[i] - (m[i-1] + m[i+1]) / 2| / max(mean solar, VAR_FLOOR_W);
    over CLOUDY is cloudy. An hour with under 3 day minutes is clear."""
    minutes: dict[str, dict[int, list[float]]] = {}
    keys = []
    for r in rows:
        d = datetime.datetime.fromtimestamp(r["t"])
        k = d.strftime("%Y-%m-%d %H")
        keys.append(k)
        if r["solar"] >= NIGHT_SOLAR_W:
            minutes.setdefault(k, {}).setdefault(d.minute, []).append(r["solar"])
    cloudy = set()
    for k, mins in minutes.items():
        m = [statistics.fmean(v) for _, v in sorted(mins.items())]
        if len(m) >= 3:
            d2 = statistics.fmean(abs(b - (a + c) / 2) for a, b, c in zip(m, m[1:], m[2:]))
            if d2 / max(statistics.fmean(m), VAR_FLOOR_W) > CLOUDY:
                cloudy.add(k)
    return ["night" if r["solar"] < NIGHT_SOLAR_W else "cloudy" if k in cloudy else "clear"
            for r, k in zip(rows, keys)]


def causes(rows: list[dict]) -> list[str]:
    """One cause per sample, first match wins:
    capped: |request_w - output_w| >= 30 W, a limit holds the output.
    spike:  within 6 samples of a demand jump over 500 W that came back within 30 % in the next 3 samples.
    step:   within 15 samples of a demand jump over 500 W that stayed.
    ramp:   demand moved faster than 30 W/s over the last 3 consecutive samples.
    offset: no command for 8 s and |error| > the row's deadband_w (DEADBAND_W unlogged), at least 5 W.
    noise:  the rest.
    Demand is the logged demand (grid_w + battery less the loss term), the same for live and replay. A jump
    needs consecutive samples; a jump inside a spike (its way back) starts nothing. budget.py tagged that way back
    as a step."""
    n = len(rows)
    dem = [r["demand"] for r in rows]
    tag: list[str | None] = [None] * n
    for i in range(1, n):
        j = dem[i] - dem[i - 1]
        if abs(j) > JUMP_W and not rows[i]["brk"] and tag[i] != "spike":
            back = any(abs(dem[k] - dem[i - 1]) < BACK * abs(j) for k in range(i + 1, min(i + 1 + BACK_ROWS, n)))
            lab = "spike" if back else "step"
            for k in range(i, min(i + (SPIKE_ROWS if back else STEP_ROWS), n)):
                tag[k] = tag[k] or lab
    out = []
    last_sent = -float("inf")
    for i, r in enumerate(rows):
        if r["sent"] is not None or r.get("sent_normal"):
            last_sent = r["t"]
        c = tag[i]
        if abs(r["request_w"] - r["output_w"]) >= logs.CAPPED_W:
            c = "capped"
        elif c is None and _ramp(rows, dem, i):
            c = "ramp"
        elif c is None and r["t"] - last_sent >= logs.SETTLE_S and abs(r["error_w"]) > _offset_w(r):
            c = "offset"
        elif c is None:
            c = "noise"
        out.append(c)
    return out


def _offset_w(r: dict) -> float:
    d = r.get("deadband_w")
    return max(DEADBAND_W if d is None else d, OFFSET_MIN_W)


def _ramp(rows: list[dict], dem: list[float], i: int) -> bool:
    if i < 3 or any(rows[k]["brk"] for k in range(i - 2, i + 1)):
        return False
    return abs(dem[i] - dem[i - 3]) / max(rows[i]["t"] - rows[i - 3]["t"], 1e-3) > RAMP_W_S


def _blank() -> dict:
    return {"s": 0.0, "imp": 0.0, "exp": 0.0, "abs": 0.0, "err": 0.0, "gimp": 0.0, "gexp": 0.0, "n": 0, "cmds": 0,
            "turns": 0}


def _finish(a: dict) -> dict:
    s = a["s"]
    h = s / 3600
    f = (lambda x: x / s) if s else (lambda x: float("nan"))
    cost = IMPORT_WEIGHT * a["imp"] + a["exp"]
    return {"hours": round(h, 3), "samples": a["n"], "cost_w": f(cost), "cost_wh": cost / 3600,
            "import_w": f(a["imp"]), "import_wh": a["imp"] / 3600,
            "export_w": f(a["exp"]), "export_wh": a["exp"] / 3600,
            "abs_error_w": f(a["abs"]), "mean_error_w": f(a["err"]),
            "grid_import_w": f(a["gimp"]), "grid_export_w": f(a["gexp"]),
            "commands_per_min": a["cmds"] / (s / 60) if s else float("nan"), "commands": a["cmds"],
            "turn_rounds": a["turns"]}


def score(rows: list[dict]) -> dict:
    """Scorecard over rows shaped like logs.samples or replay.replay output. Import and export are the error
    (grid_w - target) above and below 0; grid_import_w and grid_export_w are the grid itself."""
    reg = regimes(rows)
    cau = causes(rows)
    tot = {k: _blank() for k in ("all",) + REGIMES}
    bud = {c: {k: [0.0, 0.0, 0] for k in ("all",) + REGIMES} for c in CAUSES}
    last_sign = 0
    for i, r in enumerate(rows):
        dt = min(rows[i + 1]["t"] - r["t"], MAX_DT_S) if i + 1 < len(rows) else 1.0
        dt = max(dt, 0.0)
        e = r["error_w"]
        g = r["grid_w"]
        turn = 0
        sent = r["sent"] is not None or r.get("sent_normal")
        if r["sent"]:
            sg = 1 if r["sent"] > 0 else -1
            turn = int(last_sign != 0 and sg != last_sign)
            last_sign = sg
        for k in ("all", reg[i]):
            a = tot[k]
            a["s"] += dt
            a["imp"] += max(e, 0) * dt
            a["exp"] += max(-e, 0) * dt
            a["abs"] += abs(e) * dt
            a["err"] += e * dt
            a["gimp"] += max(g, 0) * dt
            a["gexp"] += max(-g, 0) * dt
            a["n"] += 1
            a["cmds"] += int(bool(sent))
            a["turns"] += turn
            b = bud[cau[i]][k]
            b[0] += max(e, 0) * dt / 3600
            b[1] += max(-e, 0) * dt / 3600
            b[2] += 1
    out = {k: _finish(v) for k, v in tot.items()}
    budget = {}
    for c in CAUSES:
        budget[c] = {}
        for k in ("all",) + REGIMES:
            imp, exp, n = bud[c][k]
            whole = out[k]["cost_wh"]
            cost = IMPORT_WEIGHT * imp + exp
            budget[c][k] = {"import_wh": imp, "export_wh": exp, "cost_wh": cost, "samples": n,
                            "share": cost / whole if whole else float("nan")}
    out["budget"] = budget
    return out


def print_table(sc: dict, title: str = "") -> None:
    if title:
        print(title)
    print(f"{'':8s} {'hours':>6s} {'cost W':>7s} {'cost Wh':>8s} {'imp W':>6s} {'imp Wh':>7s} {'exp W':>6s} "
          f"{'exp Wh':>7s} {'|e| W':>6s} {'cmd/min':>7s} {'turns':>5s}")
    for k in ("all",) + REGIMES:
        m = sc[k]
        if not m["samples"]:
            continue
        print(f"{k:8s} {m['hours']:6.2f} {m['cost_w']:7.1f} {m['cost_wh']:8.1f} {m['import_w']:6.1f} "
              f"{m['import_wh']:7.1f} {m['export_w']:6.1f} {m['export_wh']:7.1f} {m['abs_error_w']:6.1f} "
              f"{m['commands_per_min']:7.2f} {m['turn_rounds']:5d}")
    print(f"{'cause':8s} {'imp Wh':>7s} {'exp Wh':>7s} {'cost Wh':>8s} {'share':>6s} {'samples':>8s}   cost Wh: "
          + " ".join(f"{k:>7s}" for k in REGIMES))
    for c in sorted(CAUSES, key=lambda c: -sc["budget"][c]["all"]["cost_wh"]):
        b = sc["budget"][c]["all"]
        sh = f"{100 * b['share']:5.1f}%" if b["share"] == b["share"] else "   -  "
        print(f"{c:8s} {b['import_wh']:7.1f} {b['export_wh']:7.1f} {b['cost_wh']:8.1f} {sh} {b['samples']:8d}   "
              + "          " + " ".join(f"{sc['budget'][c][k]['cost_wh']:7.1f}" for k in REGIMES))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Score a live log window.")
    logs.add_window_args(p)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    recs, bad = logs.load(a.logs, a.t0, a.t1, a.solar_entity)
    rows = logs.samples(recs, a.t0, a.t1, a.exclude, a.solar_entity, logs.loss_from_args(a))
    if not rows:
        raise SystemExit("no regulating samples in the window")
    sc = score(rows)
    if a.json:
        print(json.dumps({"bad_lines": bad, "score": sc}, indent=1))
    else:
        print_table(sc)


if __name__ == "__main__":
    main()
