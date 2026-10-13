"""Tests for the regulation core. Loads regulator.py by path so Home Assistant is not needed."""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "custom_components" / "alpha2mqtt_controller" / "regulator.py"
_spec = importlib.util.spec_from_file_location("regulator", _PATH)
R = importlib.util.module_from_spec(_spec)
sys.modules["regulator"] = R
_spec.loader.exec_module(R)

DISCHARGE = R.Policy(R.REGULATE, max_charge_w=0, max_discharge_w=3000, discharge_soc=20)
CHARGE = R.Policy(R.REGULATE, max_charge_w=3000, max_discharge_w=0, charge_soc=100)
BOTH = R.Policy(R.REGULATE, max_charge_w=3000, max_discharge_w=3000, charge_soc=100, discharge_soc=20)


def make(**kw):
    return R.Regulator(replace(R.Settings(), **kw))


def test_bumpless_start_from_native_battery_power():
    reg = make()
    reg.update_grid(0, grid_w=20, battery_w=600)
    cmd = reg.step(0, DISCHARGE)
    assert cmd == R.Command("discharge", 600, 20, 120)


def test_load_step_is_corrected_in_one_sample():
    reg = make(kp=1.0, deadband_w=20)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(10, 1020, 500)  # 1 kW more load, battery not moved yet
    cmd = reg.step(10, DISCHARGE)
    assert cmd.kind == "discharge"
    assert cmd.watts == 1480  # 500 + (1000 - 20 deadband)


def test_output_clamped_to_policy_direction_and_limit():
    reg = make()
    reg.update_grid(0, -800, 200)  # exporting: would want to charge
    assert reg.step(0, DISCHARGE) == R.Command("charge", 0, 100, 120)
    reg = make()
    reg.update_grid(0, 5000, 2000)
    assert reg.step(0, DISCHARGE).watts == 3000


def test_small_changes_wait_for_keepalive():
    reg = make(write_threshold_w=50, keepalive_s=40)
    reg.update_grid(0, 20, 1000)
    assert reg.step(0, DISCHARGE) is not None
    for t in range(1, 40):
        reg.update_grid(t, 20 + 45, 1000)  # 25 W after the deadband, under the threshold
        assert reg.step(t, DISCHARGE) is None
    reg.update_grid(40, 65, 1000)
    assert reg.step(40, DISCHARGE) is not None


def test_min_interval_between_commands():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 20, 1000)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, 1020, 1000)
    assert reg.step(1, DISCHARGE) is None
    assert reg.step(3, DISCHARGE).watts == 1980


def test_slew_limits_each_step():
    reg = make(slew_w_per_s=500)
    reg.update_grid(0, 20, 0)
    reg.step(0, DISCHARGE)
    reg.update_grid(4, 2020, 0)
    assert reg.step(4, DISCHARGE).watts == 1980
    reg = make(slew_w_per_s=200)
    reg.update_grid(0, 20, 0)
    reg.step(0, DISCHARGE)
    reg.update_grid(4, 2020, 0)
    assert reg.step(4, DISCHARGE).watts == 800


def test_no_flapping_between_charge_and_discharge():
    reg = make(flip_hysteresis_w=150, flip_dwell_s=60, min_interval_s=0)
    reg.update_grid(0, 520, 0)
    assert reg.step(0, BOTH).kind == "discharge"
    kinds = []
    for t in range(1, 60):
        # demand swings 400 W either side of zero every second
        grid = -400 if t % 2 else 400
        reg.update_grid(t, grid + 20, reg.output_w)
        cmd = reg.step(t, BOTH)
        if cmd is not None and cmd.watts:
            kinds.append(cmd.kind)
    assert "charge" not in kinds
    # A clear, lasting demand to charge flips once the dwell has passed.
    reg.update_grid(61, -1000, 0)
    assert reg.step(61, BOTH).kind == "charge"


def test_flip_needs_margin_even_after_dwell():
    reg = make(flip_hysteresis_w=150, deadband_w=0)
    reg.update_grid(0, 520, 0)
    reg.step(0, BOTH)
    reg.update_grid(100, -80, 0)  # wants 100 W charge, under the margin
    cmd = reg.step(100, BOTH)
    assert cmd == R.Command("charge", 0, 100, 120)


def test_stale_inputs_stop_commanding():
    reg = make(stale_after_s=30, keepalive_s=10)
    reg.update_grid(0, 20, 800)
    assert reg.step(0, DISCHARGE) is not None
    for t in range(1, 120):
        cmd = reg.step(t, DISCHARGE)
        if t > 30:
            assert cmd is None
    assert reg.status == "stale"
    # A fresh sample restarts from the measured battery power.
    reg.update_grid(120, 20, 0)
    assert reg.step(120, DISCHARGE) == R.Command("charge", 0, 100, 120)


def test_correction_updates_do_not_count_as_fresh():
    reg = make(stale_after_s=30)
    reg.update_grid(0, 20, 800)
    reg.step(0, DISCHARGE)
    reg.update_grid(35, 20, 800, fresh=False)
    assert reg.step(35, DISCHARGE) is None
    assert reg.status == "stale"


