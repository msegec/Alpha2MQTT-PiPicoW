"""Tests for the regulation core. Loads regulator.py by path so Home Assistant is not needed."""

import hashlib
import importlib.util
import math
import random
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


def test_load_step_goes_out_in_one_command():
    # Steps arrive a fraction of a second apart; the step must not be cut to the slew of that fraction.
    reg = make(slew_w_per_s=2000, min_interval_s=3)
    reg.update_grid(0, 20, 2800)
    reg.step(0, DISCHARGE)
    reg.update_grid(10, 20, 2800)
    reg.step(10, DISCHARGE)
    reg.update_grid(10.3, -2080, 2800)
    assert reg.step(10.3, DISCHARGE).watts == 720  # 2800 less the 2100 W error, less the 20 W deadband


def test_slew_counts_from_the_last_command():
    reg = make(slew_w_per_s=500, min_interval_s=0, write_threshold_w=10)
    reg.update_grid(0, 20, 0)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, 2020, 0)
    assert reg.step(1, DISCHARGE).watts == 500


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


def test_default_dwell_turns_round_on_a_load_edge():
    # An air fryer element cycles every 40 s or so: the default dwell must not hold the battery at zero.
    reg = make(min_interval_s=0)
    reg.update_grid(0, -1000, 0)
    assert reg.step(0, BOTH).kind == "charge"
    reg.update_grid(6, 2000, -1000)
    assert reg.step(6, BOTH).kind == "discharge"


def test_flip_needs_margin_even_after_dwell():
    reg = make(flip_hysteresis_w=150, deadband_w=0)
    reg.update_grid(0, 520, 0)
    reg.step(0, BOTH)
    reg.update_grid(100, -80, 0)  # wants 100 W charge, under the margin
    cmd = reg.step(100, BOTH)
    assert cmd == R.Command("discharge", 0, 20, 120)  # 0 W, in the running block's kind


def test_stale_inputs_stop_commanding():
    reg = make(stale_after_s=30, keepalive_s=10)
    reg.update_grid(0, 20, 800)
    assert reg.step(0, DISCHARGE) is not None
    for t in range(1, 120):
        cmd = reg.step(t, DISCHARGE)
        if t == 31:
            assert cmd == R.Command("normal")  # handed back on the first stale step
        elif t > 31:
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
    assert reg.step(35, DISCHARGE) == R.Command("normal")
    assert reg.status == "stale"


def test_a_gap_in_samples_integrates_a_few_seconds_at_most():
    # The error before a gap is not the error through it.
    reg = make(ki=0.05, deadband_w=0, target_w=0)
    reg.update_grid(0, 0, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(25, 100, 500)
    assert reg.integral_w == pytest.approx(0.05 * 100 * R.MAX_INTEGRATE_S)


def test_release_sends_normal_once_while_dispatching():
    reg = make()
    reg.update_grid(0, 20, 800)
    reg.step(0, DISCHARGE)
    assert reg.step(5, R.Policy(R.RELEASE)) == R.Command("normal")
    assert reg.step(6, R.Policy(R.RELEASE)) is None


def test_release_without_dispatch_sends_nothing():
    reg = make()
    reg.update_grid(0, 20, 800)
    reg.step(0, R.Policy(R.RELEASE))  # the start's hand back, below
    assert reg.step(5, R.Policy(R.RELEASE)) is None


def test_a_release_at_start_hands_back_a_lease_a_crash_may_have_left():
    reg = make()
    assert reg.step(0, R.Policy(R.RELEASE)) == R.Command("normal")
    assert reg.step(1, R.Policy(R.RELEASE)) is None
    reg = make()
    assert reg.step(0, DISCHARGE) is None  # stale at start: the first sample takes over instead


def test_a_failed_normal_is_sent_again_after_the_min_interval():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 520, 0)
    reg.step(0, DISCHARGE)
    assert reg.step(5, R.Policy(R.RELEASE)) == R.Command("normal")
    reg.fail()
    assert reg.step(6, R.Policy(R.RELEASE)) is None
    assert reg.step(8, R.Policy(R.RELEASE)) == R.Command("normal")
    assert reg.step(9, R.Policy(R.RELEASE)) is None
    reg.fail()
    assert reg.step(200, DISCHARGE) == R.Command("normal")  # stale by now: still owed


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


def test_solar_feed_forward_answers_a_solar_change_on_that_sample_only():
    reg = make(kff=1.0, ki=0, min_interval_s=0)
    reg.update_grid(0, 20, 1000, solar_w=500)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, -280, 1000, solar_w=800)  # 300 W more solar: P takes 280 W off, feed forward 300 more
    assert reg.step(1, DISCHARGE).watts == 420
    assert reg.ff_w == -300
    reg.update_grid(2, 20, 420, solar_w=800)  # solar steady: feed forward drops out
    reg.step(2, DISCHARGE)
    assert reg.ff_w == 0


def test_solar_feed_forward_is_0_after_a_gap_in_samples():
    reg = make(kff=1.0, ki=0)
    reg.update_grid(0, 20, 500, solar_w=3000)
    reg.step(0, DISCHARGE)
    reg.update_grid(20, 20, 500, solar_w=500)  # 20 s without a sample while the solar fell 2500 W
    reg.step(20, DISCHARGE)
    assert reg.ff_w == 0


def test_solar_feed_forward_is_0_on_the_first_sample_back_from_stale():
    reg = make(kff=1.0, ki=0, min_interval_s=0)
    reg.update_grid(0, 20, 500, solar_w=3000)
    reg.step(0, DISCHARGE)
    assert reg.step(35, DISCHARGE) == R.Command("normal")
    reg.update_grid(36, 20, 0, solar_w=500)
    reg.step(36, DISCHARGE)
    assert reg.ff_w == 0
    reg.update_grid(37, 20, 0, solar_w=200)  # regulating again: a 1 s change feeds forward
    reg.step(37, DISCHARGE)
    assert reg.ff_w == 300


def test_solar_feed_forward_is_0_on_the_first_step_after_a_start():
    reg = make(kff=1.0, ki=0)
    reg.update_grid(0, 20, 500, solar_w=1500)  # samples come in while the controller is off
    reg.update_grid(1, 20, 500, solar_w=500)
    assert reg.step(1, DISCHARGE).watts == 500
    assert reg.ff_w == 0


@pytest.mark.parametrize("before, after", [(500, 520), (500, 480), (None, 800), (500, None), (500, math.nan)])
def test_solar_feed_forward_skips_small_or_unknown_changes(before, after):
    reg = make(kff=1.0)
    reg.update_grid(0, 20, 1000, solar_w=before)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, 20, 1000, solar_w=after)
    reg.step(1, DISCHARGE)
    assert reg.ff_w == 0


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
    assert reg.step(61, R.Policy(R.RELEASE)) == R.Command("normal")  # no step in between, readings long stale


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


@pytest.mark.parametrize("offset, watts", [(100, 1100), (-100, 900), (0, 1000)])
def test_policy_offset_moves_the_grid_target(offset, watts):
    reg = make(target_w=0, deadband_w=0)
    reg.update_grid(0, grid_w=0, battery_w=-1000)
    cmd = reg.step(0, replace(CHARGE, target_offset_w=offset))
    assert (cmd.kind, cmd.watts) == ("charge", watts)
    assert reg.error_w == -offset


HOLD = R.Policy(R.HOLD)


def test_stale_inputs_hand_the_inverter_back_at_once():
    reg = make(stale_after_s=30)
    reg.update_grid(0, 1020, 500)
    assert reg.step(0, DISCHARGE).kind == "discharge"
    assert reg.step(31, DISCHARGE) == R.Command("normal")
    assert reg.status == "stale"
    assert reg.step(32, DISCHARGE) is None  # handed back once, not every second


