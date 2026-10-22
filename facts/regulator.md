# Regulator core facts

The regulator turns each grid sample into a dispatch command (charge, discharge or normal) that holds the grid near a target, inside limits set by a policy.

Last verified: d80bfb1 plus the regulator fixes up to the ready floor command, 2026-10-22. `python3 -m pytest "Home Assistant/tests"`: 327 passed; `tools/bench/tests`: 28 passed; `GOLDEN_FLOOR_OFF` unchanged.

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
| `inverter_offset(w)` | signed setpoint W | Expected battery reading minus setpoint, linear between the `INVERTER_OFFSET` points, flat past the ends: `_interp` over its columns, the helper the learned table uses. `_setpoint` merges both tables' nodes; today every `INVERTER_OFFSET` point is a `TRIM_NODES` node, and the merge keeps the solve right if that changes. |
| `estimate_grid(correction_w, battery_w, clamp_w=None, scale=None)` | watts | Grid estimate. Without `scale`: `correction_w - battery_w`, where correction is house demand net of solar. With `scale`: `scale * clamp_w + correction_w`. |
| `Policy` (frozen) | `mode`, `max_charge_w`, `max_discharge_w`, `charge_soc` (100), `discharge_soc` (10), `fixed_w`, `target_offset_w`, `reason` | What is allowed. The regulator only clamps to it. |
| `Settings` (mutable) | tuning, table below | The controller changes fields live with `setattr`. |
| `Command` (frozen) | `kind` ("charge", "discharge", "normal"), `watts`, `soc`, `duration`, `power_only` | What to publish. |
| `Regulator.update_grid(t, grid_w, battery_w, fresh=True, soc=None, solar_w=None)` | one sample | Runs the integral, stores the sample. Ignores the call if grid or battery is not finite. Keeps the last SOC when `soc` is None. Stores the solar change since the last fresh sample as the feed forward step: 0 unless both are known and finite, that sample is at most `solar_max_gap_s` old and the change is over `solar_deadband_w`, and, with `solar_ff_min_w` above 0, both readings are at least that. `fresh=False` (only a correction changed) does not refresh the stale clock, and keeps the solar step, `solar_w`, the spike flag and the demand it compares: it is the same sample, so it cannot start, confirm or end a spike, nor feed forward a solar change. |
| `Regulator.step(t, policy)` | policy | Returns a `Command` to publish now, or None. |
| `Regulator.ack(t, power_write=False)` | | The firmware wrote the last command. |
| `Regulator.fail()` | | The last command was not published or not written. |
| `Regulator.lease_active(t)` | | True while a non-normal command's `duration` has not run out. |
| `Regulator.table(w)` | signed setpoint W | Expected battery reading minus setpoint: `offset_comp * inverter_offset(w)` plus the learned trim at `w`. |
| `Regulator.trim_points()` | | The learned table as `[[node W, trim W], ...]` over `TRIM_NODES`, trims rounded to 0.1 W. |
| `Regulator.load_trim(points)` | pairs | Restores a saved table. False and no change for a malformed table, other nodes, a non-finite value, a non-zero 0 W node, or neighbours (after the clip) further apart than `TRIM_SLOPE` times their spacing plus `TRIM_ROUND_W`. A value past `trim_limit_w` loads clipped to it. Rejected, not repaired: `_teach` never builds a steeper table, so it is not a learned one, and any repair would invent a table nobody measured. |
| `Regulator.reset_trim()` | | Zeroes the learned table. |

