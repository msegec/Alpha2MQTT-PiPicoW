"""Load controller step logs (alpha2mqtt.jsonl and its daily .gz files) into time-ordered records.

Usage, to list the iterations in some logs:

    python3 -I tools/bench/logs.py 'LOGDIR/alpha2mqtt.jsonl*'
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import glob
import gzip
import json
import math
import sys
from dataclasses import dataclass

# Battery loss in the logged grid estimate: a correction source that models inverter loss charges this share of
# |battery| plus a fixed term at every reading, 0 W included. demand takes it off; a replay adds it back. A source
# without a loss term wants --loss-share 0 --loss-w 0.
LOSS_SHARE = 0.15
LOSS_W = 10.0
GAP_S = 5.0  # consecutive samples are at most this far apart, and their seq differs by 1
EAST_SUFFIX = None  # entity id suffix of a second solar meter for older logs; installation specific, so none by default
WEST_MIN_W = 95.0  # older logs: pv * -10 under this is meter noise, not sun
SETTLE_S = 8.0  # a command unchanged this long has settled
CAPPED_W = 30.0  # request this far from output: a limit holds the output


@dataclass(frozen=True)
class Loss:
    """Battery loss in W at a battery reading: share x |battery| plus fixed_w, also at 0 W."""
    share: float = LOSS_SHARE
    fixed_w: float = LOSS_W

    def __call__(self, battery_w: float) -> float:
        return self.share * abs(battery_w) + self.fixed_w


def loss_from_args(a) -> Loss:
    return Loss(a.loss_share, a.loss_w)


def expand(patterns: list[str]) -> list[str]:
    """Files for globs or plain paths, oldest daily file first, the live file last."""
    out: list[str] = []
    for p in patterns:
        hits = sorted(glob.glob(p)) or [p]
        out += [h for h in hits if h not in out]
    return sorted(out, key=lambda f: (not f.endswith(".gz"), f))


def load(patterns: list[str], t0: float = -math.inf, t1: float = math.inf,
         solar_suffix: str | None = EAST_SUFFIX) -> tuple[list[dict], int]:
    """Records sorted by t, and the number of bad lines skipped. Entity records are kept only for the
    solar meter, none without a solar_suffix. start, cfg and learned table records are kept from before t0, so
    the cfg and table in force are known."""
    recs: list[dict] = []
    bad = 0
    for f in expand(patterns):
        opener = gzip.open if f.endswith(".gz") else open
        try:
            fh = opener(f, "rt", encoding="utf-8", errors="replace")
        except OSError as err:
            print(f"skip {f}: {err}", file=sys.stderr)
            continue
        with fh:
            try:
                for line in fh:
                    if '"k":"e"' in line and (not solar_suffix or solar_suffix not in line):
                        continue
                    try:
                        r = json.loads(line)
                        t = float(r["t"])
                        k = r["k"]
                    except (ValueError, KeyError, TypeError):
                        bad += 1
                        continue
                    if not isinstance(r, dict):
                        bad += 1
                        continue
                    r["t"] = t
                    if k in ("start", "cfg", "e", "trim_load", "trim") or t0 - 60 <= t < t1 + 60:
                        recs.append(r)
            except (OSError, EOFError) as err:  # a gzip cut short
                print(f"{f}: {err}, kept what was read", file=sys.stderr)
    recs.sort(key=lambda r: r["t"])
    return recs, bad


def parse_time(s: str) -> float:
    """Local 'YYYY-MM-DD HH:MM[:SS]', 'YYYY-MM-DD', or Unix seconds."""
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"not a time: {s!r}")


def fmt_time(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if math.isfinite(t) else str(t)


def parse_span(s: str) -> tuple[float, float]:
    """'A..B' in parse_time form."""
    a, sep, b = s.partition("..")
    if not sep:
        raise argparse.ArgumentTypeError(f"not a span A..B: {s!r}")
    return parse_time(a), parse_time(b)


def add_window_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("logs", nargs="+", help="log files or globs, .gz or plain")
    p.add_argument("--from", dest="t0", type=parse_time, default=-math.inf, help="local 'YYYY-MM-DD HH:MM'")
    p.add_argument("--to", dest="t1", type=parse_time, default=math.inf)
    p.add_argument("--exclude", type=parse_span, action="append", default=[], help="'A..B', repeatable")
    p.add_argument("--solar-entity", default=EAST_SUFFIX,
                   help="older logs without solar_w: entity id suffix of a second solar meter (default none)")
    add_loss_args(p)


def add_loss_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--loss-share", type=float, default=LOSS_SHARE, help="loss in the grid estimate, share of |battery|")
    p.add_argument("--loss-w", type=float, default=LOSS_W, help="loss in the grid estimate, fixed W at every reading")


def is_set_topic(topic: str) -> str | None:
    """'charge', 'discharge' or 'normal' for a dispatch request; None for reads, raw writes and responses."""
    head, _, kind = str(topic).rpartition("/")
    if head.endswith("request/set") and kind in ("charge", "discharge", "normal"):
        return kind
    return None


def commands(recs: list[dict]) -> list[tuple[float, float | None, bool]]:
    """Dispatch requests on the bus: (t, signed W or None for normal, powerOnly)."""
    out = []
    for r in recs:
        if r["k"] != "cmd":
            continue
        kind = is_set_topic(r.get("topic", ""))
        if kind is None:
            continue
        if kind == "normal":
            out.append((r["t"], None, False))
            continue
        try:
            p = json.loads(r["p"]) if isinstance(r["p"], str) else r["p"]
            w = float(p["watts"])
        except (ValueError, KeyError, TypeError):
            continue
        out.append((r["t"], w if kind == "discharge" else -w, p.get("powerOnly") == 1))
    return out


def _cfg_key(r: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in r.items() if k not in ("k", "t")))


def iterations(recs: list[dict]) -> list[dict]:
    """Spans of one code and one cfg. A start splits unless it carries the same code as the running span; a cfg
    splits when any setting differs. Each: t0, t1, why ('start' or 'cfg'), code (None if unlogged), cfg."""
    out: list[dict] = []
    cur: dict | None = None
    for r in recs:
        k = r["k"]
        if k == "start":
            code = r.get("code")
            if cur is None or code is None or code != cur["code"]:
                cur = {"t0": r["t"], "t1": r["t"], "why": "start", "code": code, "cfg": cur["cfg"] if cur else {}}
                out.append(cur)
        elif k == "cfg":
            cfg = {a: b for a, b in r.items() if a not in ("k", "t")}
            if cur is None:
                cur = {"t0": r["t"], "t1": r["t"], "why": "cfg", "code": None, "cfg": cfg}
                out.append(cur)
            elif cur["t0"] == r["t"] or not cur["cfg"]:
                cur["cfg"] = cfg
            elif _cfg_key(r) != _cfg_key({"k": 0, "t": 0, **cur["cfg"]}):
                cur = {"t0": r["t"], "t1": r["t"], "why": "cfg", "code": cur["code"], "cfg": cfg}
                out.append(cur)
        if cur is not None:
            cur["t1"] = r["t"]
    return out


class CfgTimeline:
    """The logged cfg in force at a time."""

    def __init__(self, recs: list[dict]) -> None:
        cfgs = [r for r in recs if r["k"] == "cfg"]
        self.ts = [r["t"] for r in cfgs]
        self.cfgs = [{a: b for a, b in r.items() if a not in ("k", "t")} for r in cfgs]

    def at(self, t: float) -> dict | None:
        i = bisect.bisect_right(self.ts, t) - 1
        return self.cfgs[i] if i >= 0 else None


def trim_at(recs: list[dict], t: float) -> list | None:
    """The learned table in force at t: the points of the last trim_load or trim record at or before t."""
    pts = None
    for r in recs:
        if r["t"] > t:
            break
        if r["k"] in ("trim_load", "trim") and isinstance(r.get("points"), list):
            pts = r["points"]
    return pts


def read_trim(path: str) -> list:
    """The points of a saved learned table: the controller's store file ({"data": {"points": ...}}), or just
    {"points": ...}."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    d = d.get("data", d) if isinstance(d, dict) else d
    if not isinstance(d, dict) or not isinstance(d.get("points"), list):
        raise SystemExit(f"{path}: no learned table points")
    return d["points"]


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def samples(recs: list[dict], t0: float = -math.inf, t1: float = math.inf,
            exclude: list[tuple[float, float]] = (), solar_suffix: str | None = EAST_SUFFIX,
            loss: Loss = Loss()) -> list[dict]:
    """Regulating step records in [t0, t1) outside the excluded spans, as flat rows:
    t, seq, grid_w, battery, soc, error_w, target, request_w, output_w, sent (signed W or None),
    power_only, solar, demand (grid_w + battery - loss(battery)), limit (None if unlogged), cfg_target and
    deadband_w (the logged cfg in force, None without one),
    brk: True when the row does not follow the previous one (seq not +1, over GAP_S, or the first row), and cmds:
    every dispatch request on the bus after this step record and before the next one (signed W, None for normal),
    so commands sent on the controller's tick count too. wait_s is how long the sample waited for its
    correction (wait_ms, 0 unlogged): the step at t ran on a sample seen wait_s earlier.
    Solar: the step's solar_w when logged, else west = pv * -10 when >= 95 W plus the last state of the
    solar_suffix entity (0 without one)."""
    cfgs = CfgTimeline(recs)
    east = 0.0
    out: list[dict] = []
    kept = False  # the last step record seen became out[-1]
    for r in recs:
        k = r["k"]
        if k == "cmd":
            cmd = commands([r])
            if kept and cmd:
                out[-1]["cmds"].append(cmd[0][1])
            continue
        if k == "e":
            if solar_suffix and str(r.get("e", "")).endswith(solar_suffix):
                east = _num(r.get("s")) or 0.0
            continue
        if k != "step":
            continue
        t = r["t"]
        kept = False
        if not (t0 <= t < t1) or any(a <= t < b for a, b in exclude) or r.get("status") != "regulating":
            continue
        vals = [_num(r.get(f)) for f in ("grid_w", "battery", "error_w", "request_w", "output_w")]
        if any(v is None for v in vals):
            continue
        kept = True
        grid_w, battery, error_w, request_w, output_w = vals
        solar = _num(r.get("solar_w"))
        if solar is None:
            west = (_num(r.get("pv")) or 0.0) * -10.0
            solar = (west if west >= WEST_MIN_W else 0.0) + east
        sent = r.get("sent")
        signed = None
        if isinstance(sent, dict) and sent.get("kind") in ("charge", "discharge"):
            w = _num(sent.get("watts")) or 0.0
            signed = w if sent["kind"] == "discharge" else -w
        cfg = cfgs.at(t) or {}
        seq = r.get("seq")
        last = out[-1] if out else None
        brk = (last is None or not isinstance(seq, int) or not isinstance(last["seq"], int)
               or seq - last["seq"] != 1 or t - last["t"] > GAP_S)
        row = {"t": t, "seq": seq, "grid_w": grid_w, "battery": battery, "soc": _num(r.get("soc")),
               "error_w": error_w, "target": grid_w - error_w, "request_w": request_w, "output_w": output_w,
               "sent": signed, "sent_normal": isinstance(sent, dict) and sent.get("kind") == "normal",
               "power_only": bool(isinstance(sent, dict) and sent.get("power_only")), "solar": solar,
               "demand": grid_w + battery - loss(battery), "limit": r.get("limit"),
               "load_w": _num(r.get("load_w")),
               "cfg_target": _num(cfg.get("target_w")), "deadband_w": _num(cfg.get("deadband_w")), "brk": brk,
               "cmds": [], "wait_s": max(_num(r.get("wait_ms")) or 0.0, 0.0) / 1000}
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="List iterations (code and cfg spans) in step logs.")
    add_window_args(p)
    a = p.parse_args(argv)
    recs, bad = load(a.logs, a.t0, a.t1, a.solar_entity)
    print(f"{len(recs)} records, {bad} bad lines")
    for it in iterations(recs):
        if it["t1"] < a.t0 or it["t0"] >= a.t1:
            continue
        print(f"{fmt_time(it['t0'])} .. {fmt_time(it['t1'])}  {it['why']:5s}  code {it['code'] or '-'}  "
              + " ".join(f"{k}={v}" for k, v in sorted(it["cfg"].items())))


if __name__ == "__main__":
    main()