def test_stale_inputs_send_nothing_once_the_lease_is_over():
    reg = make(stale_after_s=30, duration_s=20)
    reg.update_grid(0, 1020, 500)
    reg.step(0, DISCHARGE)
    assert reg.step(31, DISCHARGE) is None


def test_hold_needs_no_grid_reading():
    reg = make(stale_after_s=30)
    assert reg.step(0, HOLD) == R.Command("charge", 0, 100, 120)  # no sample yet
    assert reg.step(50, HOLD) == R.Command("charge", 0, 100, 120)  # keepalive with stale inputs
    assert reg.status == "hold"


def test_non_finite_readings_are_ignored():
    reg = make()
    reg.update_grid(0, 20, 500)
    for bad in (float("nan"), float("inf"), float("-inf")):
        reg.update_grid(1, bad, 500)
        reg.update_grid(1, 20, bad)
    assert (reg.grid_w, reg.battery_w) == (20, 500)
    assert reg.step(1, DISCHARGE).watts == 500


def test_non_finite_fixed_watts_release():
    reg = make()
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    cmd = reg.step(1, R.Policy(R.FIXED, fixed_w=float("nan")))
    assert cmd == R.Command("normal")
    assert reg.status == "released"


def test_a_release_with_a_reason_names_it_in_the_status():
    reg = make()
    reg.step(0, R.Policy(R.RELEASE, reason="unknown policy state 'boost'"))
    assert reg.status == "released: unknown policy state 'boost'"
    reg.step(1, R.Policy(R.RELEASE))
    assert reg.status == "released"


def test_min_interval_holds_across_a_release():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 1020, 500)
    reg.step(0, DISCHARGE)
    assert reg.step(1, R.Policy(R.RELEASE)) == R.Command("normal")
    reg.update_grid(2, 1020, 0)
    assert reg.step(2, DISCHARGE) is None  # a flapping policy cannot hammer the inverter
    reg.update_grid(4, 1020, 0)
    assert reg.step(4, DISCHARGE) is not None


def test_a_failed_command_is_retried():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 1020, 500)
    first = reg.step(0, DISCHARGE)
    reg.fail()
    assert reg.step(1, DISCHARGE) is None  # still inside the minimum gap
    assert reg.step(3, DISCHARGE) == first
    assert reg.step(4, DISCHARGE) is None  # once


def test_a_failed_command_still_hands_the_inverter_back_on_release():
    # A failed write may have left part of the block running, so release must still send normal.
    reg = make()
    reg.update_grid(0, 1020, 500)
    reg.step(0, DISCHARGE)
    reg.fail()
    assert reg.step(1, R.Policy(R.RELEASE)) == R.Command("normal")


def test_a_failed_power_write_is_retried_as_a_full_block():
    reg = make(min_interval_s=3, power_write_gap_s=1)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 520, 500)
    assert reg.step(5, DISCHARGE).power_only
    reg.fail()
    reg.update_grid(8, 520, 500)
    assert reg.step(8, DISCHARGE) == R.Command("discharge", 980, 20, 120)


def _acked_dip(reg, ack=True, fresh=True, grid_w=1020):
    """Discharging 500 W on target, the write is acked at 1.8 s, then a sample at 1.9 s."""
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    if ack:
        reg.ack(1.8)
    reg.update_grid(1.9, grid_w, 500, fresh=fresh)


def test_acked_import_reducing_move_goes_before_the_min_interval():
    reg = make(min_interval_s=3)
    _acked_dip(reg)
    assert reg.step(1.95, DISCHARGE) is None  # inside the floor
    assert reg.step(2.0, DISCHARGE).watts == 1480


@pytest.mark.parametrize("ack, fresh, grid_w", [
    (False, True, 1020),  # no ack
    (True, False, 1020),  # no sample since the ack
    (True, True, -980),  # exporting: less discharge waits the full interval
])
def test_early_send_needs_ack_fresh_sample_and_an_import_reducing_move(ack, fresh, grid_w):
    reg = make(min_interval_s=3)
    _acked_dip(reg, ack, fresh, grid_w)
    assert reg.step(2.5, DISCHARGE) is None
    assert reg.step(3, DISCHARGE) is not None


def test_an_ack_counts_for_the_last_command_only():
    reg = make(min_interval_s=3)
    _acked_dip(reg)
    reg.step(2.0, DISCHARGE)  # sent, not acked yet
    reg.update_grid(3.5, 1520, 1480)
    assert reg.step(4.0, DISCHARGE) is None
    assert reg.step(5.0, DISCHARGE) is not None


def test_acked_moves_still_wait_for_the_write_threshold_and_keepalive():
    reg = make(min_interval_s=3, write_threshold_w=50, keepalive_s=40)
    _acked_dip(reg, grid_w=65)  # 25 W after the deadband
    for t in range(2, 40):
        reg.update_grid(t, 65, 500)
        assert reg.step(t, DISCHARGE) is None
    reg.update_grid(40, 65, 500)
    assert reg.step(40, DISCHARGE) is not None


def test_acked_release_still_holds_the_min_interval():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 1020, 500)
    reg.step(0, DISCHARGE)
    assert reg.step(0.5, R.Policy(R.RELEASE)) == R.Command("normal")
    reg.ack(2.3)
    reg.update_grid(2.4, 1020, 0)
    assert reg.step(2.5, DISCHARGE) is None
    assert reg.step(3.5, DISCHARGE) is not None


def test_acked_hold_stays_at_zero():
    reg = make(min_interval_s=3)
    reg.update_grid(0, 1020, 500)
    assert reg.step(0, HOLD) == R.Command("charge", 0, 100, 120)
    reg.ack(1.8)
    reg.update_grid(1.9, 1020, 0)
    assert reg.step(2.5, HOLD) is None


def _power_written(reg, ack=True, power_write=True, fresh=True, grid_w=520):
    """Discharging 500 W on target, 1480 W sent at 10 s and acked at 10.15 s, then at 11 s a sample with the
    battery there and the load moved again: by default 500 W more, a negative grid_w for less."""
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(10, 1020, 500)
    cmd = reg.step(10, DISCHARGE)
    if ack:
        reg.ack(10.15, power_write=power_write)
    reg.update_grid(11, grid_w, 1480, fresh=fresh)
    return cmd


def test_power_writes_are_off_by_default():
    reg = make(min_interval_s=3)
    assert not _power_written(reg).power_only
    assert reg.step(11, DISCHARGE) is None  # today's 2 s floor after an ack
    assert reg.step(12, DISCHARGE) == R.Command("discharge", 1960, 20, 120)


def test_a_power_change_in_the_running_block_may_go_as_one_write():
    reg = make(min_interval_s=3, power_write_gap_s=1)
    reg.update_grid(0, 20, 500)
    assert not reg.step(0, DISCHARGE).power_only  # the first command writes the block
    reg.update_grid(10, 1020, 500)
    assert reg.step(10, DISCHARGE) == R.Command("discharge", 1480, 20, 120, power_only=True)


def test_keepalive_kind_and_soc_changes_write_the_whole_block():
    reg = make(min_interval_s=3, power_write_gap_s=1, keepalive_s=40)
    reg.update_grid(0, 20, 500)
    reg.step(0, BOTH)
    reg.update_grid(40, 20, 500)
    assert not reg.step(40, BOTH).power_only  # keepalive renews the lease
    reg.update_grid(50, -2000, 500)
    assert reg.step(50, BOTH).kind == "charge"
    assert not reg.sent.power_only  # a new kind
    reg.update_grid(60, -1000, -1500)
    assert reg.step(60, replace(BOTH, charge_soc=90)).soc == 90
    assert not reg.sent.power_only  # a new SOC


