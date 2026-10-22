"""Synthetic log records for the bench tests."""

import json


def step(t, seq, grid_w=0.0, battery=0.0, error_w=None, request_w=None, output_w=None, sent=None, soc=50.0,
         pv=0, status="regulating", **extra):
    """A step record as the controller writes it."""
    r = {"k": "step", "t": t, "seq": seq, "ms": seq * 1000, "read_ms": 150, "grid": grid_w, "pv": pv,
         "battery": battery, "soc": soc, "dispatch": "Start", "wait_ms": 0, "status": status, "grid_w": grid_w,
         "load_w": None, "error_w": grid_w + 20 if error_w is None else error_w, "integral_w": 0.0, "ff_w": 0.0,
         "request_w": battery if request_w is None else request_w,
         "output_w": battery if output_w is None else output_w, "sent": sent}
    r.update(extra)
    return r


def write_jsonl(path, recs, bad_lines=()):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        for line in bad_lines:
            f.write(line + "\n")


def rows(n=30, demand=500.0, target=-50.0, cfg_target=-20.0, load_w=None, soc=60.0, solar=321.0):
    """Sample rows as logs.samples gives them, one a second at a steady demand, for replays."""
    return [{"t": float(i), "seq": i + 1, "brk": i == 0, "grid_w": demand + target, "battery": 0.0, "soc": soc,
             "error_w": demand, "target": target, "request_w": 0.0, "output_w": 0.0, "sent": None,
             "sent_normal": False, "power_only": False, "solar": solar, "demand": demand, "limit": None,
             "load_w": load_w, "cfg_target": cfg_target} for i in range(n)]
