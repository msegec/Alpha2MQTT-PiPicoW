The Home Assistant glue that feeds `Regulator` from MQTT and entities and publishes its commands to Alpha2MQTT.

Last verified: working tree on 1e945f6 plus the diagnosis attributes, 2026-10-21. `python3 -m pytest "Home Assistant/tests/test_controller.py" -q`: 47 passed in 0.09s.

Files: `Home Assistant/custom_components/alpha2mqtt_controller/` `controller.py`, `__init__.py`,
`config_flow.py`, `number.py`, `switch.py`, `sensor.py`, `manifest.json`, `translations/en.json`.
Tests: `Home Assistant/tests/test_controller.py` (Home Assistant stubbed with `MagicMock`, no HA install needed).

## Conventions

| Quantity | Sign or unit |
|---|---|
| Grid (`grid_w`, `estimate_grid` result) | W, positive is import |
| Battery reading `REG_BATTERY_HOME_R_BATTERY_POWER` | W, positive is discharge (from `estimate_grid`: grid = correction - battery) |
| `Command.signed_w` | `+watts` for discharge, `-watts` for charge and normal |
| `Policy.fixed_w` | positive is discharge; a fixed charge is passed as `-watts` |
| Policy `grid_offset` | added to the grid target while charging; positive tops up from the grid |
| Time | `time.monotonic()` for control; `time.time()` only for step log `t`; `dt_util` for sensor timestamps and the daily slow count |

## Lifecycle

- `manifest.json`: domain `alpha2mqtt_controller`, `config_flow: true`, depends on `mqtt`, `iot_class: local_push`, version 0.1.0, no requirements.
- `async_setup` (YAML): if `alpha2mqtt_controller:` is in the config, starts an import flow with that dict.
- `async_setup_entry`:
  1. `mqtt.async_wait_for_mqtt_client`; if false, raises `ConfigEntryNotReady` and HA retries.
  2. Builds `Controller(hass, dict(entry.data), entry.entry_id)` and stores it in `entry.runtime_data`.
  3. Forwards to platforms NUMBER, SENSOR, SWITCH.
  4. `Controller.async_start`.
- `Controller.__init__`: fresh `Regulator()`, `enabled = False`, `ResponseMonitor()`. Device name is
  "Alpha2MQTT battery controller", plus " (<base>)" when the base topic is not `Alpha2MQTT`, so a shadow
  controller gets its own entity ids (24f5a85).
- `async_start` creates, in order:
  - `StepLog` at `<log_dir or hass.config.path(DOMAIN)>/file_name(base)` on the executor. `OSError` logs a warning and leaves the step log off.
  - MQTT subscribe: state topic -> `_on_state`.
  - MQTT subscribe: `{base}/response/set/+` -> `_on_response`.
  - Only with a step log: logs `start` and `cfg`, subscribes `{base}/request/#` -> `_on_request` (raw log only), and tracks `log_entities` state changes -> `_on_log_entity`.
  - State change tracking of the configured policy, grid scale, correction, max charge, max discharge entities -> `_on_entity`.
  - `async_track_time_interval` 1 s -> `_on_tick`.
- `async_unload_entry`: `Controller.async_stop`, then unload platforms.
- `async_stop`: `async_set_enabled(False)` (may publish normal), then in `finally` drops every subscription, cancels a pending match wait timer, logs `stop`, closes the step log on the executor.

## Triggers of a regulation step

| Trigger | Handler | What runs |
|---|---|---|
| State topic message | `_on_state` | parse, dedupe on `seq`, maybe wait for correction, then `_take` -> `_measure(fresh=True)` -> `_run` |
| Match wait timer, 0.5 s | `_on_wait_over` | `_take` on the waiting sample |
| 1 s interval | `_on_tick` | `_check_ack`, then `_run` |
| Correction entity change, entity carries `seq` | `_on_entity` | steps a waiting sample whose seq now matches, else `_run` only |
| Correction (no seq) or grid scale entity change | `_on_entity` | `_measure(fresh=False)` |
| Policy or cap entity change | `_on_entity` | `_run` |

`regulator.step` is called from `_run` only, and only while `enabled`. It runs several times a second
(sample, tick, entity changes). The regulator decides whether a call yields a command.
`_measure` runs whether enabled or not, so `regulator.update_grid` keeps tracking readings while disabled.

