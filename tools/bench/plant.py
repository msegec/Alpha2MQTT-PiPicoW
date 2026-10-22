"""Inverter model for replays, and a fit of its parameters from step logs.

The model: a command takes effect dead_s after it is sent, then the battery follows a first order lag
(tau by direction) towards the command plus a steady offset, which depends on the signed setpoint. A
discharge asked of an idle battery can be limited to a slow ramp. Usage:

    python3 -I tools/bench/plant.py LOGS... [--from A --to B --exclude A..B] [--save plant.json] [--plant old.json]

Prints the fit and how well the model reproduces the logged battery, and with --plant how well a saved one
still does, so drift shows week to week.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logs  # noqa: E402

STEP_W = 300.0  # commands moving more than this are timed
CHANGE_W = 100.0  # a later command this different ends the timing
FULL_SOC = 97.0  # at or over this the battery may deliver less than asked
IDLE_W = 15.0  # battery under this is idle: a discharge from here may ramp slowly
OFFSET_EDGES = [-4000, -3000, -2500, -2000, -1500, -1000, -750, -500, -250, -100, -1, 1, 100, 250, 500, 750,
                1000, 1500, 2000, 2500, 3000, 4000]
MIN_BIN = 30
STEADY_W = 25.0  # a settled battery moved less than this over the last 3 samples
REFUSED_W = 30.0  # a discharge rise that moved less than this was refused, not slow
MIN_RAMPS = 5


@dataclass
class Plant:
    dead_s: float = 0.7
    tau_discharge_s: float = 1.0
    tau_charge_s: float = 1.0
    offset: list[list[float]] = field(default_factory=list)  # [setpoint W, battery minus setpoint W], ascending
    ramp_w_s: float = 0.0  # 0 is off: a discharge from idle follows the lag like any other
    # Set by hand, not fitted. capacity_wh above 0 integrates the SOC from the battery power; 0 leaves it to the
    # caller (a replay sets the logged one). A discharge at or under refuse_soc, or a charge at or over full_soc,
    # delivers 0 W; None is off.
    capacity_wh: float = 0.0
    refuse_soc: float | None = None
    full_soc: float | None = None

    def offset_at(self, w: float) -> float:
        pts = self.offset
        if not pts or w == 0:
            return 0.0
        xs = [p[0] for p in pts]
        i = bisect.bisect_left(xs, w)
        if i == 0:
            return pts[0][1]
        if i == len(pts):
            return pts[-1][1]
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        return y0 + (y1 - y0) * (w - x0) / (x1 - x0)

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=1) + "\n")

    @classmethod
    def load(cls, path: str) -> "Plant":
        d = json.loads(Path(path).read_text())
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class Sim:
    """One battery driven by commands. Times in seconds, signs as the regulator: positive is discharge."""

    def __init__(self, plant: Plant, battery_w: float, aim_w: float | None = None) -> None:
        self.p = plant
        self.b = battery_w
        self.aim = battery_w if aim_w is None else aim_w
        self.pending: list[tuple[float, float]] = []
        self.ramping = False
        self.t: float | None = None
        self.soc: float | None = None  # %, None unknown: no refusal or full hold

    def aim_for(self, w: float | None) -> float:
        return 0.0 if not w else w + self.p.offset_at(w)

    def command(self, t: float, w: float | None) -> None:
        """w None is normal; the model treats it as 0 W."""
        self.pending.append((t + self.p.dead_s, 0.0 if w is None else w))

    def advance(self, t: float, h_max: float = 0.1) -> float:
        if self.t is None:
            self.t = t
        while self.t < t - 1e-9:
            h = min(h_max, t - self.t)
            self.t += h
            while self.pending and self.pending[0][0] <= self.t:
                w = self.pending.pop(0)[1]
                self.aim = self.aim_for(w)
                if self.p.ramp_w_s > 0 and w > IDLE_W and abs(self.b) <= IDLE_W:
                    self.ramping = True
            p = self.p
            aim = self.aim
            if self.soc is not None and (
                    (aim > 0 and p.refuse_soc is not None and self.soc <= p.refuse_soc)
                    or (aim < 0 and p.full_soc is not None and self.soc >= p.full_soc)):
                aim = 0.0
            tau = p.tau_discharge_s if aim > 0 or (aim == 0 and self.b > 0) else p.tau_charge_s
            nb = self.b + (aim - self.b) * (1 - math.exp(-h / max(tau, 1e-3)))
            if self.ramping:
                if aim <= 0 or nb >= aim - 1:
                    self.ramping = False
                else:
                    nb = min(nb, self.b + p.ramp_w_s * h)
            if p.capacity_wh > 0 and self.soc is not None:
                self.soc = min(max(self.soc - (self.b + nb) / 2 * h / 3600 / p.capacity_wh * 100, 0.0), 100.0)
            self.b = nb
        return self.b


def _excluded(r: dict) -> bool:
    return (r["soc"] is not None and r["soc"] >= FULL_SOC) or abs(r["request_w"] - r["output_w"]) >= logs.CAPPED_W


def fit_offset(rows: list[dict], cmds: list) -> list[list[float]]:
    """Median battery minus last command, by setpoint bin, over samples whose command has not changed for
    SETTLE_S and whose battery moved under STEADY_W over the last 3 samples, excluding high SOC and capped
    samples."""
    ct = [c[0] for c in cmds]
    changed = []  # time each command value was first sent
    for i, (t, w, _) in enumerate(cmds):
        changed.append(t if i == 0 or cmds[i - 1][1] != w else changed[-1])
    bins: dict[int, list[tuple[float, float]]] = {}
    for n, r in enumerate(rows):
        i = bisect.bisect_right(ct, r["t"]) - 1
        if i < 0 or cmds[i][1] is None or r["t"] - changed[i] < logs.SETTLE_S or _excluded(r):
            continue
        if (n < 3 or any(rows[m]["brk"] for m in range(n - 2, n + 1))
                or abs(r["battery"] - rows[n - 3]["battery"]) >= STEADY_W):
            continue
        w = cmds[i][1]
        j = bisect.bisect_right(OFFSET_EDGES, w) - 1
        if 0 <= j < len(OFFSET_EDGES) - 1:
            bins.setdefault(j, []).append((w, r["battery"] - w))
    out = []
    for j in sorted(bins):
        v = bins[j]
        if len(v) >= MIN_BIN:
            out.append([round(statistics.median(w for w, _ in v), 1), round(statistics.median(d for _, d in v), 1)])
    return out


def step_events(rows: list[dict], cmds: list, plant: Plant) -> dict[str, list[list[tuple[float, float, float]]]]:
    """For each command moving the battery over STEP_W: (seconds after the command, fraction of the move to the
    command plus offset, battery W) for up to 8 following consecutive samples, until a command over CHANGE_W
    different. Groups follow the lag the model uses: 'discharge' for a command over 0 W, or 0 W from a
    discharge; else 'charge'. A discharge rise still under half way after 6 samples is 'ramp', or 'refused' if
    the battery moved under REFUSED_W."""
    ts = [r["t"] for r in rows]
    ct = [c[0] for c in cmds]
    out: dict[str, list] = {"discharge": [], "charge": [], "ramp": [], "refused": []}
    for j, (tc, w, _) in enumerate(cmds):
        if w is None:
            continue
        i = bisect.bisect_right(ts, tc) - 1
        if i < 0 or tc - ts[i] > 2:
            continue
        b0 = rows[i]["battery"]
        aim = w + plant.offset_at(w) if w else 0.0
        if abs(w - b0) <= STEP_W or abs(aim - b0) < 1 or _excluded(rows[i]):
            continue
        ev = []
        for k in range(1, 9):
            if i + k >= len(rows) or rows[i + k]["brk"]:
                break
            r = rows[i + k]
            jj = bisect.bisect_right(ct, r["t"]) - 1
            if any(cmds[m][1] is None or abs(cmds[m][1] - w) > CHANGE_W for m in range(j + 1, jj + 1)):
                break
            ev.append((r["t"] - tc, (r["battery"] - b0) / (aim - b0), r["battery"]))
        if not ev:
            continue
        group = "discharge" if w > 0 or (w == 0 and b0 > 0) else "charge"  # as Sim picks tau
        if w > max(b0, 0) and len(ev) >= 6 and ev[-1][1] < 0.5:
            group = "refused" if max(e[2] for e in ev) - b0 < REFUSED_W else "ramp"
        out[group].append(ev)
    return out


def _curve(events: list) -> list[tuple[float, float, int]]:
    """Per sample index k: mean seconds, median fraction, count."""
    out = []
    for k in range(8):
        v = [ev[k] for ev in events if len(ev) > k]
        if len(v) >= 5:
            out.append((statistics.fmean(e[0] for e in v), statistics.median(e[1] for e in v), len(v)))
    return out


def _fit_lag(curve: list[tuple[float, float, int]]) -> tuple[float, float]:
    best = (math.inf, 0.7, 1.0)
    for d10 in range(0, 31):
        dead = d10 / 10
        for t10 in range(1, 101):
            tau = t10 / 10
            sse = sum(n * (f - (1 - math.exp(-(x - dead) / tau) if x > dead else 0.0)) ** 2 for x, f, n in curve)
            best = min(best, (sse, dead, tau))
    return best[1], best[2]


def fit(rows: list[dict], cmds: list) -> tuple[Plant, dict]:
    """Plant parameters from samples (logs.samples) and bus commands (logs.commands), plus the curves fitted.
    Dead time and lags fit the median fraction curves of the 'discharge' (up and down moves while not ramping)
    and 'charge' groups; dead time is their event-weighted mean. With MIN_RAMPS 'ramp' events, ramp_w_s is
    their median rise rate between the second and last sample."""
    plant = Plant(offset=fit_offset(rows, cmds))
    ev = step_events(rows, cmds, plant)
    curves = {g: _curve(v) for g, v in ev.items()}
    dd, td = _fit_lag(curves["discharge"]) if curves["discharge"] else (plant.dead_s, plant.tau_discharge_s)
    dc, tc = _fit_lag(curves["charge"]) if curves["charge"] else (plant.dead_s, plant.tau_charge_s)
    nd, nc = len(ev["discharge"]), len(ev["charge"])
    plant.dead_s = round((dd * nd + dc * nc) / (nd + nc), 2) if nd + nc else plant.dead_s
    plant.tau_discharge_s, plant.tau_charge_s = td, tc
    rates = [(e[-1][2] - e[1][2]) / (e[-1][0] - e[1][0]) for e in ev["ramp"] if e[-1][0] > e[1][0]]
    if len(rates) >= MIN_RAMPS:
        plant.ramp_w_s = round(max(statistics.median(rates), 1.0), 1)
    return plant, {"curves": curves, "events": {g: len(v) for g, v in ev.items()}}


def model_error(plant: Plant, rows: list[dict], cmds: list) -> dict:
    """Drive the model with the logged commands and compare with the logged battery. The model restarts from the
    logged battery at every break in the samples. Scored over samples under FULL_SOC and not capped: mean |error|,
    mean error (model minus log), the same within 8 s of a timed step, and for reference the mean |error| of
    'battery equals the last command'."""
    ct = [c[0] for c in cmds]
    sim = None
    ci = 0
    ae = bias = n = 0.0
    tae = tn = 0.0
    hold = 0.0
    last_w: float | None = None
    t_big = -math.inf
    for r in rows:
        t = r["t"]
        if r["brk"] or sim is None:
            i = bisect.bisect_right(ct, t) - 1
            last_w = cmds[i][1] if i >= 0 else None
            sim = Sim(plant, r["battery"], None if last_w is None else Sim(plant, 0).aim_for(last_w))
            sim.t = t
            ci = i + 1
            continue
        while ci < len(cmds) and cmds[ci][0] <= t:
            tc, w, _ = cmds[ci]
            sim.advance(tc)
            if w is not None and abs(w - sim.b) > STEP_W:
                t_big = tc
            sim.command(tc, w)
            last_w = w
            ci += 1
        b = sim.advance(t)
        if _excluded(r):
            continue
        d = b - r["battery"]
        ae += abs(d)
        bias += d
        n += 1
        hold += abs((last_w or 0.0) - r["battery"])
        if t - t_big <= 8:
            tae += abs(d)
            tn += 1
    return {"n": int(n), "mae_w": ae / n if n else math.nan, "bias_w": bias / n if n else math.nan,
            "step_mae_w": tae / tn if tn else math.nan, "step_n": int(tn),
            "hold_mae_w": hold / n if n else math.nan}


def fit_from_args(a) -> tuple[Plant, dict, list[dict], list]:
    recs, bad = logs.load(a.logs, a.t0, a.t1, a.solar_entity)
    if bad:
        print(f"{bad} bad lines skipped", file=sys.stderr)
    rows = logs.samples(recs, a.t0, a.t1, a.exclude, a.solar_entity, logs.loss_from_args(a))
    cmds = logs.commands(recs)
    plant, info = fit(rows, cmds)
    return plant, info, rows, cmds


def print_fit(plant: Plant, info: dict) -> None:
    print(f"dead {plant.dead_s} s  tau discharge {plant.tau_discharge_s} s  tau charge {plant.tau_charge_s} s  "
          f"slow ramp from idle {plant.ramp_w_s or 'off'} W/s")
    print("offset (setpoint W: battery minus setpoint W): "
          + "  ".join(f"{w:.0f}:{o:+.0f}" for w, o in plant.offset))
    for g, c in info["curves"].items():
        print(f"{g:9s} n {info['events'][g]:4d}  " + " ".join(f"{x:.1f}s:{f:.2f}" for x, f, _ in c))


def print_error(name: str, e: dict) -> None:
    print(f"{name}: mean |model - log| {e['mae_w']:.1f} W, bias {e['bias_w']:+.1f} W over {e['n']} samples; "
          f"within 8 s of a step {e['step_mae_w']:.1f} W ({e['step_n']}); "
          f"'battery = last command' {e['hold_mae_w']:.1f} W")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fit the inverter model to step logs.")
    logs.add_window_args(p)
    p.add_argument("--save", help="write the fitted plant as JSON")
    p.add_argument("--plant", help="also score a saved plant on these logs")
    a = p.parse_args(argv)
    plant, info, rows, cmds = fit_from_args(a)
    print_fit(plant, info)
    print_error("fitted", model_error(plant, rows, cmds))
    if a.plant:
        print_error(a.plant, model_error(Plant.load(a.plant), rows, cmds))
    if a.save:
        plant.save(a.save)


if __name__ == "__main__":
    main()
