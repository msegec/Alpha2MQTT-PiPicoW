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
    assert cmd == R.Command("charge", 0, 100, 120)


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


@pytest.mark.parametrize("soc, policy", [(21, DISCHARGE), (20, replace(DISCHARGE, discharge_soc=15))])
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
