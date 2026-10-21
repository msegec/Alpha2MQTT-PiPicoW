# Regulator core facts

The regulator turns each grid sample into a dispatch command (charge, discharge or normal) that holds the grid near a target, inside limits set by a policy.

Last verified: 851230d plus the loop version stamp, 2026-10-22. `python3 -m pytest "Home Assistant/tests/test_regulator.py" -q`: 169 passed in 0.66 s. `GOLDEN_FLOOR_OFF` unchanged with every new setting at its default.

Files: `Home Assistant/custom_components/alpha2mqtt_controller/regulator.py` (pure Python, no Home Assistant imports) and `Home Assistant/tests/test_regulator.py` (loads the module by path). The only caller is `controller.py`, which passes `time.monotonic()` seconds as `t`.

## Signs and units

- Watts everywhere, as floats. Times are seconds. SOC is percent.
- Grid power: positive is import, negative is export.
- Battery power: positive is discharge, negative is charge.
- `Command.signed_w`: `+watts` for discharge, `-watts` for charge and normal.
- `Policy.fixed_w`: positive is discharge. `Policy.target_offset_w`: positive draws more from the grid.
- `Settings.target_w`: the grid power to hold. The default 20 W is a little import.

## Public entry points

| Symbol | Inputs | Output / effect |
|---|---|---|
| `inverter_offset(w)` | signed setpoint W | Expected battery reading minus setpoint, linear between the `INVERTER_OFFSET` points, flat past the ends. |
| `estimate_grid(correction_w, battery_w, clamp_w=None, scale=None)` | watts | Grid estimate. Without `scale`: `correction_w - battery_w`, where correction is house demand net of solar. With `scale`: `scale * clamp_w + correction_w`. |
| `Policy` (frozen) | `mode`, `max_charge_w`, `max_discharge_w`, `charge_soc` (100), `discharge_soc` (10), `fixed_w`, `target_offset_w`, `reason` | What is allowed. The regulator only clamps to it. |
| `Settings` (mutable) | tuning, table below | The controller changes fields live with `setattr`. |
| `Command` (frozen) | `kind` ("charge", "discharge", "normal"), `watts`, `soc`, `duration`, `power_only` | What to publish. |
| `Regulator.update_grid(t, grid_w, battery_w, fresh=True, soc=None, solar_w=None)` | one sample | Runs the integral, stores the sample. Ignores the call if grid or battery is not finite. Keeps the last SOC when `soc` is None. Stores the solar change since the last call as the feed forward step: 0 unless both are known and finite and the change is over `SOLAR_DEADBAND_W`. `fresh=False` (only a correction changed) does not refresh the stale clock. |
| `Regulator.step(t, policy)` | policy | Returns a `Command` to publish now, or None. |
| `Regulator.ack(t, power_write=False)` | | The firmware wrote the last command. |
| `Regulator.fail()` | | The last command was not published or not written. |
| `Regulator.lease_active(t)` | | True while a non-normal command's `duration` has not run out. |