## One step, data flow

1. `_on_state`: `json.loads(msg.payload)`. Reads `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1` (raw grid) and
   `REG_BATTERY_HOME_R_BATTERY_POWER` as float. Parse error, missing key or implausible value: message dropped silently.
2. `_plausible`: finite and `abs(x) <= MAX_PLAUSIBLE_W` (20000 W). Guards a kW value read as W, and NaN (e0e291d).
3. Same `seq` as the last sample: ignored as a repeat. No `seq`: never deduped.
4. A sample still waiting is stepped first. Then, if `_correction_behind(seq)` (correction entity has a `seq`
   attribute that differs from the sample's, compared as strings), the sample waits up to `MATCH_WAIT_S` 0.5 s.
   Otherwise `_take` at once (8aa10b6).
5. `_take`: stores raw grid and battery; SOC from `REG_BATTERY_HOME_R_SOC`, or `None` when absent (the regulator keeps
   its last SOC, 7603b02); feeds `ResponseMonitor.sample(t_seen, battery)`; calls `_measure(fresh=True)`; logs a `step` record.
6. `_grid_estimate` by `grid_source`:
   - `estimate`: correction via `_number(..., CORRECTION_MAX_AGE_S=90)`; `None` if missing or older than 90 s
     by `last_reported`. Result `estimate_grid(correction, battery)` = correction - battery.
   - `clamp` (default): scale from `grid_scale_entity`, else `grid_scale` config (1.0). A configured scale
     entity that is unreadable gives `None`. Correction entity optional (0 if unset, `None` if set and unreadable,
     no age check). Result `scale * raw_grid + correction`.
7. `_measure`: grid `None` logs a `grid estimate` gap and skips `update_grid`; the regulator then holds its last
   command until `stale_after_s` (70fe0e2). Else `regulator.update_grid(now, grid_w, battery_w, fresh, soc, solar_w)`.
   `solar_w` is `_solar()`: the correction entity's `west` + `east` attributes, read from the same state as the
   correction; None unless both parse as numbers and the sum is plausible.
8. `_run` (enabled only): `_policy()` -> `regulator.step(now, policy)`. Any exception in that: logged once,
   then `regulator.step(now, Policy(RELEASE))`. Logs `refused` / `rearmed` on `regulator.refused_soc` edges
   (75e08a8), and `cross` when `warm_w > 0` and the kind flips between charge and discharge (9684b66).
9. A command goes out through `hass.async_create_task(self._publish(cmd))`. Listeners (sensors) are notified.

## Policy entity to `Policy` (`_read_policy`, `_policy`)

Default entity `sensor.battery_auto_action`. State and attributes:

| State | Result |
|---|---|
| missing, `unavailable`, `unknown` | unreadable (`None`) |
| `hold` | `Policy(HOLD)` |
| `normal` | `Policy(RELEASE)` |
| other than charge/discharge | `Policy(RELEASE, reason="unknown policy state '<first 40 chars>'")` (8259b36) |
| charge/discharge, `soc_target` not a number | unreadable |
| `fixed` == "true" | `Policy(FIXED)`; `watts` attribute, must be plausible, else unreadable. Charge: `fixed_w=-watts, charge_soc`; discharge: `fixed_w=watts, discharge_soc` (50850bf) |
| `charge` | `REGULATE`, `max_charge_w=cap`, `charge_soc=soc_target`, `target_offset_w=grid_offset` (0 if bad or implausible, 8feffdc) |
| `discharge` | `REGULATE`, `max_discharge_w=cap`, `discharge_soc=soc_target` |

- Caps come from `max_charge_entity` / `max_discharge_entity`; missing or unreadable means `DEFAULT_MAX_W` 3000 W.
- `both` == "true" plus a numeric `other_soc` opens the other direction with its cap and `other_soc` as its limit
  (4aa846d). Otherwise the other direction gets 0 W with charge_soc 100 or discharge_soc 10.
- `_policy`: an unreadable policy reuses the last good one until `settings.stale_after_s` (default 30 s), then
  `Policy(RELEASE)`. An explicit `normal` releases at once (70fe0e2). Gaps logged once at start and end.

## Publish (`_publish`)

- Dropped if `regulator.sent` is set and is not this command: superseded before the task ran, as by the switch going
  off (cff2ebf).
- `_cmd_seq` += 1 (starts at 0 per controller instance, so first seq after every start or reload is 1).
- Topic: `{base}/request/set/{kind}`, kind `charge`, `discharge` or `normal`.
- Payload, compact JSON (`separators=(",", ":")`):
  - normal: `{"void":0,"seq":N}`
  - charge/discharge: `{"watts":W,"socPercent":S,"duration":D,"seq":N}`, plus `"powerOnly":1` when `cmd.power_only` (a3a20a7).
  - Comment: the Pico reads at most 127 bytes of payload; these stay under 90.
- Publish exception: `regulator.fail()` if this is still the sent command, error logged once; regulator resends in full after the minimum gap.
- Success: records `last_command`, `last_sent`, `last_seq`; `_sent_mono = {seq: monotonic}` (only the newest);
  appends to the 1 h command counter; `ResponseMonitor.command(t, signed_w or None for normal, battery_w)`.

## Responses (`_on_response`, `_check_ack`)

- Every message on `{base}/response/set/+` is logged raw as `ack`. Non-JSON is then ignored.
- Any response with a `seq` sets `_seq_echo`: the firmware echoes seq.
- `ack` dict (shown on the Last command sensor): `ack_seq`, `ack_status` (`responseStatus`), `ack_detail`
  (`failureDetail`), `ack_after_ms`, `ack_write` (`write`: power, block or absent on older firmware).
- Success means `"success"` in `responseStatus` lowercased; the firmware spells it `setDishargeSuccess`.
- Only the newest counts: `seq == last_seq` and `regulator.sent is last_command`. Then success calls
  `regulator.ack(now, power_write=(write == "power"))` (754c81d, a3a20a7); failure calls `regulator.fail()`.
- `_check_ack` each tick: once `_seq_echo`, a newest command unanswered for `ACK_TIMEOUT_S` 10 s calls
  `regulator.fail()` once and logs `ack_timeout` (cff2ebf). Firmware that never echoed seq is never timed out.

## Config options (`config_flow.SCHEMA`, YAML `CONFIG_SCHEMA`)

| Key | Default | Effect |
|---|---|---|
| `state_topic` | `Alpha2MQTT/state/fast` | sample source; older firmware: `Alpha2MQTT/state/second/ten` (7b759af) |
| `base_topic` | `Alpha2MQTT` | request, response topics, entry unique id, device name, step log name |
| `policy_entity` | `sensor.battery_auto_action` | policy, any domain |
| `grid_source` | `clamp` | `clamp` or `estimate` (50850bf) |
| `grid_scale` | 1.0 (form range -100 to 100) | clamp multiplier |
| `grid_scale_entity` | none | overrides `grid_scale` |
| `correction_entity` | none | W added to scaled clamp, or house load less solar under `estimate`; may carry `seq`, and `west` and `east` (solar W) for the solar feed forward |
| `max_charge_entity`, `max_discharge_entity` | none (3000 W) | caps |
| `log_dir` | `<config>/alpha2mqtt_controller` | step log directory |
| `log_entities` | `[]` | each change logged as `e` |

- Entity pickers for the optional entities allow domains `sensor`, `input_number`, `number`.
- Unique id is `base_topic`; a second entry on the same base aborts `already_configured`.
- `async_step_import`: YAML stays owner; a changed file updates the existing entry.
- Options flow (dd0ea1e): same form filled from `entry.data`; save replaces `entry.data` and schedules a reload.
  Options stay `{}`. Tuning numbers are not in the form, to keep one owner per value.

## Entities

Switch `Enabled` (`{entry_id}_enabled`): off by default. Restores `on` after restart via `RestoreEntity`.
Off -> on: just sets `enabled`. On -> off: `regulator.step(now, Policy(RELEASE))`, publishes normal if the
regulator returns one, even if MQTT is down the switch still turns off; the 120 s lease then ends dispatch.

Numbers (`TuningNumber`, `RestoreNumber`, box mode): each writes `regulator.settings.<key>` via `set_setting` and logs `cfg`.
Restore: last value, else the `Settings` default, clamped into the current range.

| Key | Range, step | Key | Range, step |
|---|---|---|---|
| target_w | -500..500 W, 5 | keepalive_s | 10..100 s, 0.5 |
| kp | 0..1.5, 0.05 | flip_hysteresis_w | 0..1000 W, 10 |
| ki | 0..0.2, 0.005 | flip_dwell_s | 0..600 s, 0.5 |
| kff ("Solar feed forward gain") | 0..2, 0.05 | stale_after_s | 3..100 s, 0.5 |
| deadband_w | 0..200 W, 5 | power_write_gap_s | 0..5 s, 0.5 (0 off) |
| smoothing_s | 0..300 s, 0.5 | warm_w | 0..500 W, 5 (0 off) |
| slew_w_per_s | 50..5000 W/s, 50 | min_interval_s | 2..30 s, 0.5 |
| offset_comp ("Offset correction") | 0..1.5, 0.05 (0 off) | | |
| write_threshold_w | 10..500 W, 10 | | |

Not numbers: `integral_band_w`, `integral_limit_w`, `duration_s` (120 s lease). They show read only on the status sensor.

Sensors (`ControllerSensor`, push only, all attributes unrecorded via `MATCH_ALL`). Control never reads them.

| Key | Value |
|---|---|
| status | `regulator.status`, or `disabled`; attrs `policy_gap_since`, `sample_gap_since`, `integral_band_w`, `integral_limit_w`, `lease_s` |
| grid (power) | `grid_w` rounded |
| error, output, battery, integral (power) | regulator fields; output attrs request, integral, feed_forward, offset, `p`, `u`, `slewed` |
| limit | `regulator.limit` when enabled, else None; attrs `lo`, `hi`, `direction`, `direction_since`, `refusal_since`, `refused_soc` |

Diagnosis attributes (all for display, control never reads them):

- Times are UTC text to the second from `Controller.wall(name, t)`, which converts a monotonic time once per
  value and caches it per name, so the text stays put until the time itself changes.
- `policy_gap_since`: while the policy is in a gap, the last good read; the policy is kept until `stale_after_s`
  from it. None otherwise.
- `sample_gap_since`: the last fresh sample, once it is older than `SAMPLE_LATE_S` 3 s; None before. Firmware on the
  10 s topic would set and clear it every sample.
- `direction_since`, `refusal_since`: the regulator properties of those names; None when unset.
- Write cost: status and limit attributes change only at those edges. `p`, `u`, `slewed` change every step but sit on
  the output power sensor, which writes only on its own `MOVE_W`/`EVERY_S` gate. All attributes stay unrecorded
  through `MATCH_ALL`; a state row is still written on each state_changed.
| sent (power) | `last_command.signed_w` |
| commands | publishes in the last 3600 s (bebb043) |
| last_command | `normal` or `"<kind> <watts> W"`; attrs soc, seq, sent time, ack dict |
| step_response | `t80_s` of the last non-cancelled response record; attrs the record |
| slow_responses | slow responses today, local date |

Power sensors write on a move of 25 W (`MOVE_W`) or after 10 s (`EVERY_S`), to cut recorder rows (e0e291d).
Non-power sensors without attributes write only on change.

## Safety behaviour

| Situation | Behaviour |
|---|---|
| Implausible or unparsable sample | ignored; no step |
| Grid estimate input missing or stale | no `update_grid`; regulator holds last command until `stale_after_s`, then releases |
| Policy unreadable | last good policy until `stale_after_s`, then release |
| Unknown policy state | release at once, status says why, warning once per value |
| Exception in a step | release step, logged once |
| Publish fails | `regulator.fail()`, full resend later |
| Firmware refuses newest command | `regulator.fail()` |
| No response 10 s, firmware echoes seq | `regulator.fail()` once |
| Disable | normal published if regulator returns one; lease ends dispatch otherwise |
| HA restart or reload | new `Regulator` and seq counter; switch restores; numbers restore. Per a regulator comment, the first release with nothing sent yet sends normal in case a lease is running |
| MQTT not up at setup | `ConfigEntryNotReady`, retried |
| Firmware silent | no samples; regulator goes stale and releases (stale logic is the regulator's) |

## Step log records written by controller.py

`start`, `stop`, `cfg` (all settings), `step` (every fast message, enabled or not, 19c820f), `cmd` (every
`{base}/request/#`), `ack`, `ack_timeout`, `gap`, `refused`, `rearmed`, `cross`, `resp`, `e`. Each record
has `k` and `t`. File format and rotation belong to `steplog.py`.

## Tests to behaviour

| Test | Pins |
|---|---|
| `test_success_for_the_last_seq_acks`, `test_old_seq_failure_or_no_seq_does_not_ack`, `test_no_ack_once_a_newer_command_is_pending` | only the newest seq, still current, acks |
| `test_the_ack_says_whether_only_the_power_was_written` | `power_write` from `write` |
| `test_a_failed_write_of_the_newest_command_is_retried` | refusal of newest calls fail, older does not |
| `test_a_command_with_no_response_is_sent_again_in_full`, `test_a_command_with_a_response_is_not_timed_out` | 10 s ack timeout only after seq echo, once |
| `test_a_command_superseded_before_its_publish_is_dropped` | superseded publish dropped |
| `test_power_only_commands_carry_the_flag_and_others_do_not` | `powerOnly` payload |
| `test_state_topic_defaults_to_the_fast_state_and_keeps_an_explicit_one` | default state topic |
| `test_min_interval_restores_in_half_seconds_from_2_s` | restore clamps into range |
| `test_the_options_form_offers_the_entry_as_it_is`, `test_saved_options_replace_the_entry_data_and_reload_it` | options flow |
| `test_a_gap_in_an_input_holds_the_last_command_until_stale_after`, `test_an_input_back_within_stale_after_keeps_regulating` | gap hold, release after 30 s |
| `test_a_policy_of_normal_releases_at_once`, `test_an_unknown_policy_state_*` | release paths and once per value logging |
| `test_at_the_policy_soc_the_battery_is_held_at_zero_not_asked_to_discharge` | SOC passed through; one charge 0 W |
| `test_solar_comes_from_*` | solar from `west` + `east`, None when either is missing or not a number; `ff_w` in the step |
| `test_net_demand_*`, `test_a_sample_waits_*`, `test_without_*seq*`, `test_a_new_sample_steps_on_one_still_waiting_first` | seq matching and 0.5 s wait |
| `test_every_fast_message_logs_a_step_enabled_or_not`, `test_a_step_that_waited_*`, `test_requests_and_responses_are_logged_raw`, `test_gaps_are_logged_*`, `test_log_entities_*`, `test_start_and_stop_*`, `test_the_log_directory_can_be_set` | step log contents and paths |
| `test_a_tuning_change_logs_every_setting`, `test_the_ready_floor_can_be_set_down_to_10_w` | numbers |
| `test_a_turn_through_0_w_*`, `test_a_refusal_near_the_floor_*` | `cross`, `refused`, `rearmed` records |
| `test_a_fast_step_response_*`, `test_a_9_w_per_s_step_response_is_counted_slow` | response timing and slow count |
| `test_the_status_says_*`, `test_the_output_shows_*`, `test_the_limit_shows_*` | diagnosis attributes, wall times worked out once |

## Gotchas

- An unreadable cap entity silently means 3000 W, not a gap.
- In `clamp` mode the correction has no 90 s age check; only `estimate` mode has one.
- A correction entity with a `seq` attribute never triggers `_measure` on its own change; it only steps a waiting sample or runs `_run`.
- `{base}/request/#` and `log_entities` are subscribed only when the step log opened.
- Every options save reloads the entry, so `async_stop` runs `async_set_enabled(False)` and may publish normal before the switch re-enables.
- Platforms are forwarded before `async_start`, so the switch may restore `on` before subscriptions exist. Effect: guess, harmless (no samples yet).
- Sensors with attributes (`status`, `limit`, `last_command`, `step_response`, `output`) skip the unchanged-value gate. HA core fires no state_changed for an unchanged state and attributes (from its `async_set`, not measured on a host), so they rely on that.

## Open questions

- Seq restarts at 1 after every start or reload. A late response from before the restart carrying the same seq could ack the new command: unknown if this happens.
- Behaviour when the firmware sends `seq` as a string versus an int in responses: unknown; `_on_response` compares with `==` while `_correction_behind` compares strings.