def test_after_an_acked_power_write_any_move_follows_after_the_gap():
    reg = make(min_interval_s=3, power_write_gap_s=1)
    _power_written(reg, grid_w=-500)  # now exporting: a cut in discharge, not an import cut
    cmd = reg.step(11, DISCHARGE)
    assert (cmd.watts, cmd.power_only) == (980, True)


def test_the_power_write_gap_is_a_floor():
    reg = make(min_interval_s=3, power_write_gap_s=1.5)
    _power_written(reg)
    assert reg.step(11, DISCHARGE) is None
    assert reg.step(11.5, DISCHARGE) is not None


@pytest.mark.parametrize("ack, power_write, fresh", [
    (False, True, True),  # no ack
    (True, False, True),  # the firmware wrote the whole block, or is too old to say
    (True, True, False),  # no sample since the ack
])
def test_early_follow_up_needs_a_power_write_ack_and_a_fresh_sample(ack, power_write, fresh):
    reg = make(min_interval_s=3, power_write_gap_s=1)
    _power_written(reg, ack, power_write, fresh, grid_w=-500)
    assert reg.step(12, DISCHARGE) is None
    assert reg.step(13, DISCHARGE) is not None


def test_at_the_discharge_limit_it_holds_zero_instead_of_asking_for_discharge():
    # At its limit the battery stays at 0 W whatever it is sent, and a growing request sent a command every few seconds.
    reg = make()
    reg.update_grid(0, 20, 500, soc=20)
    hold = reg.step(0, DISCHARGE)
    assert hold == R.Command("charge", 0, 100, 120)
    sent = []
    for t in range(3, 120, 3):
        reg.update_grid(t, 300 + t, 0, soc=20)
        sent.append(reg.step(t, DISCHARGE))
    assert all(cmd is None or cmd == hold for cmd in sent)
    assert sum(cmd is not None for cmd in sent) <= 3  # keepalives only


@pytest.mark.parametrize("soc, policy", [(22, DISCHARGE), (20, replace(DISCHARGE, discharge_soc=15))])
def test_discharge_resumes_once_the_soc_rises_or_the_limit_drops(soc, policy):
    reg = make()
    reg.update_grid(0, 520, 0, soc=20)
    assert reg.step(0, DISCHARGE).watts == 0
    reg.update_grid(5, 520, 0, soc=soc)
    assert reg.step(5, policy).kind == "discharge"


def test_the_discharge_limit_does_not_stop_charging():
    reg = make()
    reg.update_grid(0, -1000, 0, soc=10)
    assert reg.step(0, BOTH) == R.Command("charge", 1000, 100, 120)
    reg = make()
    reg.update_grid(0, -1000, 0, soc=10)
    assert reg.step(0, CHARGE) == R.Command("charge", 1000, 100, 120)


def test_a_sample_without_soc_keeps_the_last_one():
    reg = make()
    reg.update_grid(0, 520, 0, soc=20)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 520, 0)
    assert reg.step(5, DISCHARGE) is None and reg.output_w == 0
    reg = make()
    reg.update_grid(0, 520, 0)  # older firmware: no SOC, regulates as before
    assert reg.step(0, DISCHARGE).kind == "discharge"


def test_keep_warm_is_off_by_default():
    reg = make()
    reg.update_grid(0, 20, 0, soc=50)  # on target, battery idle
    assert reg.step(0, BOTH) == R.Command("charge", 0, 100, 120)


def test_keep_warm_holds_a_small_discharge_instead_of_idling():
    reg = make(warm_w=80)
    reg.update_grid(0, 20, 0, soc=50)
    assert reg.step(0, BOTH) == R.Command("discharge", 80, 20, 120)


def test_keep_warm_gives_way_to_a_real_charge_or_discharge():
    reg = make(warm_w=80)
    reg.update_grid(0, -500, 0, soc=50)
    assert reg.step(0, BOTH) == R.Command("charge", 500, 100, 120)
    reg = make(warm_w=80)
    reg.update_grid(0, 520, 0, soc=50)
    assert reg.step(0, BOTH) == R.Command("discharge", 480, 20, 120)


def test_keep_warm_replaces_a_trickle_charge_after_the_dwell():
    reg = make(warm_w=80, flip_hysteresis_w=150, flip_dwell_s=5)
    reg.update_grid(0, -500, 0, soc=50)
    assert reg.step(0, BOTH).kind == "charge"
    reg.update_grid(3, 0, -40, soc=50)  # the surplus has shrunk to 40 W, still within the dwell
    assert reg.step(3, BOTH) == R.Command("charge", 40, 100, 120)
    reg.update_grid(6, 0, -40, soc=50)
    assert reg.step(6, BOTH) == R.Command("discharge", 80, 20, 120)
    reg.update_grid(9, -200, 80, soc=50)  # a surplus under the margin does not pull it back into a charge
    assert reg.step(9, BOTH) is None and reg.output_w == 80 and reg.limit == "warm"  # it goes out with the floor
    reg.update_grid(12, -500, 0, soc=50)  # one past the margin does
    cmd = reg.step(12, BOTH)
    assert cmd.kind == "charge" and cmd.watts >= 400


@pytest.mark.parametrize("soc, policy", [(20, BOTH), (50, CHARGE), (50, R.Policy(R.HOLD))])
def test_keep_warm_needs_discharge_to_be_allowed(soc, policy):
    reg = make(warm_w=80)
    reg.update_grid(0, 20, 0, soc=soc)
    assert reg.step(0, policy) == R.Command("charge", 0, 100, 120)


def test_keep_warm_does_not_wind_the_integral_into_a_charge():
    # The warm discharge exports a little all the time; integrating that would end in a grid charge.
    reg = make(warm_w=80, ki=0.01)
    sent = []
    for t in range(0, 900):
        reg.update_grid(t, 20 - 80, 80, soc=50)  # house on target, the 80 W warm discharge goes out
        sent.append(reg.step(t, BOTH))
    assert all(cmd.kind == "discharge" and cmd.watts == 80 for cmd in sent if cmd is not None)
    assert abs(reg.integral_w) < 50


def test_zero_watts_keeps_the_last_kind_so_no_block_write_is_needed():
    # 0 W as "charge" made every crossing between 0 W and a small discharge a kind flip: a full block write.
    reg = make(write_threshold_w=20, ki=0)
    reg.update_grid(0, 240, 0, soc=50)
    assert reg.step(0, BOTH) == R.Command("discharge", 200, 20, 120)
    reg.update_grid(3, 20, 200, soc=50)
    reg.step(3, BOTH)
    reg.update_grid(6, -200, 200, soc=50)  # load gone: want 0 W
    assert reg.step(6, BOTH) == R.Command("discharge", 0, 20, 120)
    reg.ack(6.1)
    reg.update_grid(16, 70, 0, soc=50)  # 50 W over target: a small discharge, same kind
    cmd = reg.step(16, BOTH)
    assert cmd.kind == "discharge" and cmd.watts == 30


def test_zero_watts_with_no_last_kind_is_a_charge():
    reg = make()
    reg.update_grid(0, 20, 0, soc=50)
    assert reg.step(0, BOTH) == R.Command("charge", 0, 100, 120)


def test_hold_zero_a_little_above_the_discharge_soc():
    # The inverter refuses about 1 % above its floor, so holding only at the policy SOC left the house on the grid.
    reg = make()
    reg.update_grid(0, 520, 0, soc=21.2)
    assert reg.step(0, DISCHARGE) == R.Command("charge", 0, 100, 120)
    reg = make()
    reg.update_grid(0, 520, 0, soc=21.6)
    assert reg.step(0, DISCHARGE).kind == "discharge"