Readable fields for status: `status`, `limit`, `error_w` (raw, before the deadband), `request_w`, `output_w`, `integral_w`, `ff_w`, `sent`, `refused_soc`, `soc`, `grid_w`, `battery_w`, `solar_w` (the last sample's solar, None when unknown), `offset_w` (the offset correction this step: the command asks for `output_w + offset_w`; 0 outside `REGULATE` and after a hand back).

Diagnosis only, never read back by `step`: `p_w` (`kp * e`, 0 outside regulating), `lo_w` and `hi_w` (caps after floor, refusal and full; `fixed_w` under HOLD/FIXED), `u_w` (output after caps, turn round and ready floor, before the slew), `slewed` (the slew clip changed it). A hand back sets `lo_w`, `hi_w`, `u_w` to None, `p_w` to 0, `slewed` to False. Properties: `direction`, `direction_since` (`_t_direction`, None while it is still `-inf`), `refusal_since` (`_t_low`), `fresh_at` (`_t_fresh`). Adding them left `GOLDEN_FLOOR_OFF` unchanged.

## Modes

| Constant | Value | Effect |
|---|---|---|
| `RELEASE` | "release" | No dispatch, the inverter runs itself. |
| `HOLD` | "hold" | Dispatch at 0 W, always `Command("charge", 0, 100, duration_s)`. |
| `FIXED` | "fixed" | Dispatch `fixed_w`, a manual override. No turn round dwell. |
| `REGULATE` | "regulate" | Grid following. |

## Status states and transitions

`status` starts as "waiting". Each `step` sets it from the policy and input freshness.

| Condition in `step` | New status | Sends |
|---|---|---|
| `RELEASE`, or `FIXED` with non-finite `fixed_w` | "released" or "released: <reason>" | `normal` if a lease is active, or a failed normal is due for retry, or nothing was ever sent (a crash may have left a lease). Else None. |
| `HOLD` or `FIXED` | "hold" / "fixed" | Fixed watts. Needs no reading, so it works with stale inputs. |
| `REGULATE`, no fresh sample within `stale_after_s` (or none yet) | "stale" | `normal` if a lease is active or a failed normal is due. Never on the first stale start. |
| `REGULATE` with a live sample | "regulating" | Per the decision order below. |

Every non-regulating branch calls `_restart`: integral to 0, refusal clock and `refused_soc` cleared, smoothing filter and saturation cleared, `_regulating` False. The next regulating step starts bumpless from the measured battery power.

## Direction state (turn round)

`_direction` is -1 (charge), 0, or +1 (discharge), with `_t_direction` the time it last changed.

- Bumpless start sets `_direction = sign(battery_w)`, without touching `_t_direction`.
- In `REGULATE`, a change of sign is blocked (output 0, `limit = "turn_round"`) while `|u| < flip_hysteresis_w` or less than `flip_dwell_s` has passed since the last change. From direction 0 any sign is taken at once.
- `FIXED` flips at once.

## Refusal state

| State | Meaning | Exit |
|---|---|---|
| Armed | `_t_low` None, `refused_soc` None | Last sent is a discharge of `REFUSED_ASK_W` or more and battery reads under `REFUSED_W`: start `_t_low`. |
| Counting | `_t_low` set | Battery reaches `REFUSED_W` or the ask drops: back to armed. `REFUSED_AFTER_S` elapsed: refused. |
| Timed hold | `_t_refused` set, SOC more than `REFUSED_NEAR_SOC` above `discharge_soc` | Discharge cap 0 W (`limit = "refused"`) for `REFUSED_HOLD_S`, then retry. |
| Latched near floor | `refused_soc` set | Discharge cap 0 W until SOC >= `refused_soc + REARM_SOC` or `policy.discharge_soc` changes. Then rearm. |

On refusal the integral is set to 0. `_refused` is not evaluated while the SOC floor hold is on (it is an `elif`).

## Constants

| Name | Value | Guards |
|---|---|---|
| `MIN_SEND_GAP_S` | 2.0 s | Earliest early send after a block write ack. |
| `MAX_INTEGRATE_S` | 10.0 s | Cap on `dt` per integration, so a sample gap does not wind the integral. |
| `FLOOR_MARGIN` | 1.5 % | Discharge cap is 0 W when SOC <= `discharge_soc + 1.5`. The inverter cuts about 1 % above its floor. |
| `LAG_W` | 100 W | Battery this far short of the last setpoint has not arrived: integral waits. |
| `ARRIVE_S` | 15 s | Arrival wait ends after this, for an inverter that clamps below the request. |
| `REFUSED_W` | 30 W | Battery under this counts as giving nothing. |
| `REFUSED_AFTER_S` | 20 s | How long under `REFUSED_W` before a refusal. Starts left 10 W within 5.1 s in logs. |
| `REFUSED_HOLD_S` | 60 s | Timed hold after a refusal far from the floor. |
| `REFUSED_ASK_W` | 100 W | Minimum discharge ask that can be refused. Battery reads about 35 W under a 100 to 400 W command. |
| `REFUSED_NEAR_SOC` | 3.0 % | Refusal within this of `discharge_soc` latches. |
| `REARM_SOC` | 1.0 % | SOC rise that ends a latched refusal. |
| `SMALL_TRIM_W` | 50 W | Moves under this are trims. |
| `TRIM_GAP_S` | 6.0 s | Trims wait this long since the last command. |
| `WARM_BAND_W` | 50 W | Ready floor replaces only requests above -50 W (trickle charge, idle, small discharge). |
| `INVERTER_OFFSET` | (-100, -8), (0, 0), (100, -25), (1500, -25), (2500, 210) | Battery reading minus setpoint by signed setpoint, measured 20 to 22 Oct: charge about -8 W, discharge 100 to 1500 W about -25 W, +198 W at 2750 W, +220 W at 3000 W. |
| `SOLAR_DEADBAND_W` | 20 W | A solar change this small between samples gets no feed forward, so night noise stays out. |

## Settings defaults

| Field | Default | Guards |
|---|---|---|
| `target_w` | 20 W | Grid setpoint. |
| `kp` | 1.0 | Share of grid error corrected at once. |
| `ki` | 0.01 /s | Integral gain, trims the inverter's own error. |
| `integral_band_w` | 300 W | Integrate only when deadbanded error is within this. Bigger is a load step. |
| `integral_limit_w` | 300 W | Integral clamp, both signs. |
| `kff` | 0.0 | Solar feed forward gain: `ff_w = -kff * (solar change since the last sample)`, held for the steps on that sample only. |
| `deadband_w` | 20 W | Subtracted from the error magnitude. |
| `smoothing_s` | 0.0 s | Low pass on the request. 0 is off. |
| `slew_w_per_s` | 2000 W/s | Max move from the last sent command, times the time since it was sent. |
| `write_threshold_w` | 50 W | Minimum move to send, outside the other send reasons. |
| `min_interval_s` | 3.0 s | Minimum gap between commands, unless an early send applies. |
| `keepalive_s` | 40 s | Resend at least this often. |
| `duration_s` | 120 s | Dispatch lease in every command. |
| `flip_hysteresis_w` | 150 W | Margin to turn charge/discharge round. |
| `flip_dwell_s` | 5.0 s | Minimum time in one direction before turning. |
| `stale_after_s` | 30 s | No fresh sample this long: hand back. |
| `power_write_gap_s` | 0.0 s | 0 means every command is a full block write. Above 0: same-block changes go `power_only`, and after a power write ack the next may follow after this gap. |
| `warm_w` | 0.0 W | Ready floor ("Keep warm"). 0 is off. |
| `spike_confirm_w` | 0.0 W | Above 0, a sample whose net demand (`grid_w + battery_w`) moved more than this from the last sample is held: every step on it returns None with `limit = "spike"`, and the integral skips it. The next sample always acts. 0 is off. |
| `offset_comp` | 0.0 | Share of `inverter_offset(output_w)` taken off the command under `REGULATE`, so the battery lands on the output. 0 is off, 1 full. |

The test file's `LIVE` dict, labelled "the live tuning": `target_w=-20, deadband_w=5, ki=0.01, write_threshold_w=20, min_interval_s=2.5, slew_w_per_s=2400, flip_hysteresis_w=150, flip_dwell_s=3, keepalive_s=40, power_write_gap_s=0.5, warm_w=20`. Whether it matches the deployed config: unknown.

## Integration (in `update_grid`, before storing the sample)

The integral runs only if all hold:

1. The previous step regulated (`_regulating`) and there was an earlier sample.
2. Deadbanded error `|e| <= integral_band_w`. `e` uses the offset from the last regulating step.
3. Arrived: last sent is None or normal, or the battery is no more than `LAG_W` short, in its own direction, of `_sent_w` (the battery watts the last command should deliver: its `signed_w` less the offset correction it went out with), or `ARRIVE_S` has passed since it was sent. A battery past its setpoint counts as arrived.
4. `sign(e) != _saturation`: conditional integration, frozen when the output is pinned in the error's direction.
5. Not a held spike (`_spike`, see `spike_confirm_w`).

Then `integral += ki * e * min(dt, MAX_INTEGRATE_S)`, clipped to `integral_limit_w`.

## Decision order in `step` (regulating path)

1. `limit = "none"`. A `FIXED` policy with non-finite watts becomes `RELEASE`.
2. `RELEASE`: hand back, return.
3. `HOLD`/`FIXED`: restart, `request = lo = hi = fixed_w`, skip to 9.
4. Not live: hand back as "stale", return.
5. Bumpless start if the last step did not regulate: `output_w = battery_w`, `_direction = sign(battery_w)`. Then a held spike returns None with `limit = "spike"`; output, sent and the diagnosis fields keep their last values.
6. `request = battery_w + kp * e + integral_w + ff_w`, then optional smoothing. `ff_w` is `-kff` times the solar step stored by `update_grid`; P already sends the whole measured demand change, so feed forward only leads a solar ramp.
7. Caps: `lo = -max_charge_w`, `hi = max_discharge_w`. SOC <= `discharge_soc + FLOOR_MARGIN`: `hi = 0` ("floor"). Else refused: `hi = 0` ("refused"). SOC >= `charge_soc`: `lo = 0` ("full"). `limit` names the cap the request crosses ("discharge_cap" / "charge_cap" by default).
8. `u = clip(request, lo, hi)`, `_saturation = sign(request - u)`.
9. Turn round check (above).
10. Ready floor: `ready_w = warm_w` if `REGULATE` and `0 < warm_w <= hi`. If `-WARM_BAND_W < u < ready_w` and (direction >= 0 or the dwell has passed): `u = ready_w`, `limit = "warm"`, saturation recomputed, direction forced to +1.
11. Slew: `output_w = clip(u, _sent_w +/- slew_w_per_s * (t - t_sent))`. No limit before the first send.
12. If the ready floor applies and `u >= ready_w` and output > -50 W: `output_w = max(output_w, ready_w)`.
13. Offset correction: under `REGULATE`, `offset_w = -offset_comp * inverter_offset(output_w)`, else 0. Everything before this is battery watts: caps, turn round, ready floor and slew all mean what the battery gives. Only the command is moved.
14. `_command` on `ask = output_w + offset_w`: watts rounded to 10 W. Under `REGULATE`, an output at or over `warm_w` is lifted to `ceil(warm_w)`. `HOLD` is `charge 0 W SOC 100`. 0 W keeps the last kind and SOC, or `charge 0 W SOC 100` if there is none. Positive is discharge with `discharge_soc`, else charge with `charge_soc`.
15. `_maybe_send`, in order (it compares commands, so its thresholds are command watts):
    1. Within `min_interval_s` and no early send: None.
    2. No last command, last failed, lease over, or keepalive due: send full.
    3. Kind changed and either side is non-zero: send full.
    4. `to_floor`: `REGULATE`, floor > 0, last signed under `ceil(warm_w)` and new at or over it.
    5. Small trim: not `to_floor`, non-zero watts, same SOC, move under `SMALL_TRIM_W` and under `TRIM_GAP_S` since last: None.
    6. `to_floor`, SOC changed, or move >= `write_threshold_w`: send, as `power_only` if same kind/SOC/duration and `power_write_gap_s > 0`.
    7. Else None.

Early send (`_may_send_early`) needs: last not normal, acked, and a fresh sample after the ack. After a power write ack with `power_write_gap_s > 0`: any move after that gap. Otherwise only an import cut (`signed_w` rises) after `MIN_SEND_GAP_S`.

## Named behaviours and why

| Behaviour | Commit | Why |
|---|---|---|
| Battery + kp x error, no D term | 3ba305d | The plant is dead time with quantised readings. The battery read with the grid makes the step land on demand whatever is in flight. |
| Slew from last sent command | 2076fcb | A per-step slew split one load step across two commands. |
| Target offset | 8feffdc | Policy can top up from the grid or leave solar while charging. |
| 5 s dwell, 150 W margin | 4aa846d | A 60 s dwell held the battery at 0 W through load edges. |
| Hand back on first stale step, retry failed publish, min interval across release | e0e291d | Fail safe: stale watts could run for a full lease. |
| Early send of an import cut after ack | 754c81d | A cloud dip cost grid import while waiting the min interval. |
| Power writes, `fail()` | a3a20a7 | Follow a power write sooner; a refused write is resent as a full block. |
| `MAX_INTEGRATE_S`, hold through short gaps | 70fe0e2 | Do not wind on an error nobody measured. |
| No import gain | 9630537 | Gain above 1 swung the grid more on live noise. |
| Floor hold at discharge SOC | 7603b02 | A growing request against a battery at its limit sent a command every few seconds. |
| Power write hold reverted | b47c99a, 9490f53 | Block writes started a discharge no faster; the hold cost response. |
| Keep warm | f333100 | Discharge starts from 0 W ramp at about 10 W/s on this inverter. |
| 0 W keeps last kind; `FLOOR_MARGIN`; timed refusal; arrival gate; `WARM_BAND_W` | 15aaddd | Kind flips cost block writes; the inverter cuts 1 % above floor; trimming the start lag wound a charge up; a 150 W warm band threw away storable surplus. |
| Arrival gate only when short; full stops charge; release at start; `limit` field | 42e3727 | A battery reading 200 W over setpoint never "arrived", so the house exported the gap. A full battery wound the integral. |
| No refactor change | 42a9c10, 0af19ef | Derived fields; one `_hand_back`. Fuzzed 3000 seeds, no change. |
| Release `reason` | 8259b36 | An unknown policy state released silently. |
| Warm only within 50 W of idle, also under turn round | 9ef2224 | Warm pinned on top of a held surplus exported about 130 W at crossovers. |
| Ready floor | 9684b66 | Near 0 W the discharge ceiling restarts low; never idle while discharge is allowed. A surplus that cannot be stored holds the floor and exports a little. |
| Offset correction, `_sent_w` | 2026-10-22 | The integral alone held a level dependent inverter offset: after a 2 kW load it wound to about -155 W and gave about +175 W of import back when the load stopped. |
| Latched refusal near floor; small trims wait | 75e08a8 | An inverter at cutoff was asked again for 2 h. Trim gating cut commands from 810 to 531 an hour for 0.6 W more mean import. |

## Invariants the tests pin

| Test (prefix) | Proves |
|---|---|
| `test_bumpless_start_*`, `test_load_step_is_corrected_*`, `test_load_step_goes_out_in_one_command` | Start from measured battery; a load step goes out whole in one command. |
| `test_output_clamped_*`, `test_slew_*`, `test_min_interval_*`, `test_small_changes_wait_for_keepalive` | Caps, slew from last sent, min interval, write threshold vs keepalive. |
| `test_no_flapping_*`, `test_default_dwell_*`, `test_flip_needs_margin_*` | Turn round margin and dwell. |
| `test_stale_*`, `test_correction_updates_*`, `test_release_*`, `test_lease_expiry_*`, `test_a_release_at_start_*` | Hand back rules and lease logic. |
| `test_a_failed_*`, `test_min_interval_holds_across_a_release` | Retry and anti-hammer. |
| `test_acked_*`, `test_early_send_*`, `test_an_ack_counts_*`, `test_power_*`, `test_a_power_change_*`, `test_keepalive_kind_and_soc_*`, `test_after_an_acked_power_write_*`, `test_early_follow_up_*` | Early send and power-only write rules. |
| `test_integral_*`, `test_a_gap_in_samples_*`, `test_closed_loop_settles_*`, `test_small_trims_do_not_hold_*`, `test_a_big_move_still_waits_*` | Integral bounds, freeze, arrival gate. |
| `test_at_the_discharge_limit_*`, `test_discharge_resumes_*`, `test_hold_zero_a_little_above_*`, `test_a_full_battery_*` | SOC floor and full holds. |
| `test_a_refus*`, `test_a_normal_start_is_not_a_refusal`, `test_a_hand_back_forgets_*` | Refusal state machine. |
| `test_keep_warm_*`, `test_the_ready_floor_*`, `test_the_floor_hold_wins_*`, `test_the_slew_does_not_leave_*` | Ready floor. Seeded fuzz: no discharge under `warm_w`, no charge under a discharge-only policy. |
| `test_a_trim_under_fifty_watts_*`, `test_a_load_step_still_follows_*` | Trim gap vs load steps. |
| `test_the_proportional_term_*`, `test_the_caps_after_*`, `test_the_output_before_the_slew_*`, `test_the_direction_and_*`, `test_the_refusal_clock_*` | Diagnosis fields and properties. |
| `test_the_inverter_offset_*`, `test_offset_correction_*`, `test_the_caps_stay_battery_watts_*`, `test_under_offset_correction_*` | Table, off by default, caps in battery watts, no change to HOLD/FIXED, arrival gate and slew against `_sent_w`. The ready floor fuzz also runs with `offset_comp=1`. |
| `test_spike_confirm_*`, `test_a_jump_*`, `test_a_spike_gone_*` | Off by default; a jump over the setting waits one sample, a lasting one goes out then, a gone one moves nothing and winds no integral. |
| `test_with_the_ready_floor_at_zero_*` | Golden hash `GOLDEN_FLOOR_OFF = (265, "fba5a193a8a348a0")` of the full command list with `warm_w=0` against `RampingInverter`. Any behaviour change with the floor off breaks it. |

## Gotchas

- `HOLD` always sends `charge 0 W SOC 100`, even when the running block is a discharge. A 0 W regulate output keeps the last kind instead.
- Python `round` is half to even: 25 W rounds to 20 W. Hence `ceil(warm_w)` in `_command`.
- Rounding to 10 W eats the offset correction under about 200 W of discharge, where it is 25 W or less.
- With `offset_comp` 1, between 1500 and 2500 W a move of the output moves the command about 0.77 times as far, so the write threshold and trim gate need a slightly larger output move there. The command stays monotonic in the output for `offset_comp` up to the number's 1.5.
- At default `write_threshold_w = 50 = SMALL_TRIM_W` the trim gate changes nothing. It only bites with a lower threshold, as in `LIVE`.
- A stop at 0 W skips the trim gate but still needs the write threshold, a kind change, or keepalive.
- Release and stale hand backs call `_send` directly and skip `min_interval_s`. The next command after them still waits it.
- `_saturation` is set before the turn round check, so a turn round hold does not freeze the integral. The ready floor recomputes it.
- `_restart` clears `refused_soc` and `_t_low` but not `_t_refused`. A timed refusal hold survives a short HOLD, FIXED or stale spell. Any HOLD/FIXED step ends a latched near-floor refusal.
- A mode string other than the four constants takes the regulating branch with `_regulating` False: no integral, bumpless start every step. The controller maps unknown states to `RELEASE` first.
- `error_w` in HOLD/FIXED is `grid_w - target_w`, without the offset.
- `fresh=False` samples still update `grid_w`, `battery_w` and the integral.
- Bumpless start does not reset `_t_direction`, so on a new regulator the dwell has already passed.
- The comment in `test_a_refused_discharge_is_held_at_zero` says "over 200 W"; the code uses `REFUSED_ASK_W = 100` since 75e08a8.
- `MIN_SEND_GAP_S` comment cites the five-write firmware (about 1.8 s). The one-frame write (9452d4a) is about 50 ms; the constant was not changed.

## Open questions

- Whether the turn round hold leaving the integral running is intended: unknown.
- Whether `_t_refused` surviving `_restart` is intended: unknown.
- The deployed `Settings` values: unknown from this component. `LIVE` in the tests is the only record read.
- Whether `MIN_SEND_GAP_S` should drop with one-frame block writes: unknown.
