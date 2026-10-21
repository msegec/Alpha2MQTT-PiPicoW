import datetime

import pytest

import score


def row(t, error_w=0.0, demand=0.0, request_w=0.0, output_w=0.0, sent=None, solar=0.0, brk=False):
    return {"t": float(t), "brk": brk, "grid_w": error_w - 20, "error_w": error_w, "target": -20.0, "demand": demand,
            "request_w": request_w, "output_w": output_w, "sent": sent, "sent_normal": False, "solar": solar,
            "battery": 0.0, "soc": 50.0}


def test_cost_is_four_times_import_plus_export_time_weighted():
    rows = [row(0, 10), row(1, -20), row(2, 0), row(3, 30)]
    m = score.score(rows)["all"]
    assert m["import_w"] == pytest.approx(10.0)
    assert m["export_w"] == pytest.approx(5.0)
    assert m["cost_w"] == pytest.approx((4 * 40 + 20) / 4)
    assert m["cost_wh"] == pytest.approx((4 * 40 + 20) / 3600)
    assert m["abs_error_w"] == pytest.approx(15.0)
    assert m["mean_error_w"] == pytest.approx(5.0)
    assert m["hours"] == pytest.approx(4 / 3600, abs=1e-3)


def test_a_gap_counts_at_most_five_seconds():
    m = score.score([row(0, 100), row(100, 0, brk=True)])["all"]
    assert m["import_wh"] == pytest.approx(100 * 5 / 3600)


def test_commands_and_turn_rounds():
    sent = [100, -100, 0, -50, 200, None]
    rows = [row(i, sent=s) for i, s in enumerate(sent)]
    m = score.score(rows)["all"]
    assert m["commands"] == 5
    assert m["turn_rounds"] == 2  # + to -, the 0 W keeps the sign, - to +
    assert m["commands_per_min"] == pytest.approx(5 / (6 / 60))


def test_budget_sums_to_the_total_and_shares_to_one():
    rows = [row(i, error_w=(-1) ** i * 40, demand=1000 * (i % 7 == 0), sent=1 if i % 3 == 0 else None)
            for i in range(60)]
    sc = score.score(rows)
    assert sum(b["all"]["cost_wh"] for b in sc["budget"].values()) == pytest.approx(sc["all"]["cost_wh"])
    assert sum(b["all"]["share"] for b in sc["budget"].values()) == pytest.approx(1.0)


def labels(demand, **kw):
    sent = kw.pop("sent", [1] * len(demand))
    err = kw.pop("error_w", [0] * len(demand))
    rows = [row(i, error_w=e, demand=d, sent=s) for i, (d, s, e) in enumerate(zip(demand, sent, err))]
    for i, r in enumerate(rows):
        for k, v in kw.items():
            r[k] = v[i]
    return score.causes(rows)


def test_a_jump_that_comes_back_is_a_spike():
    c = labels([0] * 3 + [1000] + [0] * 10)
    assert c[3:9] == ["spike"] * 6
    assert c[9] == "noise"


def test_a_jump_that_stays_is_a_step_for_fifteen_samples():
    c = labels([0] * 3 + [1000] * 20)
    assert c[3:18] == ["step"] * 15
    assert c[18] == "noise"


def test_a_jump_across_a_break_is_not_a_step():
    c = labels([0] * 3 + [1000] * 5, brk=[False] * 3 + [True] + [False] * 4)
    assert "step" not in c


def test_a_capped_sample_is_capped_even_inside_a_step():
    n = 10
    c = labels([0] * 3 + [1000] * 7, request_w=[0] * 4 + [500] + [0] * 5, output_w=[0] * n)
    assert c[4] == "capped" and c[3] == "step" and c[5] == "step"


def test_a_steady_demand_slope_is_a_ramp():
    c = labels([40 * i for i in range(10)])
    assert c[:3] == ["noise"] * 3
    assert c[3:] == ["ramp"] * 7
    assert "ramp" not in labels([20 * i for i in range(10)])


def test_an_error_eight_seconds_after_the_last_command_is_offset():
    sent = [1] + [None] * 12
    c = labels([0] * 13, sent=sent, error_w=[10] * 12 + [3])
    assert c[:8] == ["noise"] * 8
    assert c[8:12] == ["offset"] * 4
    assert c[12] == "noise"  # inside the deadband


def test_regimes_night_clear_and_cloudy_hours():
    t0 = datetime.datetime(2026, 1, 1, 10).timestamp()
    rows = []
    for m in range(180):
        hour = m // 60
        solar = 0.0 if hour == 0 else 2000.0 if hour == 1 else (1000.0 if m % 2 else 2000.0)
        rows.append(row(t0 + 60 * m, solar=solar))
    reg = score.regimes(rows)
    assert set(reg[:60]) == {"night"}
    assert set(reg[60:120]) == {"clear"}
    assert set(reg[120:]) == {"cloudy"}
    sc = score.score(rows)
    assert sc["night"]["samples"] == sc["clear"]["samples"] == sc["cloudy"]["samples"] == 60