def test_a_refused_discharge_is_held_at_zero():
    # Battery under 30 W for 20 s against a request of 100 W or more: the inverter is refusing, stop asking and
    # drop the integral, whatever the SOC says.
    reg = make()
    for t in range(0, 15, 3):
        reg.update_grid(t, 520, 0, soc=30)
        reg.step(t, DISCHARGE)
    assert reg.output_w == 480  # still waiting: a slow start looks like this for a while
    for t in range(15, 27, 3):
        reg.update_grid(t, 520, 0, soc=30)
        cmd = reg.step(t, DISCHARGE)
    assert cmd == R.Command("discharge", 0, 20, 120) or reg.output_w == 0
    assert reg.integral_w == 0
    reg.update_grid(30, 520, 0, soc=30)
    assert reg.step(30, DISCHARGE) is None or reg.output_w == 0
    reg.update_grid(90, 520, 400, soc=30)  # the hold is over and it delivers: regulate
    reg.step(90, DISCHARGE)
    assert reg.output_w > 200


def _refuse_near_the_floor(reg, policy=DISCHARGE, soc=22.0):
    """The battery stays at 0 W against a 500 W request for 21 s, 2 points over the policy SOC."""
    for t in range(0, 27, 3):
        reg.update_grid(t, 520, 0, soc=soc)
        reg.step(t, policy)


def test_a_refusal_near_the_floor_holds_zero_until_the_soc_rises():
    # On 19 Oct a SMILE-B3 refused at 12.8 % for 2 h while it was asked for discharge about every 40 s.
    # Near the floor a retry each minute only asks again; the SOC rising is the sign it may deliver.
    reg = make()
    _refuse_near_the_floor(reg)
    assert reg.output_w == 0 and reg.refused_soc == 22.0
    sent = []
    for t in range(24, 1800, 3):
        reg.update_grid(t, 520, 0, soc=22.9)
        sent.append(reg.step(t, DISCHARGE))
    assert all(cmd is None or cmd.watts == 0 for cmd in sent)
    assert sum(cmd is not None for cmd in sent) <= 1800 / 40 + 1  # keepalives only
    reg.update_grid(1800, 520, 0, soc=23.0)
    assert reg.step(1800, DISCHARGE).watts > 200 and reg.refused_soc is None


def test_a_refusal_near_the_floor_rearms_when_the_policy_soc_changes():
    reg = make()
    _refuse_near_the_floor(reg)
    reg.update_grid(90, 520, 0, soc=22.0)
    assert reg.step(90, replace(DISCHARGE, discharge_soc=15)).watts > 200


def test_a_refusal_near_the_floor_still_stores_a_real_surplus_but_does_not_flip_for_a_trim():
    reg = make()
    _refuse_near_the_floor(reg, policy=BOTH)
    reg.update_grid(30, -80, 0, soc=22.0)  # a trim's worth of export: no flip into a charge
    assert reg.step(30, BOTH) is None and reg.output_w == 0
    reg.update_grid(33, -600, 0, soc=22.0)
    cmd = reg.step(33, BOTH)
    assert cmd.kind == "charge" and cmd.watts >= 400


def test_a_refusal_far_from_the_floor_still_retries_after_the_hold():
    reg = make()
    _refuse_near_the_floor(reg, soc=40.0)
    assert reg.output_w == 0 and reg.refused_soc is None
    reg.update_grid(90, 520, 0, soc=40.0)
    reg.step(90, DISCHARGE)
    assert reg.output_w > 200


def test_a_normal_start_is_not_a_refusal():
    # Every start in the step logs since 17 Oct left the 10 W band within 5.1 s of the command.
    reg = make()
    for t in range(0, 6):
        reg.update_grid(t, 520, 0, soc=22.0)
        reg.step(t, DISCHARGE)
    reg.update_grid(6, 20, 480, soc=22.0)
    reg.step(6, DISCHARGE)
    assert reg.output_w > 200 and reg.refused_soc is None and reg.limit != "refused"


def _trimmed(reg, grid_w):
    """500 W discharge, power written and acked, then a sample 3 s later with the grid at grid_w."""
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(10, 1020, 500)
    reg.step(10, DISCHARGE)
    reg.ack(10.15, power_write=True)
    reg.update_grid(13, grid_w, 1480)


def test_a_trim_under_fifty_watts_waits_six_seconds():
    # On 21 Oct three commands in four were moves under 50 W, most a second or less after the last.
    reg = make(min_interval_s=2.5, power_write_gap_s=0.5, write_threshold_w=20, deadband_w=5)
    _trimmed(reg, grid_w=75)  # about 40 W over the 1500 W sent
    assert reg.step(13, DISCHARGE) is None
    reg.update_grid(15.9, 75, 1480)
    assert reg.step(15.9, DISCHARGE) is None
    reg.update_grid(16, 75, 1480)
    assert 1530 <= reg.step(16, DISCHARGE).watts <= 1550


def test_a_load_step_still_follows_a_power_write_at_the_gap():
    reg = make(min_interval_s=2.5, power_write_gap_s=0.5, write_threshold_w=20, deadband_w=5)
    _power_written(reg, grid_w=220)  # 200 W more, a sample 0.85 s after the ack
    assert reg.step(11, DISCHARGE).watts == 1680


def test_a_hand_back_forgets_a_half_counted_refusal():
    reg = make(stale_after_s=30)
    reg.update_grid(0, 520, 0, soc=30)
    reg.step(0, DISCHARGE)
    reg.update_grid(3, 520, 0, soc=30)  # the refusal clock starts
    reg.step(3, DISCHARGE)
    reg.step(40, DISCHARGE)  # no sample for 37 s: handed back
    reg.update_grid(41, 520, 0, soc=30)
    reg.step(41, DISCHARGE)
    assert reg.output_w == 480  # a fresh request, not a refusal carried over the gap


def test_integral_waits_while_the_inverter_is_still_arriving():
    reg = make(ki=0.1)
    reg.update_grid(0, 520, 0, soc=50)
    reg.step(0, DISCHARGE)  # asks 500 W
    for t in range(1, 6):
        reg.update_grid(t, 220, 300, soc=50)  # battery 200 W short of the output: still on its way
        reg.step(t, DISCHARGE)
    assert reg.integral_w == 0
    reg.update_grid(6, 60, 480, soc=50)  # arrived within 100 W: trim again
    reg.step(6, DISCHARGE)
    reg.update_grid(7, 60, 480, soc=50)
    reg.step(7, DISCHARGE)
    assert reg.integral_w > 0


def test_integral_resumes_when_the_inverter_clamps_below_the_request():
    reg = make(ki=0.1)
    reg.update_grid(0, 520, 0, soc=50)
    reg.step(0, DISCHARGE)  # asks 500 W
    for t in range(1, 15):
        reg.update_grid(t, 220, 300, soc=50)  # the inverter gives 300 W and no more
        reg.step(t, DISCHARGE)
    assert reg.integral_w == 0
    reg.update_grid(16, 220, 300, soc=50)  # 15 s after the command: it is not coming, trim what there is
    reg.step(16, DISCHARGE)
    assert reg.integral_w > 0


def test_keep_warm_only_replaces_requests_within_fifty_watts_of_idle():
    reg = make(warm_w=50)
    reg.update_grid(0, -80, 0, soc=50)  # an 80 W surplus is stored, not thrown away
    assert reg.step(0, BOTH) == R.Command("charge", 80, 100, 120)
    reg.update_grid(6, 20, -80, soc=50)  # settled, past the dwell: still stored
    reg.step(6, BOTH)
    assert reg.output_w == -80
    reg = make(warm_w=50)
    reg.update_grid(0, -40, 0, soc=50)  # a 40 W trickle gives way to the warm discharge after the dwell
    assert reg.step(0, BOTH) == R.Command("charge", 40, 100, 120)
    reg.update_grid(6, 20, -40, soc=50)
    assert reg.step(6, BOTH) == R.Command("discharge", 50, 20, 120)


