# Step log and step response monitor

The controller writes every input, decision and bus message to a daily JSONL file, and times each large setpoint step to 80 % of its rise.

Last verified: b6f96ed plus the feed forward minimum solar, 2026-10-22. `python3 -m pytest "Home Assistant/tests" tools/bench -q`: 313 passed; `tests/firmware/run` OK; `GOLDEN_FLOOR_OFF` unchanged.

## Files

| Symbol | File | Role |
|---|---|---|
| `StepLog`, `file_name`, `_gzip` | `Home Assistant/custom_components/alpha2mqtt_controller/steplog.py` | JSONL writer on a background thread, midnight rotation |
| `ResponseMonitor` and constants | `.../alpha2mqtt_controller/response.py` | Times large steps; changes no command |
| `Controller._log`, `_log_step`, `_responses` | `.../alpha2mqtt_controller/controller.py` | Builds records, feeds the monitor |
| `SENSORS` entries `step_response`, `slow_responses` | `.../alpha2mqtt_controller/sensor.py` | Exposes the last response and the daily slow count |
| Tests | `Home Assistant/tests/test_steplog.py`, `test_response.py`, plus log and resp cases in `test_controller.py` | |

Neither module imports Home Assistant. `regulator.py` imports neither.

## Where the log goes

- Path: `<log_dir>/<file_name(base_topic)>`. `log_dir` is option `CONF_LOG_DIR` ("log_dir"), default `hass.config.path("alpha2mqtt_controller")`, so `<config>/alpha2mqtt_controller/`.
- `file_name`: lowercase the base topic, replace each run of non `[a-z0-9]` with `_`, strip `_`, add `.jsonl`. `Alpha2MQTT` gives `alpha2mqtt.jsonl`; `Alpha2MQTT/shadow` gives `alpha2mqtt_shadow.jsonl`. A second controller on another topic gets its own file.
- Format: one JSON object per line, compact separators `(",", ":")`, `default=str` (datetimes become text such as `"2026-01-01 00:00:00"`).
- Writes: `StepLog.write` puts a `LogRecord` on a `queue.SimpleQueue`; a `QueueListener` thread writes it. `write` never blocks on disk.
- Rotation: `TimedRotatingFileHandler(when="midnight")`, local time. The closed day becomes `<file>.YYYY-MM-DD.gz`, gzipped through `<dest>.tmp` then `os.replace`, so a gzip cut short by a restart is redone.
- Retention: `backupCount = days`, default 400. The controller passes no `days`, so 400 daily files. Deletion only touches files of the same base name.
- A restart appends. A file last written on an earlier day rolls over on the first write after start.
- `StepLog()` and `close()` run in the executor. `close` drains the queue.
- If `StepLog()` raises `OSError`, the controller warns "step log is off" and runs without it. Then it also skips the `<base>/request/#` subscription and `log_entities` tracking.

## Record contract

Every record: `k` (kind, str) and `t` (Unix wall time in seconds, float, 3 decimals, `time.time()` at the moment `_log` runs, not when the message arrived). Kind specific fields follow.

| `k` | Written when | Fields |
|---|---|---|
| `start` | `async_start`, log open | `code`: first 12 hex of a sha256 over `regulator.py` and `controller.py`, also the Loop version sensor |
| `cfg` | after `start`, and on every `set_setting` | every `Settings` field: `target_w`, `kp`, `ki`, `ki_import_x`, `integral_band_w`, `integral_limit_w`, `kff`, `ff_guard`, `solar_ff_min_w`, `deadband_w`, `smoothing_s`, `slew_w_per_s`, `write_threshold_w`, `min_interval_s`, `keepalive_s`, `duration_s`, `flip_hysteresis_w`, `flip_dwell_s`, `stale_after_s`, `power_write_gap_s`, `warm_w`, `offset_comp`, `spike_confirm_w`, `integral_reset_w`, `import_trim_gap_s`, `trim_learn`, `trim_rate`, `trim_limit_w`, `trim_settle_s`, `trim_band_w`, `trim_min_w`, `trim_max_soc`, `full_register`, `discharge_needs_soc` |
| `step` | each new fast message (`_take`), enabled or not | see below |
| `cmd` | every message on `<base>/request/#` | `topic` (str), `p` (raw payload) |
| `ack` | every message on `<base>/response/set/+` | `topic`, `p` (raw payload) |
| `ack_timeout` | seq echoed but no response in `ACK_TIMEOUT_S` (10 s) | `seq` |
| `resp` | a step response finishes (see monitor) | `result`, `slow`, `from_w`, `to_w`, `rise_w`, `t80_s`, `elapsed_s`, `slope_w_s` |
| `gap` | an input goes missing or returns, once each | `what` (`"policy"`, `"grid estimate"`, `"policy state"`), `missing` (bool), `reason` (only for `policy state`: str or null) |
| `refused` | regulator `refused_soc` goes from None to set | `soc`, `floor` (policy `discharge_soc`) |
| `rearmed` | `refused_soc` clears | `soc` |
| `cross` | `warm_w > 0` and a command turns between charge and discharge | `from_w`, `to_w` (signed W, positive is discharge) |
| `trim_load` | `async_start` found a saved learned table | `loaded` (bool: it passed `load_trim`), `points` (the table now in force, `[[node W, trim W], ...]`) |
| `trim` | learning moved the table, at most once per `TRIM_SAVE_S` (60 s); or the Forget learned offsets button | `points`; `forgot: true` from the button |
| `e` | change of an entity in option `log_entities` | `e` (entity id), `s` (state or null), `a` (attributes dict, `{}` when removed) |
| `stop` | `async_stop` | none |

