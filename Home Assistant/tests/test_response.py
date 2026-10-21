"""Tests for the step response monitor. Loads response.py by path so Home Assistant is not needed."""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

_DIR = Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_response", _DIR / f"{name}.py")
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


P = _load("response")
R = _load("regulator")  # the monitor's limits are regulator Settings fields


def run(plant, setpoints, seconds=60, start_w=100.0, **settings):
    """Sends each setpoint at its second and samples the plant every second; returns the finished records."""
    m = P.ResponseMonitor(replace(R.Settings(), **settings))
    battery, records = start_w, []
    target = None
    for t in range(seconds + 1):
        if t in setpoints:
            target = setpoints[t]
            records += m.command(t, target, battery)
        battery = plant(battery, target)
        records += m.sample(t + 1, battery)
    return records


def fast(battery, target):
    """Settles a 1.9 kW step in 3 s, as a healthy inverter does."""
    if target is None:
        return battery
    return battery + max(-650, min(650, target - battery))


def ramp(battery, target):
    """Climbs at 9 W/s, as the slow case seen live."""
    if target is None or target <= battery:
        return battery if target is None else target
    return min(target, battery + 9)


def test_a_fast_plant_is_not_slow():
    (record,) = run(fast, {0: 2000})
    assert record["result"] == "ok" and record["t80_s"] <= 3
    assert record["rise_w"] == 1900


def test_a_9_w_per_s_ramp_from_100_to_2000_w_times_out_as_slow():
    (record,) = run(ramp, {0: 2000})
    assert record["result"] == "timeout" and record["t80_s"] is None
    assert record["slow"] and round(record["slope_w_s"]) == 9


def test_a_step_that_reaches_80_percent_after_4_s_is_slow():
    # 600 W rise at 80 W/s: 480 W takes 6 s.
    (record,) = run(lambda b, t: min(t, b + 80), {0: 700})
    assert record["result"] == "slow" and 5 <= record["t80_s"] <= 7 and record["slow"]


def test_a_step_that_reaches_80_percent_in_4_s_is_ok():
    # 600 W rise at 120 W/s: 480 W takes 4 s.
    (record,) = run(lambda b, t: min(t, b + 120), {0: 700})
    assert record["result"] == "ok" and record["t80_s"] <= 4 and not record["slow"]


def test_a_request_that_drops_below_80_percent_is_cancelled():
    (record,) = run(ramp, {0: 2000, 10: 1500})
    assert record["result"] == "cancelled" and not record["slow"]


def test_a_request_that_holds_above_80_percent_keeps_measuring():
    (record,) = run(ramp, {0: 2000, 10: 1700})
    assert record["result"] == "timeout"


def test_a_release_cancels():
    (record,) = run(ramp, {0: 2000, 10: None})
    assert record["result"] == "cancelled"


def test_a_down_step_is_ignored():
    assert run(fast, {0: 300}, start_w=2000) == []


def test_a_step_under_the_threshold_is_ignored():
    assert run(ramp, {0: 500}) == []


def test_a_charge_step_is_measured_the_same_way():
    (record,) = run(lambda b, t: max(t, b - 9), {0: -2000}, start_w=-100)
    assert record["result"] == "timeout" and record["rise_w"] == 1900


def test_a_resend_of_the_same_setpoint_starts_no_new_measurement():
    # Keepalives repeat a setpoint the battery still lags: one slow step, not one per keepalive.
    records = run(ramp, {0: 2000, 40: 2000, 80: 2000}, seconds=120)
    assert [r["result"] for r in records] == ["timeout"]


def test_the_monitor_limits_are_settings_with_the_old_values():
    s = R.Settings()
    assert (s.response_step_w, s.response_slow_s, s.response_timeout_s) == (500, 4, 30)


def test_the_monitor_reads_its_limits_from_the_settings():
    slow = lambda b, t: min(t, b + 80)  # noqa: E731  480 W of a 600 W rise takes 6 s
    assert run(slow, {0: 700}, response_slow_s=6)[0]["result"] == "ok"
    assert run(ramp, {0: 2000}, response_timeout_s=10)[0]["elapsed_s"] == 10
    assert run(ramp, {0: 500}, response_step_w=300)[0]["result"] == "timeout"