def test_small_trims_do_not_hold_the_integral_off_for_good():
    # The battery reads 200 W over the command at high power. Every 50 W trim restarted the arrival clock, so the
    # integral never ran and the house exported the gap. Only a move bigger than the lag restarts it now.
    reg = make(ki=0.01)
    load = 3000.0
    sent_w = 0
    for t in range(0, 900):
        noise = 60 if t % 4 < 2 else -60
        battery = sent_w + 200 if sent_w else 0
        reg.update_grid(t, load + noise - battery, battery, soc=50)
        cmd = reg.step(t, BOTH)
        if cmd is not None:
            sent_w = cmd.signed_w
    late = []
    for t in range(900, 1000):
        noise = 60 if t % 4 < 2 else -60
        battery = sent_w + 200
        reg.update_grid(t, load + noise - battery, battery, soc=50)
        late.append(load - battery)
        cmd = reg.step(t, BOTH)
        if cmd is not None:
            sent_w = cmd.signed_w
    assert sum(late) / len(late) > -60
    assert reg.integral_w < -100


def test_a_big_move_still_waits_for_the_inverter_to_arrive():
    reg = make(ki=0.1)
    reg.update_grid(0, 20, 0, soc=50)
    reg.step(0, DISCHARGE)
    reg.update_grid(20, 1020, 0, soc=50)
    reg.step(20, DISCHARGE)  # asks about 980 W
    for t in range(21, 30):
        reg.update_grid(t, 520, 500, soc=50)  # halfway there
        reg.step(t, DISCHARGE)
    assert reg.integral_w == 0


def test_a_full_battery_is_not_asked_to_charge():
    reg = make(ki=0.1)
    reg.update_grid(0, -600, 0, soc=100)
    assert reg.step(0, CHARGE) == R.Command("charge", 0, 100, 120)
    assert reg.limit == "full"
    for t in range(1, 60):
        reg.update_grid(t, -600, 0, soc=100)
        reg.step(t, CHARGE)
    assert reg.integral_w == 0  # no windup to turn into an import at the next load
    reg.update_grid(60, -600, 0, soc=99)
    assert reg.step(60, CHARGE).watts > 500


def test_a_charge_past_full_keeps_asking_at_100():
    # A balance charge targets 101 % (register 251), so the BMS, not the inverter, ends it.
    reg = make()
    reg.update_grid(0, -600, 0, soc=100)
    cmd = reg.step(0, R.Policy(R.REGULATE, max_charge_w=5000, charge_soc=101))
    assert cmd.kind == "charge" and cmd.watts > 500 and cmd.soc == 101
    assert reg.limit != "full"


def test_limit_names_what_holds_the_output():
    reg = make()
    reg.update_grid(0, 20, 500, soc=50)
    reg.step(0, DISCHARGE)
    assert reg.limit == "none"
    reg.update_grid(5, 5000, 2000, soc=50)
    reg.step(5, DISCHARGE)
    assert reg.limit == "discharge_cap"
    reg = make()
    reg.update_grid(0, 520, 0, soc=21.2)
    reg.step(0, DISCHARGE)
    assert reg.limit == "floor"
    reg = make(flip_hysteresis_w=150)
    reg.update_grid(0, 520, 0, soc=50)
    reg.step(0, BOTH)
    reg.update_grid(10, -600, 500, soc=50)  # wants a 100 W charge: under the margin
    reg.step(10, BOTH)
    assert reg.limit == "turn_round"
    reg.step(10, R.Policy(R.RELEASE))
    assert reg.limit == "none"


class RampingInverter:
    """A discharge start rides a ceiling: 50 W plus 8 W/s from the last reading of 10 W or less. A step under the
    ceiling lands a second after the command and settles within 3 s; a charge is not limited."""

    def __init__(self):
        self.battery_w = 0.0
        self._aim = 0.0
        self._pending = []
        self._t_low = 0.0

    def command(self, t, watts):
        self._pending.append((t + 1.0, watts))

    def read(self, t):
        while self._pending and self._pending[0][0] <= t:
            self._aim = self._pending.pop(0)[1]
        aim = min(self._aim, 50 + 8 * (t - self._t_low)) if self._aim > 0 else self._aim
        self.battery_w += (aim - self.battery_w) * (1 - math.exp(-1 / 0.8))
        if self.battery_w <= 10:
            self._t_low = t
        return self.battery_w


# The live tuning, with the ready floor at 20 W.
LIVE = dict(target_w=-20, deadband_w=5, ki=0.01, write_threshold_w=20, min_interval_s=2.5, slew_w_per_s=2400,
            flip_hysteresis_w=150, flip_dwell_s=3, keepalive_s=40, power_write_gap_s=0.5, warm_w=20)


def on_inverter(reg, demand, policy, seconds, soc=50.0):
    """Runs the regulator against the ramping inverter, one sample a second. Returns (t, battery, command)."""
    inverter, rows = RampingInverter(), []
    for t in range(seconds):
        battery = inverter.read(t)
        reg.update_grid(t, demand(t) - battery, battery, soc=soc)
        cmd = reg.step(t, policy)
        if cmd is not None:
            inverter.command(t, cmd.signed_w)
            reg.ack(t + 0.3, power_write=cmd.power_only)
        rows.append((t, battery, cmd))
    return rows


@pytest.mark.parametrize("policy", [DISCHARGE, BOTH])
def test_the_ready_floor_keeps_a_slow_starting_inverter_ready_for_a_load_step(policy):
    # The house at dawn: a small surplus, under the margin, then a 1.5 kW load. Warm used to give way to the
    # surplus, the battery read 0 W, and the load step rode the ceiling up at 8 W/s.
    demand = lambda t: -80 + (-25, 0, 25)[t % 3] + (1500 if t >= 600 else 0)
    rows = on_inverter(make(**LIVE), demand, policy, 610)
    assert [t for t, battery, _ in rows if 10 <= t < 600 and battery <= 10] == []
    assert rows[604][1] >= 0.9 * (demand(604) - LIVE["target_w"])  # within 4 s of the step