`step` fields, in order:

| Field | Source | Type, unit |
|---|---|---|
| `seq`, `ms`, `read_ms` | fast message, firmware `sendFastState` | int; `ms` is Pico `millis()` when reads started, `read_ms` their duration in ms |
| `grid`, `pv`, `battery`, `soc`, `dispatch` | fast message keys in `FAST_FIELDS` | raw values (W, %, dispatch start as sent, `"Start"` in tests); null if absent |
| `wait_ms` | `time.monotonic() - t_seen` | int ms the sample waited for its correction (up to `MATCH_WAIT_S`, 0.5 s) |
| `status` | `Controller.status` | str, `"disabled"` when off, else regulator status |
| `limit` | `Regulator.limit` | str: what held the output (`none`, `floor`, `soc_unknown`, `refused`, `full`, `warm`, `discharge_cap`, `charge_cap`, `turn_round`, `spike`); kept from the last step while disabled |
| `grid_w`, `solar_w`, `error_w`, `integral_w`, `ff_w`, `request_w`, `output_w`, `offset_w` | controller and regulator after this step | float W rounded to 0.1, or null. `solar_w` is the solar the feed forward used (null when unknown), `ff_w` the feed forward, `offset_w` the offset correction (the command asks for `output_w + offset_w`) |
| `sent` | `Command` returned by this step's `_run` | null or `{kind, watts, soc, power_only}` |

Not in `step`: firmware `haIdle`, ack round trip (`ack_after_ms` lives only in `Controller.ack`, shown as attributes of the `last_command` sensor).

## Step response monitor (`ResponseMonitor`)

Signs follow the regulator: positive is discharge. Pure observer. Its output goes to the log and sensors only.

Constants:

| Name | Value | Meaning |
|---|---|---|
| `STEP_W` | 500.0 W | minimum step to time |
| `REACH` | 0.8 | arrival is 80 % of the rise |
| `SLOW_S` | 4.0 s | arrival later than this is `slow` |
| `TIMEOUT_S` | 30.0 s | give up, result `timeout` |
| `CANCEL` | 0.8 | a new setpoint below 80 % of the step's target cancels it |

Algorithm:

- `command(t, setpoint_w, battery_w)`: called from `Controller._publish` after a successful publish, with `time.monotonic()`, `cmd.signed_w` (None for `normal`) and the last battery reading.
  1. If a step is open and the new setpoint is None or `d * setpoint < CANCEL * d * target`, finish it as `cancelled`.
  2. If no step is open and both values are known, `d = sign(setpoint)`. Open a step if `d * (setpoint - battery) > STEP_W` and `d * (setpoint - prev_setpoint) > STEP_W` (prev None counts as 0). The second test stops keepalives of a lagging setpoint from opening new steps.
- `sample(t, battery_w)`: called from `Controller._take` on every fast message, with `t_seen` (monotonic arrival time). Finishes `ok` or `slow` when `d * (battery - start) >= REACH * d * (target - start)`, else `timeout` once `t - t0 >= TIMEOUT_S`.
- Record from `_finish`:

| Field | Value |
|---|---|
| `result` | `"ok"`, `"slow"`, `"timeout"` or `"cancelled"` |
| `slow` | true for `slow` and `timeout` |
| `from_w`, `to_w` | battery at start, setpoint target; int W |
| `rise_w` | `d * (target - start)`, int W, positive for charge and discharge |
| `t80_s` | elapsed s, 0.1 s, only for `ok` and `slow`; else null |
| `elapsed_s` | seconds since the step opened, 0.1 s |
| `slope_w_s` | mean W/s moved in the step's direction; null if no battery value or elapsed <= 0 |

Controller handling (`_responses`): every record is logged as `resp`. `cancelled` stops there. Others set `last_response`. If `slow`, `_slow` (local date, count) increments and `_LOGGER.warning` fires.

Sensors: `step_response` state is `last_response["t80_s"]` (null after a timeout), attributes are the whole record. `slow_responses` is `slow_responses_today`, 0 once the local date changes. All controller sensor attributes are unrecorded (`_unrecorded_attributes = MATCH_ALL`), so history holds only the states.

## Firmware timing

- Fast state (`sendFastState`): `seq` counts messages from boot, `ms` is `millis()` at read start, `read_ms` is read duration.
- Set response (`closeSetResponse`): echoes the request `seq` (0 if none) and `ms`, `millis()` when the writes finished.
- Both `ms` values use the same Pico clock, so an `ack` payload can be placed among `step` records by `ms`.
- The firmware does not time steps or count slow ones. Commit 7a07eab touched only the Home Assistant side.