Readable fields for status: `trim` (learned W per `TRIM_NODES` node), `trim_moves` (count of learning moves, for the controller to notice a change), `status`, `limit`, `error_w` (raw, before the deadband: `grid_w - target_w - target_offset_w` in every mode, 0 under HOLD/FIXED before any reading), `request_w`, `output_w`, `integral_w`, `ff_w`, `sent`, `refused_soc`, `soc`, `grid_w`, `battery_w`, `solar_w` (the last sample's solar, None when unknown), `offset_w` (the offset correction this step: the command asks for `output_w + offset_w`; 0 outside `REGULATE` and after a hand back).

Diagnosis only, never read back by `step`: `p_w` (`kp * e`, 0 outside regulating), `lo_w` and `hi_w` (caps after floor, refusal and full; `fixed_w` under FIXED, 0 under HOLD), `u_w` (output after caps, turn round and ready floor, before the slew), `slewed` (the slew clip changed it). A hand back sets `lo_w`, `hi_w`, `u_w` to None, `p_w` to 0, `slewed` to False. Properties: `direction`, `direction_since` (`_t_direction`, None while it is still `-inf`), `refusal_since` (`_t_low`), `fresh_at` (`_t_fresh`). Adding them left `GOLDEN_FLOOR_OFF` unchanged.

## Modes

| Constant | Value | Effect |
|---|---|---|
| `RELEASE` | "release" | No dispatch, the inverter runs itself. |
| `HOLD` | "hold" | Dispatch at 0 W, always `Command("charge", 0, 100, duration_s)`. `fixed_w` is ignored: request, output and direction are 0 W too. |
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
| Armed | `_t_low` None, `refused_soc` None | Last sent is a discharge of `refused_ask_w` or more and battery reads under `refused_w`: start `_t_low`. |
| Counting | `_t_low` set | Battery reaches `refused_w` or the ask drops: back to armed. `refused_after_s` elapsed: refused. |
| Timed hold | `_t_refused` set, SOC more than `refused_near_soc` above `discharge_soc` | Discharge cap 0 W (`limit = "refused"`) for `refused_hold_s`, then retry. |
| Latched near floor | `refused_soc` set | Discharge cap 0 W until SOC >= `refused_soc + rearm_soc` or `policy.discharge_soc` changes. Then rearm. |

On refusal the integral is set to 0. `_refused` is not evaluated while the SOC floor hold is on (it is an `elif`).

## Constants

| Name | Value | Guards |
|---|---|---|
| `INVERTER_OFFSET` | (-100, -8), (0, 0), (100, -25), (1500, -25), (2750, 198), (3000, 220) | Battery reading minus setpoint, indexed by the signed setpoint. Measured 20 to 22 Oct: charge about -8 W, discharge 100 to 1500 W about -25 W, +198 W at 2750 W, +220 W at 3000 W. Chosen: 0 at 0 W, the ramps either side of it, the line from 1500 to 2750 W, flat past the ends. |
| `TRIM_NODES` | -4000, -3500, -3000, -2500, -2000, -1500, -1000, -750, -500, -250, -100, 0, 100, 250, 500, 750, 1000, 1500, 2000, 2500, 2750, 3000, 3500, 4000 W | Nodes of the learned table, linear between, flat past the ends. They index the saved table, so changing them discards it. The 0 W node stays 0. |
| `TRIM_SLOPE` | 0.5 | Neighbouring learned nodes differ by at most this times their spacing, so the table stays monotonic and each output has one command. A maths bound, not a tuning choice. `_teach` keeps it; `load_trim` rejects a table that breaks it. |
| `LEARN_SOC_MARGIN` | 1.0 % | Learning needs the SOC this far over the floor hold, so samples just before the hold teach nothing. Set with the table (9381e49); no measurement behind it. |
| `TRIM_ROUND_W` | 0.1 W | Slack on the slope check in `load_trim`: `trim_points` rounds each node to 0.1 W, so a stored pair at the bound may be this far past it. |

Kept as code on purpose: the tables above, the `abs(u) >= 1` direction test and the protocol values. The rest of the
former constants are `Settings` fields since this round.

## Settings defaults

| Field | Default | Guards |
|---|---|---|
| `target_w` | 20 W | Grid setpoint. |
| `kp` | 1.0 | Share of grid error corrected at once. |
| `ki` | 0.01 /s | Integral gain, trims the inverter's own error. |
| `ki_import_x` | 1.0 | Integral gain on an import error (`e > 0`) is `ki * ki_import_x`; export keeps `ki`. 1 is symmetric. Not the reverted import gain of 9630537, which was on the proportional side (see design history). |
| `integral_band_w` | 300 W | Integrate only when deadbanded error is within this. Bigger is a load step. |
| `integral_limit_w` | 300 W | Integral clamp, both signs. |
| `kff` | 0.0 | Solar feed forward gain: `ff_w = -kff * (solar change since the last sample)`, held for the steps on that sample only. |
| `solar_ff_min_w` | 0 W | Above 0, a solar step where either reading is under this (or unknown) is 0. The net demand template floors West at 95 W, so at dawn and dusk it flips 0 to 100 W; the edge research counted 833 crossings in the logs of 19 to 22 Oct (not re-counted here). Recommended 120 at deploy. 0 is off. |
| `ff_guard` | False | On: `ff_w` is set to 0 when it alone would turn the request against the running direction (`_direction`). Replayed on 20 to 22 Oct, kff 1 without it gave 60 % more turn rounds. |
| `deadband_w` | 20 W | Subtracted from the error magnitude. |
| `smoothing_s` | 0.0 s | Low pass on the request. 0 is off. |
| `slew_w_per_s` | 2000 W/s | Max move from the last sent command, times the time since it was sent. |
| `write_threshold_w` | 50 W | Minimum move to send, outside the other send reasons. |
| `min_interval_s` | 3.0 s | Minimum gap between commands, unless an early send applies. Sized for five-write blocks (about 1.8 s); one frame takes about 50 ms, but the five-write fallback still runs on a failed or latched frame (`firmware-dispatch.md`). |
| `keepalive_s` | 40 s | Resend at least this often. |
| `duration_s` | 120 s | Dispatch lease in every command. |
| `flip_hysteresis_w` | 150 W | Margin to turn charge/discharge round. |
| `flip_dwell_s` | 5.0 s | Minimum time in one direction before turning. |
| `stale_after_s` | 30 s | No fresh sample this long: hand back. |
| `power_write_gap_s` | 0.0 s | 0 means every command is a full block write. Above 0: same-block changes go `power_only`, and after a power write ack the next may follow after this gap. |
| `warm_w` | 0.0 W | Ready floor ("Keep warm"). 0 is off. |
| `discharge_needs_soc` | False | On: no discharge (`hi = 0`, `limit = "soc_unknown"`) while `soc` is None, as after a restart whose first samples lacked it. Seen once in 219,759 logged steps. Off: the inverter's own dispatch SOC is the only floor then. |
| `arrive_restart_w` | 0 W | Above 0, restarts the arrival clock each time the battery closes this much on its setpoint. Against a 10 W/s start to 600 W of demand with the test's `LIVE` tuning, 0 lets the integral wind to 24 W and 100 holds it at 5 W (`test_a_slow_start_*`). Recommended 100 at deploy. 0 is off. |
| `full_register` | 252 | Not used by the regulator. The controller publishes a charge to 100 % as `socPercent` this minus 150, since register 250 stopped the battery at 99.2 %. |
| `spike_confirm_w` | 0.0 W | Above 0, a sample whose net demand (`grid_w + battery_w`) moved more than this from the last sample is held: every step on it returns None with `limit = "spike"`, and the integral skips it. The next sample always acts. 0 is off. |
| `integral_reset_w` | 0.0 W | Above 0, `_send` sets the integral to 0 when the new command's `signed_w` moves this much or more from the last one's: what it held trimmed the old level. 0 is off. |
| `import_trim_gap_s` | 6.0 s | A small trim that raises `signed_w` (cuts import) waits this long instead of `small_trim_gap_s`; a trim towards export keeps `small_trim_gap_s`. 6 is the old rule. |
| `offset_comp` | 0.0 | Share of `INVERTER_OFFSET` in `table`. Under `REGULATE` the command asks for the setpoint whose battery reading on `table` is the output (`_setpoint`), so the battery lands on the output. 0 is off, 1 full. |
| `trim_learn` | False | Long term trim on: learning moves the integral into the learned table. Off: nothing is learned, the table is still used. |
| `trim_rate` | 0.0 /s | Share of the integral moved into the table per settled second. 0 learns nothing, even with `trim_learn` on. |
| `trim_limit_w` | 100 W | Bound on each learned node, and on a table that loads. |
| `trim_settle_s` | 20 s | The command's kind and watts unchanged this long before learning. |
| `trim_band_w` | 50 W | Raw grid error (before the deadband) within this to learn. |
| `trim_min_w` | 50 W | Setpoints under this teach nothing. |
| `trim_max_soc` | 97 % | SOC at or over this teaches nothing (BMS taper). |
| `min_send_gap_s` | 2.0 s | Earliest early send after a block write ack. Sized for the five-write block, which is still the fallback path. |
| `max_integrate_s` | 10.0 s | Cap on `dt` per integration, so a sample gap does not wind the integral. |
| `floor_margin_soc` | 1.5 % | Discharge cap is 0 W when SOC <= `discharge_soc + 1.5`. The inverter cuts about 1 % above its floor. |
| `lag_w` | 100 W | Battery this far short of the last setpoint has not arrived: integral waits. |
| `arrive_s` | 15 s | Arrival wait ends after this, for an inverter that clamps below the request. |
| `refused_w` | 30 W | Battery under this counts as giving nothing. |
| `refused_after_s` | 20 s | How long under `refused_w` before a refusal. Starts left 10 W within 5.1 s in logs. |
| `refused_hold_s` | 60 s | Timed hold after a refusal far from the floor. |
| `refused_ask_w` | 100 W | Minimum discharge ask that can be refused. Battery reads about 35 W under a 100 to 400 W command. |
| `refused_near_soc` | 3.0 % | Refusal within this of `discharge_soc` latches. |
| `rearm_soc` | 1.0 % | SOC rise that ends a latched refusal. |
| `small_trim_w` | 50 W | Moves under this are trims. |
| `small_trim_gap_s` | 6.0 s | Trims wait this long since the last command. |
| `warm_band_w` | 50 W | Ready floor replaces only requests above -50 W (trickle charge, idle, small discharge). |
| `solar_deadband_w` | 20 W | A solar change this small between samples gets no feed forward, so night noise stays out. |
| `solar_max_gap_s` | 2.0 s | A solar change over a longer gap gets no feed forward: after a 20 s gap while solar fell 2500 W it would have asked for 2500 W more discharge. Older firmware's 10 s samples get none. |
| `command_step_w` | 10 W | Commands go out rounded to this (`_command`). |
| `default_max_w`, `max_plausible_w`, `correction_max_age_s`, `ack_timeout_s`, `match_wait_s` | 3000 W, 20000 W, 90 s, 10 s, 0.5 s | Read by the controller only; see `ha-controller.md`. |
| `response_step_w`, `response_slow_s`, `response_timeout_s` | 500 W, 4 s, 30 s | Read by `ResponseMonitor` only; see `steplog-and-response.md`. |

The test file's `LIVE` dict, labelled "the live tuning": `target_w=-20, deadband_w=5, ki=0.01, write_threshold_w=20, min_interval_s=2.5, slew_w_per_s=2400, flip_hysteresis_w=150, flip_dwell_s=3, keepalive_s=40, power_write_gap_s=0.5, warm_w=20`. Whether it matches the deployed config: unknown.

## Integration (in `update_grid`, before storing the sample)

The integral runs only if all hold:

1. The previous step regulated (`_regulating`) and there was an earlier sample.
2. Deadbanded error `|e| <= integral_band_w`. `e` uses the policy's grid offset from the last regulating step (`_target_offset_w`; not `offset_w`, the inverter correction).
3. Arrived: last sent is None or normal, or the battery is no more than `lag_w` short, in its own direction, of `_sent_w` (the battery watts the last command should deliver: `signed_w + table(signed_w)`, also after a HOLD or FIXED command), or `arrive_s` has passed on the arrival clock (`_t_arrive`, set at each send). With `arrive_restart_w` above 0, the clock restarts, and `_arrive_w` takes the battery reading, whenever the battery has moved that much towards the setpoint's sign since the last restart, so a slow ramp stays lag. A battery past its setpoint counts as arrived.
4. `sign(e) != _saturation`: conditional integration, frozen when the output is pinned in the error's direction.
5. Not a held spike (`_spike`, see `spike_confirm_w`).

Then `integral += g * e * min(dt, max_integrate_s)`, clipped to `integral_limit_w`, with `g = ki * ki_import_x` on import (`e > 0`) and `ki` otherwise.

Learning (long term trim) runs next, on `c`, the running command's `signed_w` (0 for none or normal). It needs `trim_learn`, `trim_rate > 0`, `_learn_ok` (the last step regulated with `limit` none and no floor, refusal, unknown SOC or full in force even when it did not clip, no saturation, no slew, SOC known and between `discharge_soc + floor_margin_soc + LEARN_SOC_MARGIN` and `charge_soc`), arrived, no held spike, `|c| >= trim_min_w`, `trim_settle_s` since the command last changed (`_t_change`), raw error within `trim_band_w`, no policy offset and SOC under `trim_max_soc`. Then `_teach(c, -trim_rate * dt * integral)` moves the two nodes around `c` (never the 0 W node), each within `trim_limit_w` and `TRIM_SLOPE` (`load_trim` clips a stored table to the limit, and lowering the limit reclips at once, so the table is kept), and the integral gets the actual move added, so the command does not change. After that, with `c` non-zero, the integral is clipped so the combined push, integral minus the trim at `c`, stays within `integral_limit_w`: learning keeps that difference, and the command moves by it. A trim past the limit on its own is bounded by `trim_limit_w` only; the clip range always holds 0, so the integral may take such a trim back towards the limit but is never forced off 0 (before, it was forced to `-trim` and doubled the correction).

## Decision order in `step` (regulating path)

1. `limit = "none"`. A `FIXED` policy with non-finite watts becomes `RELEASE`.
2. `RELEASE`: hand back, return.
3. `HOLD`/`FIXED`: restart, `request = lo = hi = fixed_w` under FIXED and 0 under HOLD, skip to 9. Before, HOLD carried `fixed_w` into the diagnosis fields and the direction while it sent 0 W.
4. Not live: hand back as "stale", return.
5. Bumpless start if the last step did not regulate: `output_w = battery_w`, `_direction = sign(battery_w)`, solar step 0 (every `_restart` leads here). Then a held spike returns None with `limit = "spike"`; output, sent and the diagnosis fields keep their last values.
6. `request = battery_w + kp * e + integral_w + ff_w`, then optional smoothing. `ff_w` is `-kff` times the solar step stored by `update_grid`; P already sends the whole measured demand change, so feed forward only leads a solar ramp. With `ff_guard` on, a feed forward that would flip the sign of `battery_w + p + integral` against a non-zero `_direction` is dropped (`ff_w = 0`).
7. Caps: `lo = -max(0, max_charge_w)`, `hi = max(0, max_discharge_w)`: a negative cap is 0 W, never a push the other way (before, `_clip` returned `lo` when `lo > hi`). With `discharge_needs_soc` on and no SOC read yet: `hi = 0` ("soc_unknown"). Else SOC <= `discharge_soc + floor_margin_soc`: `hi = 0` ("floor"). Else refused: `hi = 0` ("refused"). SOC >= `charge_soc`: `lo = 0` ("full"). `limit` names the cap the request crosses ("discharge_cap" / "charge_cap" by default).
8. `u = clip(request, lo, hi)`, `_saturation = sign(request - u)`.
9. Turn round check (above).
10. Ready floor: `ready_w = warm_w` if `REGULATE` and `0 < warm_w <= hi`. If `-warm_band_w < u < ready_w` and (direction >= 0 or the dwell has passed): `u = ready_w`, `limit = "warm"`, saturation recomputed, direction forced to +1.
11. Slew: `output_w = clip(u, _sent_w +/- slew_w_per_s * (t - t_sent))`. No limit before the first send. Under `REGULATE` the output is then clipped back into `[lo, hi]`, so a cut to a cap goes out whole; HOLD and FIXED keep the slew.
12. If the ready floor applies and `u >= ready_w` and output > -50 W: `output_w = max(output_w, ready_w)`.
13. Offset correction: under `REGULATE` with `offset_comp` above 0 or any learned trim, `offset_w = _setpoint(output_w) - output_w`, else 0. `_setpoint` solves `x + table(x) = w` on the merged nodes of `TRIM_NODES` and `INVERTER_OFFSET`, slope 1 past the ends. Then `_learn_ok` is set for the next sample. The table is indexed by setpoint, so it is solved for the command, not read at the output. Everything before this is battery watts: caps, turn round, ready floor and slew all mean what the battery gives. Only the command is moved.
14. `_command` on `ask = output_w + offset_w`: watts rounded to 10 W. Under `REGULATE`, an output at or over `warm_w` is lifted to `_ready_cmd()`: the ceiling of `_setpoint(warm_w)` under the offset correction, else `ceil(warm_w)`. `warm_w` is battery watts, so the floor's command is the one that lands the battery on it. `HOLD` is `charge 0 W SOC 100`. 0 W keeps the last kind and SOC, or `charge 0 W SOC 100` if there is none. Positive is discharge with `discharge_soc`, else charge with `charge_soc`.
15. `_maybe_send`, in order (it compares commands, so its thresholds are command watts):
    1. Within `min_interval_s` and no early send: None.
    2. No last command, last failed, lease over, or keepalive due: send full.
    3. Kind changed and either side is non-zero: send full.
    4. `to_floor`: `REGULATE`, `warm_w` > 0, last signed under `ready_cmd = _ready_cmd()` and new at or over it. Not checked: that the ready floor applied (`warm_w <= hi`). With `ready_cmd` the floor's setpoint, the command for an output capped under `warm_w` is under it too, unless rounding to 10 W lifts it past.
    5. Small trim: not `to_floor`, non-zero watts, same SOC, move under `small_trim_w` and under `small_trim_gap_s` since last (`import_trim_gap_s` for a move up): None.
    6. `to_floor`, SOC changed, or move >= `write_threshold_w`: send, as `power_only` if same kind/SOC/duration and `power_write_gap_s > 0`.
    7. Else None.

Early send (`_may_send_early`) needs: last not normal, acked, and a fresh sample after the ack. After a power write ack with `power_write_gap_s > 0`: any move after that gap. Otherwise only an import cut (`signed_w` rises) after `min_send_gap_s`.

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
| `max_integrate_s`, hold through short gaps | 70fe0e2 | Do not wind on an error nobody measured. |
| No import gain | 9630537 | Gain above 1 swung the grid more on live noise. |
| Floor hold at discharge SOC | 7603b02 | A growing request against a battery at its limit sent a command every few seconds. |
| Power write hold reverted | b47c99a, 9490f53 | Block writes started a discharge no faster; the hold cost response. |
| Keep warm | f333100 | Discharge starts from 0 W ramp at about 10 W/s on this inverter. |
| 0 W keeps last kind; `floor_margin_soc`; timed refusal; arrival gate; `warm_band_w` | 15aaddd | Kind flips cost block writes; the inverter cuts 1 % above floor; trimming the start lag wound a charge up; a 150 W warm band threw away storable surplus. |
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
| `test_limit_names_what_holds_the_output` | `limit` reads none, discharge_cap, charge_cap, floor and turn_round when each holds the output, and none after a release. |
| `test_no_flapping_*`, `test_default_dwell_*`, `test_flip_needs_margin_*` | Turn round margin and dwell. |
| `test_hold_shows_and_turns_on_the_0_w_*`, `test_the_error_counts_the_policy_offset_*`, `test_stale_*`, `test_correction_updates_*`, `test_release_*`, `test_lease_expiry_*`, `test_a_release_at_start_*` | Hand back rules and lease logic. |
| `test_a_failed_*`, `test_min_interval_holds_across_a_release` | Retry and anti-hammer. |
| `test_acked_*`, `test_early_send_*`, `test_an_ack_counts_*`, `test_power_*`, `test_a_power_change_*`, `test_keepalive_kind_and_soc_*`, `test_after_an_acked_power_write_*`, `test_early_follow_up_*` | Early send and power-only write rules. |
| `test_integral_*`, `test_a_gap_in_samples_*`, `test_closed_loop_settles_*`, `test_small_trims_do_not_hold_*`, `test_a_big_move_still_waits_*` | Integral bounds, freeze, arrival gate. |
| `test_at_the_discharge_limit_*`, `test_discharge_resumes_*`, `test_hold_zero_a_little_above_*`, `test_a_full_battery_*` | SOC floor and full holds. |
| `test_a_refus*`, `test_a_normal_start_is_not_a_refusal`, `test_a_hand_back_forgets_*` | Refusal state machine. |
| `test_keep_warm_*`, `test_the_ready_floor_*`, `test_the_floor_hold_wins_*`, `test_the_slew_does_not_leave_*` | Ready floor. Seeded fuzz: no discharge under `warm_w`, no charge under a discharge-only policy. |
| `test_a_trim_under_fifty_watts_*`, `test_a_load_step_still_follows_*` | Trim gap vs load steps. |
| `test_the_proportional_term_*`, `test_the_caps_after_*`, `test_the_output_before_the_slew_*`, `test_the_direction_and_*`, `test_the_refusal_clock_*` | Diagnosis fields and properties. |
| `test_the_inverter_offset_*`, `test_offset_correction_*`, `test_a_plant_on_the_table_*`, `test_the_caps_stay_battery_watts_*`, `test_under_offset_correction_*` (including the ready floor command and a small rise to it) | Table, a plant on the table lands within 5.9 W from -4000 to 4000 W for `offset_comp` 0, 0.5 and 1, off by default, caps in battery watts, no change to HOLD/FIXED, arrival gate and slew against `_sent_w`. The ready floor fuzz also runs with `offset_comp=1`. |
| `test_with_the_guard_*`, `test_the_feed_forward_guard_is_off_by_default` | Guard off by default; on, feed forward never turns a discharge round alone but still leads a ramp in the running direction. |
| `test_a_big_move_starts_the_integral_from_zero`, `test_the_integral_reset_is_off_by_default` | Integral reset on a command move of `integral_reset_w`, off by default. |
| `test_the_integral_winds_faster_on_import_than_on_export`, `test_the_integral_import_factor_is_1_by_default` | Import factor on the integral gain, symmetric by default. |
| `test_a_small_import_cut_goes_out_after_the_import_trim_gap`, `test_a_small_export_cut_still_waits_the_trim_gap`, `test_the_import_trim_wait_is_the_trim_gap_by_default` | Import trim wait for moves up only, equal to the trim gap by default. |
| `test_learning_*`, `test_a_learned_table_*`, `test_a_table_steeper_than_the_trim_slope_*`, `test_a_table_learned_against_the_trim_slope_*`, `test_nothing_is_learned_*`, `test_forgetting_the_table_zeroes_it`, `test_the_integral_and_the_trim_together_*`, `test_a_trim_past_the_integral_limit_*` | Learning moves the integral into the table and keeps the command, off by default (and at rate 0), a loaded table applies with learning off, bad tables (and ones steeper than `TRIM_SLOPE`) do not load, a table learned to the slope loads back, nodes stay in bounds, nothing is learned at a cap or under a policy offset, the integral minus the trim stays within `integral_limit_w` and a trim past it forces no integral. |
| `test_each_learning_gate_is_a_setting_*`, `test_the_learning_band_is_on_the_raw_grid_error`, `test_learning_stops_a_margin_above_the_floor_hold` | `trim_settle_s`, `trim_min_w`, `trim_max_soc` and `trim_band_w` each stop learning at one value and allow it at another; so does `LEARN_SOC_MARGIN` above the floor hold. Each fails under a mutation that drops its gate. |
| `test_smoothing_moves_the_request_*` | `smoothing_s` 10 moves a 980 W step 100 W after 1 s; 0 sends it whole. |
| `test_a_negative_cap_never_pushes_*` | A negative cap is 0 W. |
| `test_discharge_waits_for_a_known_soc_*`, `test_discharge_without_a_known_soc_*` | `discharge_needs_soc`, off by default. |
| `test_feed_forward_can_ignore_west_crossing_*`, `test_the_feed_forward_minimum_solar_*` | `solar_ff_min_w`, off by default. |
| `test_a_slow_start_need_not_wind_*`, `test_the_arrival_restart_is_off_by_default` | `arrive_restart_w`, off by default. |
| `test_the_former_constants_are_settings_*`, `test_the_floor_margin_is_a_setting`, `test_the_command_rounding_is_a_setting`, `test_the_refusal_time_is_a_setting` | The former constants are settings with their old values, and the code reads them. |
| `test_solar_feed_forward_*` | Feed forward on a 1 s solar change only; 0 for small or unknown changes, after a gap, back from stale and on the first step after a start. |
| `test_the_slew_never_holds_the_output_past_a_cap` | After an early send, a cut to the floor or to a 0 W discharge cap goes out whole. |
| `test_spike_confirm_*`, `test_a_jump_*`, `test_a_spike_gone_*`, `test_a_correction_update_*` | Off by default; a jump over the setting waits one sample, a lasting one goes out then, a gone one moves nothing and winds no integral. A correction update neither confirms nor starts a spike and keeps the sample's solar step. |
| `test_with_the_ready_floor_at_zero_*` | Golden hash `GOLDEN_FLOOR_OFF = (265, "fba5a193a8a348a0")` of the full command list with `warm_w=0` against `RampingInverter`. Any behaviour change with the floor off breaks it. |

## Gotchas

- `HOLD` always sends `charge 0 W SOC 100`, even when the running block is a discharge. A 0 W regulate output keeps the last kind instead.
- Python `round` is half to even: 25 W rounds to 20 W. Hence the ceiling in `_ready_cmd`.
- The command moves `1 / (1 + k * table slope)` W per W of output. Under 100 W of discharge the table falls 0.25 W per W, so at `offset_comp` 1 the command moves 1.33 times the output (1.6 times at 1.5); between 1500 and 2750 W it moves 0.85 times, so the write threshold and trim gate need about 59 W of output there. The command stays monotonic in the output for `offset_comp` under 4.
- Caps are battery watts, so a command may exceed a small cap by the correction: a 1000 W discharge cap asks for 1020 W, a 100 W cap for 130 W, at `offset_comp` 1.
- Rounding to 10 W eats a correction under 5 W, under about 15 W of discharge, and moves the battery up to 5.9 W off the output (5 W times the table's steepest rise, 1.18). At the ready floor the command rounds up instead, so the battery lands at or over `warm_w`. Before, the lift compared battery watts with command watts: at `offset_comp` 1 and a 100 W floor the command was 120 W and the battery 95 W; with a +40 % trim it was 100 W and the battery 140 W.
- At default `write_threshold_w = 50 = small_trim_w` the trim gate changes nothing. It only bites with a lower threshold, as in `LIVE`.
- A stop at 0 W skips the trim gate but still needs the write threshold, a kind change, or keepalive.
- Release and stale hand backs call `_send` directly and skip `min_interval_s`. The next command after them still waits it.
- `_saturation` is set before the turn round check, so a turn round hold does not freeze the integral. The ready floor recomputes it.
- `_restart` clears `refused_soc` and `_t_low` but not `_t_refused`. A timed refusal hold survives a short HOLD, FIXED or stale spell. Any HOLD/FIXED step ends a latched near-floor refusal.
- A mode string other than the four constants takes the regulating branch with `_regulating` False: no integral, bumpless start every step. The controller maps unknown states to `RELEASE` first.
- `fresh=False` samples still update `grid_w`, `battery_w`, `_t_measure` and the integral, but not the solar step or the spike flag.
- Bumpless start does not reset `_t_direction`, so on a new regulator the dwell has already passed.

## Open questions

- Whether the turn round hold leaving the integral running is intended: unknown.
- Whether `_t_refused` surviving `_restart` is intended: unknown.
- The deployed `Settings` values: unknown from this component. `LIVE` in the tests is the only record read.
- Whether `min_send_gap_s` and `min_interval_s` should drop with one-frame block writes: unknown. They still guard the five-write fallback.