@pytest.mark.parametrize("offset_comp", [0.0, 1.0])
@pytest.mark.parametrize("policy", [DISCHARGE, BOTH])
@pytest.mark.parametrize("seed", range(10))
def test_the_ready_floor_is_never_undercut_while_discharge_is_allowed(policy, seed, offset_comp):
    rnd = random.Random(seed)
    levels = [rnd.choice([-600, -300, -160, -120, -80, -40, -10, 0, 15, 40, 80, 300, 1500, 2500])
              for _ in range(40)]
    demand = lambda t: levels[t // 45] + rnd.randint(-30, 30)
    rows = on_inverter(make(**LIVE, offset_comp=offset_comp), demand, policy, 45 * 40)
    sent = [cmd for _, _, cmd in rows if cmd is not None]
    for last, cmd in zip([None] + sent, sent):
        if cmd.kind == "discharge":
            assert cmd.watts >= LIVE["warm_w"]
        elif policy is DISCHARGE:
            pytest.fail(f"{cmd} under a discharge only policy")
        elif cmd.watts == 0:  # the turn from a storable charge back to the floor passes 0 W
            assert last is not None and last.kind == "charge" and last.watts > 0


def test_the_ready_floor_goes_out_under_the_write_threshold():
    reg = make(warm_w=20, write_threshold_w=50)
    reg.update_grid(0, 540, 0, soc=30)
    assert reg.step(0, DISCHARGE).watts == 500
    reg.update_grid(5, 520, 500, soc=21.4)  # the floor hold: 0 W
    assert reg.step(5, DISCHARGE) == R.Command("discharge", 0, 20, 120)
    reg.update_grid(10, 20, 0, soc=23)  # discharge allowed again: the floor goes out, 20 W under the threshold
    assert reg.step(10, DISCHARGE) == R.Command("discharge", 20, 20, 120)


def test_the_slew_does_not_leave_the_output_between_a_charge_and_the_ready_floor():
    reg = make(warm_w=20, slew_w_per_s=400, min_interval_s=2.5, flip_dwell_s=3)
    reg.update_grid(0, -1200, 0, soc=50)
    assert reg.step(0, BOTH).watts == 1200
    reg.update_grid(10, 0, -1000, soc=50)
    assert reg.step(10, BOTH) == R.Command("charge", 1000, 100, 120)
    reg.update_grid(12.5, 1000, -1000, soc=50)  # the surplus is gone: the slew allows 1000 W of the turn
    assert reg.step(12.5, BOTH) == R.Command("discharge", 20, 20, 120)


def test_the_ready_floor_is_not_rounded_under():
    reg = make(warm_w=25)  # 25 W rounded half to even was 20 W
    reg.update_grid(0, 20, 0, soc=50)
    assert reg.step(0, DISCHARGE) == R.Command("discharge", 25, 20, 120)


def test_the_ready_floor_holds_through_a_surplus_it_cannot_store_and_one_under_the_margin():
    # The floor's watts go out on top of the surplus for as long as it lasts; a 0 W reading costs a slow start.
    reg = make(warm_w=20)
    reg.update_grid(0, -300, 0, soc=50)  # discharge only
    assert reg.step(0, DISCHARGE) == R.Command("discharge", 20, 20, 120)
    reg = make(warm_w=20)
    reg.update_grid(0, -300, 0, soc=100)  # full
    assert reg.step(0, BOTH) == R.Command("discharge", 20, 20, 120)
    reg = make(warm_w=20, flip_hysteresis_w=150, flip_dwell_s=3)
    reg.update_grid(0, 520, 0, soc=50)
    reg.step(0, BOTH)
    reg.update_grid(10, -620, 500, soc=50)  # 120 W of surplus after a discharge: under the margin
    assert reg.step(10, BOTH) == R.Command("discharge", 20, 20, 120)
    for t in range(11, 40):
        reg.update_grid(t, -140, 20, soc=50)
        assert reg.step(t, BOTH) is None or reg.sent.watts == 20
    assert reg.integral_w == 0  # the export the floor causes does not wind into a charge


def test_the_ready_floor_gives_way_to_a_surplus_the_battery_can_store():
    reg = make(warm_w=20, flip_hysteresis_w=150, flip_dwell_s=3)
    reg.update_grid(0, 520, 0, soc=50)
    reg.step(0, BOTH)
    reg.update_grid(10, -900, 500, soc=50)  # 400 W of surplus after a discharge: over the margin, stored
    assert reg.step(10, BOTH) == R.Command("charge", 400, 100, 120)
    reg.update_grid(20, 20, -80, soc=50)  # an 80 W charge, past the dwell: still stored, not padded
    reg.step(20, BOTH)
    assert reg.output_w == -80


def test_the_floor_hold_wins_over_the_ready_floor():
    reg = make(warm_w=20, write_threshold_w=20)
    reg.update_grid(0, 0, 20, soc=30)
    assert reg.step(0, DISCHARGE) == R.Command("discharge", 20, 20, 120)
    reg.update_grid(5, 0, 20, soc=21.4)  # within the margin of the policy SOC: the battery refuses anyway
    assert reg.step(5, DISCHARGE) == R.Command("discharge", 0, 20, 120)
    assert reg.limit == "floor"


def test_with_the_ready_floor_at_zero_the_commands_are_as_before():
    # The commands on this run since small trims wait the trim gap. A change here changes the controller with the setting off.
    rnd = random.Random(7)
    levels = [rnd.choice([-600, -160, -80, -40, 0, 40, 300, 1500]) for _ in range(40)]
    rows = on_inverter(make(**{**LIVE, "warm_w": 0}), lambda t: levels[t // 45] + rnd.randint(-30, 30), BOTH, 1800)
    sent = [(t, cmd.kind, cmd.watts, cmd.power_only) for t, _, cmd in rows if cmd is not None]
    assert (len(sent), hashlib.sha256(repr(sent).encode()).hexdigest()[:16]) == GOLDEN_FLOOR_OFF


GOLDEN_FLOOR_OFF = (265, "fba5a193a8a348a0")


# Read-only diagnosis: the values a status display needs to say why the output is what it is. They never feed back.

def test_the_proportional_term_is_kept_for_display():
    reg = make(kp=0.5, deadband_w=20)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, 1020, 500)
    reg.step(1, DISCHARGE)
    assert (reg.p_w, reg.request_w) == (490, 990)  # 0.5 x (1000 - 20 deadband), on top of 500 W


def test_the_caps_after_floor_and_full_are_kept_and_cleared_on_a_release():
    reg = make()
    reg.update_grid(0, 20, 500, soc=50)
    reg.step(0, DISCHARGE)
    assert (reg.lo_w, reg.hi_w) == (0, 3000)
    reg.update_grid(1, 20, 500, soc=21)  # within FLOOR_MARGIN of the policy SOC
    reg.step(1, DISCHARGE)
    assert (reg.lo_w, reg.hi_w, reg.limit) == (0, 0, "floor")
    reg.step(2, R.Policy(R.RELEASE))
    assert (reg.lo_w, reg.hi_w, reg.u_w) == (None, None, None)


def test_the_output_before_the_slew_and_whether_the_slew_cut_it_are_kept():
    reg = make(slew_w_per_s=100)
    reg.update_grid(0, 20, 0)
    reg.step(0, DISCHARGE)
    assert reg.slewed is False
    reg.update_grid(1, 1020, 0)
    reg.step(1, DISCHARGE)
    assert (reg.u_w, reg.output_w, reg.slewed) == (980, 100, True)


def test_the_direction_and_when_it_last_turned_are_readable():
    reg = make(flip_dwell_s=5)
    reg.update_grid(0, 20, 500)
    reg.step(0, BOTH)
    assert (reg.direction, reg.direction_since) == (1, None)  # a bumpless start sets no time
    reg.update_grid(5, -2000, 500)
    reg.step(5, BOTH)
    assert (reg.direction, reg.direction_since) == (-1, 5)


def test_the_refusal_clock_and_the_last_fresh_sample_are_readable():
    reg = make()
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    assert reg.refusal_since is None and reg.fresh_at == 0
    reg.update_grid(1, 520, 0)  # asked for 500 W, giving nothing
    reg.step(1, DISCHARGE)
    assert reg.refusal_since == 1
    reg.update_grid(2, 20, 500, fresh=False)
    reg.step(2, DISCHARGE)
    assert reg.refusal_since is None and reg.fresh_at == 1


# Inverter offset correction: the command asks for the setpoint that lands the battery on the output.

@pytest.mark.parametrize("w, offset", [(-3000, -8), (-100, -8), (0, 0), (100, -25), (1500, -25), (2000, 64.2),
                                       (2750, 198), (3000, 220), (4000, 220)])
def test_the_inverter_offset_is_read_off_the_measured_table(w, offset):
    assert R.inverter_offset(w) == pytest.approx(offset)


def test_offset_correction_is_off_by_default():
    reg = make(deadband_w=0, target_w=0)
    reg.update_grid(0, 2500, 0)
    assert reg.step(0, DISCHARGE).watts == 2500
    assert reg.offset_w == 0


@pytest.mark.parametrize("comp, watts, offset", [(1.0, 2370, -130.2), (0.5, 2430, -70.4)])
def test_offset_correction_asks_for_what_lands_the_battery_on_the_output(comp, watts, offset):
    reg = make(offset_comp=comp, deadband_w=0, target_w=0)
    reg.update_grid(0, 2500, 0)
    assert reg.step(0, DISCHARGE).watts == watts  # the table at the command, not at the output
    assert (reg.output_w, reg.offset_w) == (2500, pytest.approx(offset, abs=0.1))


@pytest.mark.parametrize("comp", [0.0, 0.5, 1.0])
def test_a_plant_on_the_table_lands_on_the_output(comp):
    # The inverter gives the command plus comp x the table at the command. Rounding the command to 10 W moves the
    # battery by up to 5 W times the table's steepest rise, 1.18.
    policy = R.Policy(R.REGULATE, max_charge_w=4000, max_discharge_w=4000)
    for output in range(-4000, 4001, 7):
        reg = make(offset_comp=comp, deadband_w=0, target_w=0)
        reg.update_grid(0, output, 0)
        signed = reg.step(0, policy).signed_w
        assert reg.output_w == output
        assert abs(signed + comp * R.inverter_offset(signed) - output) <= 5.9, output


def test_the_caps_stay_battery_watts_under_offset_correction():
    reg = make(offset_comp=1.0, deadband_w=0, target_w=0)
    reg.update_grid(0, 5000, 0)
    assert reg.step(0, DISCHARGE).watts == 2800  # 3000 W of battery
    assert (reg.output_w, reg.limit) == (3000, "discharge_cap")
    reg = make(offset_comp=1.0, deadband_w=0, target_w=0)
    reg.update_grid(0, 1000, 0)
    assert reg.step(0, R.Policy(R.REGULATE, max_discharge_w=1000)).watts == 1020  # a 1000 W cap reads 975 at 1000


def test_offset_correction_does_not_touch_hold_or_fixed():
    reg = make(offset_comp=1.0)
    reg.update_grid(0, 0, 0)
    assert reg.step(0, R.Policy(R.FIXED, fixed_w=3000)).watts == 3000
    assert reg.offset_w == 0


def test_under_offset_correction_the_integral_waits_for_the_battery_to_reach_the_output():
    reg = make(offset_comp=1.0, deadband_w=0, target_w=0, min_interval_s=0)
    reg.update_grid(0, 2500, 0)
    assert reg.step(0, DISCHARGE).watts == 2370
    reg.update_grid(1, 100, 2380)  # past the command but 120 W short of the 2500 W it should give
    reg.step(1, DISCHARGE)
    assert reg.integral_w == 0
    reg.update_grid(2, 100, 2450)
    assert reg.integral_w > 0


def test_under_offset_correction_the_slew_counts_from_the_battery_watts_asked_for():
    reg = make(offset_comp=1.0, deadband_w=0, target_w=0, slew_w_per_s=100, ki=0)
    reg.update_grid(0, 2500, 0)
    reg.step(0, DISCHARGE)
    reg.update_grid(1, 0, 2500)  # landed: the output holds, it is not slewed back towards the 2370 W command
    reg.step(1, DISCHARGE)
    assert (reg.output_w, reg.slewed) == (2500, False)


# Spike confirm: a big jump in demand waits one sample to show it lasts.

def test_spike_confirm_is_off_by_default():
    reg = make(min_interval_s=0)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 1220, 500)
    assert reg.step(5, DISCHARGE).watts == 1680


def test_a_jump_that_stays_goes_out_one_sample_late():
    reg = make(spike_confirm_w=800, min_interval_s=0)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 1220, 500)  # 1200 W more demand
    assert reg.step(5, DISCHARGE) is None and reg.step(5.5, DISCHARGE) is None
    assert (reg.output_w, reg.limit) == (500, "spike")
    reg.update_grid(6, 1220, 500)
    assert reg.step(6, DISCHARGE).watts == 1680