def test_unusable_inputs_stop_commanding():
    reg = make()
    reg.update_grid(0, 20, 800)
    assert reg.step(0, DISCHARGE, inputs_ok=False) is None


def test_release_sends_normal_once_while_dispatching():
    reg = make()
    reg.update_grid(0, 20, 800)
    reg.step(0, DISCHARGE)
    assert reg.step(5, R.Policy(R.RELEASE)) == R.Command("normal")
    assert reg.step(6, R.Policy(R.RELEASE)) is None


def test_release_without_dispatch_sends_nothing():
    reg = make()
    reg.update_grid(0, 20, 800)
    assert reg.step(0, R.Policy(R.RELEASE)) is None


def test_lease_expiry_means_no_normal_needed():
    reg = make(stale_after_s=1000)
    reg.update_grid(0, 20, 800)
    reg.step(0, DISCHARGE)
    assert reg.step(200, R.Policy(R.RELEASE)) is None


def test_hold_dispatches_zero():
    reg = make()
    reg.update_grid(0, 900, 800)
    assert reg.step(0, R.Policy(R.HOLD)) == R.Command("charge", 0, 100, 120)


def test_charge_uses_policy_soc():
    reg = make()
    reg.update_grid(0, -1500, 0)
    assert reg.step(0, CHARGE) == R.Command("charge", 1500, 100, 120)


def test_feed_forward_adds_load_change_since_sample():
    reg = make(kff=1.0)
    reg.update_load(0, 300)
    reg.update_grid(0, 20, 300)
    reg.step(0, DISCHARGE)
    reg.update_load(4, 2400)
    assert reg.step(4, DISCHARGE).watts == 2400
    # The next sample already holds the new load: feed forward drops out, output stays.
    reg.update_grid(10, 2120, 300)
    assert reg.step(10, DISCHARGE) is None
    assert reg.ff_w == 0
    assert reg.output_w == pytest.approx(2380)


def test_integral_bounded_when_battery_does_not_follow():
    reg = make(ki=0.05, integral_limit_w=300, min_interval_s=0)
    reg.update_grid(0, 20, 0)
    reg.step(0, DISCHARGE)
    for t in range(1, 600):
        reg.update_grid(t, 220, 0)  # battery stuck at 0, small steady import
        reg.step(t, DISCHARGE)
    assert abs(reg.integral_w) <= 300


def test_integral_stops_at_the_policy_limit():
    reg = make(ki=0.05, min_interval_s=0)
    reg.update_grid(0, 20, 3000)
    reg.step(0, DISCHARGE)
    for t in range(1, 200):
        reg.update_grid(t, 220, 3000)  # wants more than the 3000 W limit
        reg.step(t, DISCHARGE)
    assert reg.integral_w <= 10  # one sample before the limit is seen, then frozen


def test_closed_loop_settles_on_target_despite_inverter_error():
    """Inverter gives 1.046 u - 42 W and lags one sample. The loop should end inside the deadband."""
    reg = make(min_interval_s=0)
    load = 1500.0
    battery = 0.0
    applied = 0
    for t in range(0, 600, 2):
        grid = load - battery
        reg.update_grid(t, grid, battery)
        cmd = reg.step(t, DISCHARGE)
        battery = 1.046 * applied - 42 if applied > 0 else 0.0
        if cmd is not None:
            applied = cmd.signed_w
    assert abs(load - battery - 20) <= 20 + 10


def test_release_goes_out_even_when_stale():
    # Turning the controller off, or "Inverter only", must not wait for the lease when readings stop.
    reg = make(stale_after_s=30)
    reg.update_grid(0, 20, 800)
    reg.step(0, DISCHARGE)
    assert reg.step(60, DISCHARGE) is None
    assert reg.step(61, R.Policy(R.RELEASE)) == R.Command("normal")


def test_fixed_override_sends_its_watts_whatever_the_grid():
    reg = make()
    reg.update_grid(0, -1500, 0)  # exporting, regulation would charge
    force = R.Policy(R.FIXED, fixed_w=2000, discharge_soc=20)
    assert reg.step(0, force) == R.Command("discharge", 2000, 20, 120)
    assert reg.status == "fixed"


def test_fixed_override_flips_without_waiting_for_the_dwell():
    reg = make(min_interval_s=0)
    reg.update_grid(0, 520, 0)
    assert reg.step(0, BOTH).kind == "discharge"
    force = R.Policy(R.FIXED, fixed_w=-1500, charge_soc=100)
    assert reg.step(1, force) == R.Command("charge", 1500, 100, 120)


def test_estimate_takes_the_same_sample_battery_off_the_net_demand():
    # House 650 W, solar 1250 W: net demand -600 W. Battery power is positive on discharge.
    assert R.estimate_grid(-600, -580) == pytest.approx(-20)  # charging 580 W still exports 20 W
    assert R.estimate_grid(-600, -620) == pytest.approx(20)  # charging 620 W imports 20 W
    assert R.estimate_grid(225, 200) == pytest.approx(25)  # night: 200 W out against a 225 W house
    assert R.estimate_grid(100, 0, clamp_w=-16, scale=10.8) == pytest.approx(-72.8)
