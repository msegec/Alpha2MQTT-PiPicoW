"""The whole bench on one synthetic log, with the shipped regulator.py: a live run is simulated against a known
plant and written as the controller writes its step log, then loaded, fitted, replayed and scored through the
command line entry points."""

import dataclasses
import json

import pytest

import compare
import plant as P
import replay
import score
from synth import write_jsonl

REG = replay.load_regulator()
TRUE = P.Plant(dead_s=0.6, tau_discharge_s=1.2, tau_charge_s=0.8,
               offset=[[-2000.0, -30.0], [-500.0, -10.0], [500.0, -20.0], [2000.0, 15.0]])
LEVELS = (300.0, 1500.0, -800.0, 900.0, -1600.0, 200.0)  # house demand net of solar, W, 30 s each


def live_log(cycles=6, settings=None):
    """Step and bus records of a live run: the regulator against TRUE, a sample a second, no loss term."""
    s = settings or REG.Settings()
    reg = REG.Regulator(dataclasses.replace(s))
    sim = P.Sim(TRUE, 0.0)
    t0 = 1_700_000_000.0
    sim.t = t0
    recs = [{"k": "start", "t": t0 - 1, "code": "synthetic"}, {"k": "cfg", "t": t0 - 1, **dataclasses.asdict(s)}]
    policy = REG.Policy(REG.REGULATE, max_charge_w=3000.0, max_discharge_w=3000.0, discharge_soc=10)
    n = 0
    for _ in range(cycles):
        for demand in LEVELS:
            for _ in range(30):
                t = t0 + n
                b = sim.advance(t)
                grid = demand - b
                reg.update_grid(t, grid, b, soc=50.0, solar_w=0.0)
                cmd = reg.step(t, policy)
                sent = None
                if cmd is not None:
                    sent = {"kind": cmd.kind, "watts": cmd.watts, "soc": cmd.soc, "power_only": cmd.power_only}
                    sim.command(t + 0.05, None if cmd.kind == "normal" else float(cmd.signed_w))
                    reg.ack(t + 0.15, power_write=cmd.power_only)
                recs.append({"k": "step", "t": t, "seq": n + 1, "ms": n * 1000, "read_ms": 150, "grid": grid,
                             "pv": 0, "battery": b, "soc": 50.0, "dispatch": "Start", "wait_ms": 0,
                             "status": "regulating", "limit": reg.limit, "grid_w": grid, "solar_w": 0.0,
                             "error_w": reg.error_w, "integral_w": reg.integral_w, "ff_w": reg.ff_w,
                             "request_w": reg.request_w, "output_w": reg.output_w, "offset_w": reg.offset_w,
                             "sent": sent})
                if cmd is not None:
                    recs.append({"k": "cmd", "t": t + 0.05, "topic": f"A/request/set/{cmd.kind}",
                                 "p": json.dumps({"watts": cmd.watts, "socPercent": cmd.soc, "seq": n})})
                    recs.append({"k": "ack", "t": t + 0.15, "topic": f"A/response/set/{cmd.kind}",
                                 "p": json.dumps({"responseStatus": "success", "seq": n})})
                n += 1
    return recs


@pytest.fixture(scope="module")
def log(tmp_path_factory):
    f = tmp_path_factory.mktemp("logs") / "alpha2mqtt.jsonl"
    write_jsonl(f, live_log())
    return str(f)


def run_json(main, argv, capsys):
    main(argv)
    return json.loads(capsys.readouterr().out)


def test_the_fit_finds_the_plant_the_log_was_made_with(log, capsys):
    out = run_json(replay.main, [log, "--loss-share", "0", "--loss-w", "0", "--json"], capsys)
    got = out["plant"]
    assert got["dead_s"] == pytest.approx(TRUE.dead_s + 0.05, abs=0.3)  # the bus echo comes 0.05 s after the step
    assert got["tau_discharge_s"] == pytest.approx(TRUE.tau_discharge_s, abs=0.5)
    assert got["tau_charge_s"] == pytest.approx(TRUE.tau_charge_s, abs=0.5)


def test_replaying_the_live_regulator_on_its_own_log_scores_like_live(log, capsys):
    common = [log, "--loss-share", "0", "--loss-w", "0", "--settings-from-log", "--tick-s", "0", "--json"]
    sim = run_json(replay.main, common, capsys)["score"]["all"]
    rows = replay.logs.samples(replay.logs.load([log])[0], loss=replay.logs.Loss(0, 0))
    live = score.score(rows)["all"]
    assert sim["samples"] == live["samples"] == 6 * len(LEVELS) * 30
    assert sim["cost_w"] == pytest.approx(live["cost_w"], rel=0.05)
    assert sim["commands"] == pytest.approx(live["commands"], abs=2)
    assert sim["turn_rounds"] == live["turn_rounds"]


def test_compare_ranks_a_detuned_variant_below_the_live_one(log, capsys):
    t0 = 1_700_000_000.0
    half = f"{t0}..{t0 + 540}"
    rest = f"{t0 + 540}..{t0 + 1080}"
    out = run_json(compare.main, [log, "--window", half, "--window", rest, "--loss-share", "0", "--loss-w", "0",
                                  "--settings-from-log", "--variant", "now", "--variant", "slow:kp=0.2",
                                  "--json"], capsys)
    res = out["results"]
    assert set(res) == {f"{replay.logs.fmt_time(t0)}..{replay.logs.fmt_time(t0 + 540)}",
                        f"{replay.logs.fmt_time(t0 + 540)}..{replay.logs.fmt_time(t0 + 1080)}", "all windows"}
    every = res["all windows"]
    assert set(every) == {"live", "now", "slow"}
    assert every["now"]["all"]["samples"] == 6 * len(LEVELS) * 30
    assert every["slow"]["all"]["cost_w"] > every["now"]["all"]["cost_w"]
    assert [v["name"] for v in out["variants"]] == ["now", "slow"]