def test_a_spike_gone_by_the_next_sample_moves_nothing():
    reg = make(spike_confirm_w=800, min_interval_s=0, ki=0.05)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 1220, 500)
    assert reg.step(5, DISCHARGE) is None
    reg.update_grid(6, 20, 500)
    assert reg.step(6, DISCHARGE) is None
    assert reg.output_w == 500 and reg.integral_w == 0


def test_a_jump_under_the_spike_confirm_goes_out_at_once():
    reg = make(spike_confirm_w=800, min_interval_s=0)
    reg.update_grid(0, 20, 500)
    reg.step(0, DISCHARGE)
    reg.update_grid(5, 720, 500)
    assert reg.step(5, DISCHARGE).watts == 1180


@pytest.mark.parametrize("soc, policy", [(21.0, DISCHARGE), (50.0, R.Policy(R.REGULATE, max_charge_w=3000))])
def test_the_slew_never_holds_the_output_past_a_cap(soc, policy):
    # An acked power write lets the next command follow 0.5 s later, when the slew allows only 1000 W of the cut.
    reg = make(power_write_gap_s=0.5, slew_w_per_s=2000)
    reg.update_grid(0, 2040, 0, soc=50)
    assert reg.step(0, DISCHARGE).watts == 2000
    reg.ack(0.1, power_write=True)
    reg.update_grid(0.5, 20, 2000, soc=soc)  # at the floor, or discharge no longer allowed
    assert reg.step(0.5, policy).signed_w == 0
    assert reg.output_w == 0


# Feed forward guard: the solar lead never turns the battery round on its own.

def test_with_the_guard_feed_forward_never_turns_a_discharge_round_on_its_own():
    reg = make(kff=1.0, ff_guard=True, ki=0, deadband_w=0, target_w=0, min_interval_s=0, flip_dwell_s=0,
               flip_hysteresis_w=0)
    reg.update_grid(0, 0, 100, solar_w=1000)
    reg.step(0, BOTH)  # discharging 100 W
    reg.update_grid(1, 0, 100, solar_w=1300)  # solar +300 W in a second: the demand is still met
    reg.step(1, BOTH)
    assert reg.ff_w == 0  # -300 W of feed forward would have asked for a 200 W charge
    assert reg.output_w > 0


def test_with_the_guard_feed_forward_still_leads_a_ramp_in_the_running_direction():
    reg = make(kff=1.0, ff_guard=True, ki=0, deadband_w=0, target_w=0, min_interval_s=0)
    reg.update_grid(0, 0, 1000, solar_w=1000)
    reg.step(0, BOTH)
    reg.update_grid(1, 0, 1000, solar_w=1300)
    reg.step(1, BOTH)
    assert reg.ff_w == -300


def test_the_feed_forward_guard_is_off_by_default():
    assert R.Settings().ff_guard is False
    reg = make(kff=1.0, ki=0, deadband_w=0, target_w=0, min_interval_s=0, flip_dwell_s=0, flip_hysteresis_w=0)
    reg.update_grid(0, 0, 100, solar_w=1000)
    reg.step(0, BOTH)
    reg.update_grid(1, 0, 100, solar_w=1300)
    reg.step(1, BOTH)
    assert reg.ff_w == -300


# Integral reset: a big move starts the integral from 0.

def test_a_big_move_starts_the_integral_from_zero():
    reg = make(integral_reset_w=300, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0, write_threshold_w=10)
    reg.update_grid(0, 0, 500)
    reg.step(0, BOTH)
    for t in range(1, 30):  # the battery reads 40 W short: the integral winds
        reg.update_grid(t, 40, 460)
        reg.step(t, BOTH)
    assert reg.integral_w > 20
    reg.update_grid(30, 2000, 460)  # a kettle
    reg.step(30, BOTH)
    assert reg.integral_w == 0