## Diagnosing the loop

| Question | Look at |
|---|---|
| What was asked? | `step.sent`, and `cmd.p` (`watts`, `socPercent`, `duration`, `seq`, `powerOnly`) |
| Did the inverter accept it? | `ack.p` `responseStatus`, `failureDetail`, `write`; `ack_timeout` |
| Did the battery follow? | `step.battery` over following steps against the signed setpoint; `resp.result` |
| How long did it take? | `resp.t80_s`, `elapsed_s`, `slope_w_s` (about 9 W/s is the slow ramp; hundreds is healthy); `ack.p.ms` minus the matching `step.ms` window; `t` gap between `cmd` and `ack` |
| Was the sample late or stale? | `step.wait_ms`, `read_ms`, `seq` gaps, `ms` resets (reboot) |
| Which code ran? | `start.code`, the Loop version sensor |
| Why did the regulator do X? | `step.status`, `limit`, `error_w`, `integral_w`, `ff_w`, `offset_w`, `request_w`, `output_w`, `grid_w`, `solar_w`; `cfg` in force; `gap`, `refused`, `rearmed`, `cross`; `e` for policy or input entities |
| Who wrote the inverter? | `cmd` records with no matching `step.sent` came from a tick, an entity change, a person or a script |

## History

- 24f5a85: first step log. One line per step while enabled, `RotatingFileHandler` at 10 MB x 3 files.
- 19c820f: a `step` per fast message whether enabled or not, plus `cmd`, `ack`, `cfg`, `gap`, `e`, `start`, `stop`. Midnight rotation, gzip, 400 days, `log_dir` and `log_entities` options. Reason in the message: replay could not see the noise between quiet steps, nor writes by hand.
- cff2ebf: added `ack_timeout` (origin found with `git log -S`; diff not read).
- 9684b66: added `cross` with the ready floor (`warm_w`).
- 7a07eab: `response.py`, `resp` records and the two sensors. Reason: the inverter can answer a discharge step with a slow ramp, and nothing showed it.
- 75e08a8: added `refused` and `rearmed`.
- 2026-10-22: `step` lost `load_w` and gained `solar_w`, `offset_w` and `limit`; `start` gained `code`.

## Invariants pinned by tests

- One JSON line per record; datetimes become text; a restart appends (`test_steplog.py`).
- A closed day gzips to `<file>.YYYY-MM-DD.gz`; today stays plain; only the newest `days` of its own base name survive.
- `step` written for every fast message, enabled or not, with the listed keys; `sent` null when nothing went out (`test_every_fast_message_logs_a_step_enabled_or_not`).
- `wait_ms` measures the wait for a seq-matched correction (250 in the test).
- `cfg` holds every `Settings` field. Start and stop write `start`, `cfg`, `stop` in order.
- A healthy 1.9 kW step: `ok`, `t80_s <= 3`, `rise_w == 1900`. A 9 W/s ramp from 100 to 2000 W: `timeout`, `t80_s` null, `slope_w_s` about 9.
- 80 % reached at 6 s is `slow`; at 4 s is `ok` (the boundary is strict `> SLOW_S`).
- A cut to 1500 of a 2000 step cancels; to 1700 keeps measuring. A release cancels.
- Down steps and steps under 500 W are not timed. A charge step is timed the same way.
- Keepalives of the same setpoint over 120 s give one `timeout`, not one per resend.
- Controller: a fast plant leaves `slow_responses_today == 0`; a 9 W/s plant counts 1.

Test run, from the repo root: `python3 -m pytest "Home Assistant/tests/test_steplog.py" "Home Assistant/tests/test_response.py" -q` gave 16 passed. `test_controller.py -k "log or resp or slow"` gave 16 passed.

## Gotchas

- `step.sent` is what the regulator chose in that step, not proof of a publish. The publish task can be dropped as superseded or fail. `cmd` is the bus truth, and it includes the controller's own requests, so they appear twice.
- Commands chosen on the 1 s tick or an entity change have no `step` record; only `cmd` shows them.
- Regulator fields in `step` are after the step. A `resp` from a sample comes before that sample's `step` line.
- Monitor time is monotonic; log `t` is wall time. They do not mix.
- `timeout` is checked only when a sample arrives. If fast messages stop, `elapsed_s` can exceed 30.
- A step stays measured against its first target. A later larger setpoint neither restarts nor retargets it.
- A cancel and a new step can happen in one `command` call.
- A turn from one direction to the other passes both tests and is timed, though a step towards zero is not (read from code; no test).
- No battery reading yet means no step opens. A failed publish never reaches `command`.
- `slow_responses_today` lives in memory and restarts at 0 with the controller.

## Open questions

- Type of `p` in `cmd` and `ack` when Home Assistant delivers bytes: unknown; `default=str` would write a `b'...'` text.
- Exact `dispatch` values from the firmware beyond `"Start"`: unknown.
- Whether `TimedRotatingFileHandler` rolls correctly across DST changes in this setup: unknown.
- Any replay or analysis tool that reads these logs: none found in this repo; unknown elsewhere.
