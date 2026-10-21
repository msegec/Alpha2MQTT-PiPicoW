import pytest

import plant as P

TRUE = P.Plant(dead_s=0.8, tau_discharge_s=1.5, tau_charge_s=0.6, ramp_w_s=10.0,
               offset=[[-1500.0, -20.0], [-800.0, -10.0], [600.0, -30.0], [1500.0, 10.0]])
CYCLE = [(0, 20), (1000, 130), (1500, 20), (600, 20), (-800, 20), (-1500, 20), (0, 20)]


def trace(plant, cycles=12):
    """Samples every second and commands 0.3 s after a sample, from a known plant."""
    sim = P.Sim(plant, 0.0)
    sim.t = 0.0
    rows, cmds = [], []
    t = 0
    for _ in range(cycles):
        for w, hold in CYCLE:
            for k in range(hold):
                b = sim.advance(float(t))
                rows.append({"t": float(t), "battery": b, "soc": 50.0, "request_w": 0.0, "output_w": 0.0,
                             "brk": t == 0})
                if k == 0:
                    sim.advance(t + 0.3)
                    sim.command(t + 0.3, float(w))
                    cmds.append((t + 0.3, float(w), False))
                t += 1
    return rows, cmds


def test_fit_recovers_known_parameters():
    rows, cmds = trace(TRUE)
    got, info = P.fit(rows, cmds)
    assert got.dead_s == pytest.approx(0.8, abs=0.15)
    assert got.tau_discharge_s == pytest.approx(1.5, abs=0.2)
    assert got.tau_charge_s == pytest.approx(0.6, abs=0.15)
    assert got.ramp_w_s == pytest.approx(10.0, abs=0.5)
    for w in (-1500, -800, 600, 1000, 1500):
        assert got.offset_at(w) == pytest.approx(TRUE.offset_at(w), abs=2)
    assert info["events"]["ramp"] == 12


def test_the_true_plant_reproduces_its_own_trace():
    rows, cmds = trace(TRUE, cycles=3)
    e = P.model_error(TRUE, rows, cmds)
    assert e["mae_w"] < 1.0
    assert e["hold_mae_w"] > 10 * e["mae_w"]


def test_high_soc_and_capped_samples_are_left_out_of_the_offset():
    rows, cmds = trace(TRUE, cycles=3)
    for r in rows:
        r["soc"] = 98.0
    assert P.fit_offset(rows, cmds) == []


def test_dead_time_then_lag():
    sim = P.Sim(P.Plant(dead_s=1.0, tau_discharge_s=1.0), 0.0)
    sim.t = 0.0
    sim.command(0.0, 1000.0)
    assert sim.advance(1.0) == pytest.approx(0.0)
    assert sim.advance(2.0) == pytest.approx(1000 * (1 - 2.718281828 ** -1), rel=0.02)


def test_save_and_load(tmp_path):
    f = tmp_path / "p.json"
    TRUE.save(str(f))
    assert P.Plant.load(str(f)) == TRUE