def test_the_integral_reset_is_off_by_default():
    assert R.Settings().integral_reset_w == 0
    reg = make(ki=0.05, deadband_w=0, target_w=0, min_interval_s=0, write_threshold_w=10)
    reg.update_grid(0, 0, 500)
    reg.step(0, BOTH)
    for t in range(1, 30):
        reg.update_grid(t, 40, 460)
        reg.step(t, BOTH)
    reg.update_grid(30, 2000, 460)
    reg.step(30, BOTH)
    assert reg.integral_w > 20


# Import factor on the integral gain.

def test_the_integral_winds_faster_on_import_than_on_export():
    up, down = (make(ki=0.01, ki_import_x=3, deadband_w=0, target_w=0) for _ in range(2))
    for reg, g in ((up, 40), (down, -40)):
        reg.update_grid(0, g, 500)
        reg.step(0, BOTH)
        reg.update_grid(1, g, 500)
    assert abs(up.integral_w - 3 * -down.integral_w) < 1e-9 and up.integral_w > 0


def test_the_integral_import_factor_is_1_by_default():
    assert R.Settings().ki_import_x == 1


# Import trim wait: a small trim that cuts import may go sooner than one towards export.

def test_a_small_import_cut_goes_out_after_the_import_trim_gap():
    reg = make(import_trim_gap_s=2, ki=0, deadband_w=0, target_w=0, min_interval_s=0, write_threshold_w=20)
    reg.update_grid(0, 0, 500)
    reg.step(0, BOTH)
    reg.update_grid(2.5, 30, 500)  # 30 W of import: a small trim up
    assert reg.step(2.5, BOTH) is not None


def test_a_small_export_cut_still_waits_the_trim_gap():
    reg = make(import_trim_gap_s=2, ki=0, deadband_w=0, target_w=0, min_interval_s=0, write_threshold_w=20)
    reg.update_grid(0, 0, 500)
    reg.step(0, BOTH)
    reg.update_grid(2.5, -30, 500)
    assert reg.step(2.5, BOTH) is None


def test_the_import_trim_wait_is_the_trim_gap_by_default():
    assert R.Settings().import_trim_gap_s == R.TRIM_GAP_S


# Learned offset table, the long term trim: the integral is the short term trim.

def _settle(reg, short_w, t0=0, n=200, battery=500):
    """Hold a settled discharge where the battery reads short_w under the command."""
    reg.update_grid(t0, 0, battery, soc=50)
    reg.step(t0, BOTH)
    for t in range(t0 + 1, t0 + n):
        sent = reg.sent.signed_w
        reg.update_grid(t, battery - (sent - short_w), sent - short_w, soc=50)  # demand = battery
        reg.step(t, BOTH)
    return reg


def test_learning_moves_the_integral_into_the_table_and_keeps_the_command():
    reg = _settle(make(trim_learn=True, trim_rate=0.05, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0,
                       write_threshold_w=10), short_w=40)
    i = R.TRIM_NODES.index(500.0)
    assert reg.trim[i] < -25  # the table learned that the battery gives about 40 W less at 500 W
    assert abs(reg.integral_w) < 10  # and the integral gave it up
    assert abs(reg.sent.signed_w - 540) <= 10  # the command still asks for what the battery needs
    assert reg.trim_moves > 0


def test_learning_is_off_by_default_and_frozen_learning_still_applies_the_table():
    s = R.Settings()
    assert s.trim_learn is False and s.trim_rate == 0
    reg = _settle(make(ki=0.05, deadband_w=0, target_w=0, min_interval_s=0), short_w=40)
    assert not any(reg.trim)
    reg = _settle(make(trim_learn=True, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0), short_w=40)
    assert not any(reg.trim)  # the switch alone learns nothing at the default rate of 0
    reg = make(offset_comp=0)
    pts = reg.trim_points()
    for w in (250.0, 500.0, 750.0):
        pts[R.TRIM_NODES.index(w)][1] = -40.0
    assert reg.load_trim(pts)
    reg.update_grid(0, 0, 500, soc=50)
    assert reg.step(0, BOTH).watts == 540


def test_a_learned_table_past_its_bound_or_off_the_nodes_does_not_load():
    reg = make(trim_limit_w=150)
    pts = reg.trim_points()
    pts[3][1] = 151.0
    assert not reg.load_trim(pts)
    assert not reg.load_trim(pts[:-1])
    assert not reg.load_trim("junk")
    pts[3][1] = 0.0
    pts[R.TRIM_NODES.index(0.0)][1] = 5.0  # the 0 W node stays 0 so a stop stays a stop
    assert not reg.load_trim(pts)
    assert not any(reg.trim)


def test_learning_never_takes_a_node_past_its_bound():
    reg = _settle(make(trim_learn=True, trim_rate=0.2, trim_limit_w=30, ki=0.2, deadband_w=0, target_w=0,
                       min_interval_s=0, write_threshold_w=10), short_w=200, n=400)
    assert max(abs(y) for y in reg.trim) <= 30


def test_nothing_is_learned_at_a_cap_or_under_a_policy_offset():
    pol = replace(BOTH, max_discharge_w=400)
    reg = make(trim_learn=True, trim_rate=0.05, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0)
    reg.update_grid(0, 200, 400, soc=50)
    for t in range(1, 100):
        reg.update_grid(t, 200, 360, soc=50)
        reg.step(t, pol)
    assert not any(reg.trim)
    reg = make(trim_learn=True, trim_rate=0.05, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0)
    pol = replace(BOTH, target_offset_w=100)
    reg.update_grid(0, 100, 500, soc=50)
    reg.step(0, pol)
    for t in range(1, 200):
        sent = reg.sent.signed_w
        reg.update_grid(t, 100 + 40, sent - 40, soc=50)
        reg.step(t, pol)
    assert not any(reg.trim)


def test_forgetting_the_table_zeroes_it():
    reg = _settle(make(trim_learn=True, trim_rate=0.05, ki=0.05, deadband_w=0, target_w=0, min_interval_s=0,
                       write_threshold_w=10), short_w=40)
    reg.reset_trim()
    assert not any(reg.trim) and reg.table(500) == 0


# Edge cases.

def test_a_negative_cap_never_pushes_the_battery_the_other_way():
    # A -500 W charge cap made lo greater than hi, and _clip returns lo: a 500 W discharge at the floor.
    reg = make()
    policy = R.Policy(R.REGULATE, max_charge_w=-500, max_discharge_w=3000, discharge_soc=20)
    reg.update_grid(0, 0, 0, soc=21.0)  # inside FLOOR_MARGIN: no discharge allowed
    assert reg.step(0, policy).signed_w <= 0
    reg = make()
    policy = R.Policy(R.REGULATE, max_charge_w=3000, max_discharge_w=-500, charge_soc=100)
    reg.update_grid(0, 0, 0, soc=50)  # balanced: nothing to do
    assert reg.step(0, policy).watts == 0  # was a forced 500 W charge from the grid


def test_discharge_waits_for_a_known_soc_when_asked_to():
    # A restart whose first samples lack the SOC has no floor check: the soc guards skip the floor and full caps.
    reg = make(discharge_needs_soc=True)
    reg.update_grid(0, 1020, 0)  # no SOC yet
    cmd = reg.step(0, DISCHARGE)
    assert cmd.signed_w <= 0 and reg.limit == "soc_unknown"
    reg.update_grid(1, 1020, 0, soc=60)
    reg.step(4, DISCHARGE)
    assert reg.output_w > 900


def test_discharge_without_a_known_soc_goes_out_by_default():
    assert R.Settings().discharge_needs_soc is False
    reg = make()
    reg.update_grid(0, 1020, 0)
    assert reg.step(0, DISCHARGE).signed_w > 900
