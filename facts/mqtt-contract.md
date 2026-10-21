# MQTT contract and HA package

Every topic between the Pico firmware and Home Assistant, who writes and reads it, and what the HA package wires up.

Last verified: working tree on 75e08a8 plus the 2026-10-21 cleanup and the past-full SOC 101; package templates on 2026-10-21 rendered offline with jinja2. Method: read and grep only; nothing was run against MQTT or hardware.

## Ground rules

- Base topic is `DEVICE_NAME` in `Definitions.h`, `"Alpha2MQTT"`. Firmware topics are `DEVICE_NAME` plus a
  `MQTT_SUB_*` or `MQTT_MES_*` suffix. The controller takes the base from config `base_topic` (default `Alpha2MQTT`).
  The YAML package hard codes `Alpha2MQTT/...`.
- Firmware MQTT client is PubSubClient. `sendMqtt` calls `_mqtt.publish(topic, payload)`: QoS 0, not retained.
- No firmware publish is retained. The firmware sets no will and no availability topic. HA sensors rely on
  `expire_after` instead.
- The controller and the package scripts publish with defaults (`mqtt.async_publish`, `mqtt.publish`): not retained.
- State JSON keys are the register's `mqttName`, taken from `handledRegisters` in `Registers.h`, equal to the C define name
  (for example `REG_BATTERY_HOME_R_SOC`). A register that fails to read is left out of the payload.
- Request payloads over `MQTT_REQUEST_PAYLOAD_MAX` (127 bytes, `Dispatch.h`) are dropped by `requestPayloadFits`
  with no response.

## Topic table

Direction: FW->HA is published by the Pico. HA->FW is subscribed by the Pico. "pkg" is `Home Assistant/alpha2mqtt.yaml`.
"ctl" is the `alpha2mqtt_controller` custom component.

| Topic | Dir | Publisher | Consumers | Payload | Period or trigger |
|---|---|---|---|---|---|
| `Alpha2MQTT/state/fast` | FW->HA | `sendFastState` | ctl `_on_state` (default `state_topic`); pkg 4 Fast sensors | JSON: `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1`, `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1`, `REG_BATTERY_HOME_R_BATTERY_POWER`, `REG_BATTERY_HOME_R_SOC`, `REG_DISPATCH_RW_DISPATCH_START`, then `seq`, `ms`, `read_ms`, `haIdle` | every 1 s (`STATUS_INTERVAL_FAST`) |
| `Alpha2MQTT/state/second/ten` | FW->HA | `sendData` | pkg 18 sensors | JSON, 18 keys from `_mqttTenSecondStatusRegisters` | every 10 s |
| `Alpha2MQTT/state/minute/one` | FW->HA | `sendData` | pkg 35 sensors | JSON, 35 keys: 3 voltages, 8 energy totals, 24 battery health keys | every 60 s |
| `Alpha2MQTT/state/minute/five` | FW->HA | `sendData` | pkg 24 sensors | JSON, 24 slow battery keys | every 300 s |
| `Alpha2MQTT/state/hour/one` | FW->HA | `sendData` | pkg 4 sensors | JSON: 3 frequencies, `REG_SYSTEM_OP_R_SYSTEM_FAULT_1` | every 3600 s |
| `Alpha2MQTT/state/day/one` | FW->HA | never in practice | none | none | array empty |
| `Alpha2MQTT/state/device` | FW->HA | `sendDeviceState` | pkg Pico VSYS, Pico USB Power | `{"vsys_mv": int, "usb_power": bool}` | every 60 s, with minute/one |
| `Alpha2MQTT/request/set/charge` | HA->FW | ctl `_publish`; pkg script `alpha2mqtt_set_charge` | FW `mqttCallback` | `watts`, `socPercent`, `duration` required; `seq`, `powerOnly` optional | ctl: each regulator step that emits a command; others on demand |
| `Alpha2MQTT/request/set/discharge` | HA->FW | ctl; pkg script `alpha2mqtt_set_discharge` | FW | same keys as charge | same |
| `Alpha2MQTT/request/set/normal` | HA->FW | ctl (`{"void":0,"seq":n}`); pkg script `alpha2mqtt_set_normal` | FW | any non-empty JSON; `seq` optional | on release |
| `Alpha2MQTT/response/set/charge` | FW->HA | `mqttCallback` | ctl `_on_response` (via `response/set/+`); pkg Charge Response | `responseStatus`, `failureDetail`, `write`, `frames` (block only), `seq`, `ms` | once per request |
| `Alpha2MQTT/response/set/discharge` | FW->HA | same | ctl; pkg Discharge Response | same | once per request |
| `Alpha2MQTT/response/set/normal` | FW->HA | same | ctl; pkg Normal Response | `responseStatus`, `failureDetail`, `seq`, `ms` | once per request |
| `Alpha2MQTT/request/read/register/handled` | HA->FW | nothing in repo (README documents it) | FW | `{"registerAddress": "<hex>"}` | on demand |
| `Alpha2MQTT/response/read/register/handled` | FW->HA | FW | nothing in repo | `responseStatus`, `registerAddress`, `functionCode`, `registerName`, `dataType`, `dataValue`, `formattedDataValue`, `rawDataSize`, `rawData`, `end` | per request |
| `Alpha2MQTT/request/read/register/raw` | HA->FW | nothing in repo (README documents it) | FW | `registerAddress` (hex), `dataBytes` (dec) | on demand |
| `Alpha2MQTT/response/read/register/raw` | FW->HA | FW | nothing in repo | generic response keys | per request |
| `Alpha2MQTT/request/write/register/raw/data` | HA->FW | nothing in repo (README documents it) | FW | `registerAddress`, `dataBytes`, `value` | on demand |
| `Alpha2MQTT/response/write/register/raw/data` | FW->HA | FW | nothing in repo | generic response keys | per request |
| `Alpha2MQTT/request/write/register/raw/single` | HA->FW | nothing in repo (README documents it) | FW | `registerAddress`, `value` | on demand |
| `Alpha2MQTT/response/write/register/raw/single` | FW->HA | FW | nothing in repo | generic response keys | per request |
| `Alpha2MQTT/request/read/register/handled/all` | HA->FW | nothing in repo (README documents it) | FW | `start`, `end` (array indexes, dec) | on demand |
| `Alpha2MQTT/response/read/register/handled/all` | FW->HA | FW | nothing in repo | JSON of `mqttName: value` pairs | per request |
| `Alpha2MQTT/request/#` | HA->FW | any | ctl `_on_request`, only when the step log is on | logged raw | passive |
| `homeassistant/status` | HA->FW | HA MQTT integration birth and will (not in repo) | FW (`MQTT_SUB_HA_STATUS`) | `online` or `offline`, exact bytes | HA start and stop |

## Request parsing (firmware `mqttCallback`)

- Hand-rolled parser. Names keep only `[A-Za-z0-9]`. Values keep only `-`, `x`, `0-9`, `a-f`, `A-F`.
  A `.` is stripped, so `"watts": 1500.0` would parse as `15000`. The controller and package scripts send integers.
- A name or value over 31 chars fails the request as `invalidMQTTPayload`.
- `powerOnly` is honoured only when its cleaned value is exactly `1`. `true` cleans to `e` and is ignored.
- `watts` over 32000 or `socPercent` over 105 is refused with `failureDetail: "range"` (`dispatchRequestValid`).
  Negative numbers wrap through `strtoul` and are refused the same way. A missing key gives `failureDetail: "missing"`.
- An empty payload gives the generic response (`responseStatus: "noMQTTPayload"`, `end`) with no `seq`.
- Any `set/*` request calls `idleHoldCommand`, which marks HA online and cancels a firmware idle hold.

## Response values

- `responseStatus` on success: `setChargeSuccess`, `setDishargeSuccess` (sic, missing "c"), `setNormalSuccess`.
  The controller tests `"success" in responseStatus.lower()`, so the typo is harmless there.
- On failure `responseStatus` is the Modbus status text (`noResponse`, `slaveError`, `invalidFrame`, and so on) and
  `failureDetail` names the register, for example `REG_DISPATCH_RW_ACTIVE_POWER_1`.
- `write` is `power` or `block`. `frames` is 1 for the single 9 register frame, more for the five write fallback.
- `seq` echoes the request, 0 if none. `ms` is Pico `millis()` after the writes.

## Units and signs on the wire

| Key | Unit | Sign or format |
|---|---|---|
| `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1` | W | + import, - export |
| `REG_BATTERY_HOME_R_BATTERY_POWER` | W | + discharge, - charge |
| `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1` | W | + generating. A lone 0 after a reading 10 W or more from 0 is left out (`unconfirmedZero`) |
| `REG_BATTERY_HOME_R_SOC` | % | float, `%0.02f` |
| `REG_DISPATCH_RW_DISPATCH_START` | none | string `"Start"`, `"Stop"` or `"Unknown"` (lookup) |
| `REG_DISPATCH_RW_DISPATCH_MODE` | none | lookup string, for example `"State of charge control"` |
| `REG_DISPATCH_RW_ACTIVE_POWER_1` | raw | 32000 offset kept: charge reads 32000 - W, discharge 32000 + W |
| `REG_DISPATCH_RW_DISPATCH_SOC` | % | converted back from 0.4 %/bit |
| `REG_CUSTOM_LOAD` | W | firmware computes pv + grid + battery (signed sum, `RegisterHandler::readHandledRegister`) |
| `REG_CUSTOM_GRID_CURRENT_A_PHASE` | A, integer | phase A power / phase A voltage. `Definitions.h` comment says 0.1 A; the code divides by volts |
| `REG_CUSTOM_TOTAL_SOLAR_POWER` | W | sum from `readTotalSolarPower` |
| Request `watts` | W | always positive; the topic picks the direction. Firmware writes 32000 - W for charge, 32000 + W for discharge |
| Request `socPercent` | % | integer; firmware rounds up to the 0.4 % register; 101 to 105 are register steps past full (251 to 255) |
| Controller `signed_w` | W | + discharge, - charge, same as battery power |

## The HA package (`Home Assistant/alpha2mqtt.yaml`)

- Holds only `mqtt.sensor`, one `mqtt.binary_sensor` and three `script`s. No `template:`, no `automation:`.
- 42 sensors and 1 binary sensor, all on device identifier `alpha2mqtt`. Unique ids are `alpha2mqtt_<register>`.
  Entity ids are not written in the file. The recorder comment's glob `sensor.alpha2mqtt_*_fast` implies ids
  like `sensor.alpha2mqtt_grid_power_fast` (guess, from HA naming).
- `expire_after`: 60 s on ten second sensors, 300 s on minute and device sensors, 10800 s on hour sensors,
  10 s on fast sensors. Fast sensors have no `state_class`, so they keep no long term statistics.
- Energy totals use `state_class: total_increasing` for the Energy dashboard. All eight keep `this.state`
  when the new value is missing, 0, lower, or more than 100 kWh higher. On 2026-10-21 one 0 and one value
  copied from another register each booked over 6000 kWh into the battery statistics. Why the firmware
  published them is unknown.
- Response sensors take `value_json.responseStatus` as state and the whole JSON as attributes.
- Inverter Power maps 32768..65535 to negative: the inverter fills only the low word of 0x0406 with a
  signed 16-bit value (62511 read while charging 3025 W; inferred from values, not seen on the wire).
- Battery SOH reads 0 on the SMILE-B3 always, so 0 becomes unknown.
- PV Meter Power and PV Meter Power Fast keep `this.state` when the key is missing,
  for at most 60 s and 10 s. Reason in the comment and in 70fe0e2: an unavailable PV meter takes the net demand,
  and with it the controller, down.
- Pico VSYS: `vsys_mv / 1000`, rounded to 2 places, in V. Pico USB Power: `ON` when `usb_power` is true.
- Scripts `alpha2mqtt_set_charge` and `alpha2mqtt_set_discharge`: fields `watts` (default 3000),
  `soc_percent` (100 charge, 10 discharge), `duration` (default 14400 s). No `seq`, no `powerOnly`.
  The defaults only fill the UI form; a run missing any field stops before publishing.
  `alpha2mqtt_set_normal` sends `{"void": 0}`.
- No package entity feeds the controller. The controller reads `state/fast` itself.

## What the controller needs from outside the repo

| Config key | Default | Defined in repo? | What it reads |
|---|---|---|---|
| `state_topic` | `Alpha2MQTT/state/fast` | firmware | keys `GRID_KEY`, `BATTERY_KEY`, `SOC_KEY`, `seq`; logs the rest of `FAST_FIELDS` |
| `base_topic` | `Alpha2MQTT` | firmware | publishes `request/set/{kind}`, subscribes `response/set/+` |
| `policy_entity` | `sensor.battery_auto_action` | no, only in tests | state `charge`, `discharge`, `hold`, `normal`; attributes `soc_target`, `fixed`, `watts`, `both`, `other_soc`, `grid_offset` |
| `correction_entity` | none | no (`sensor.net_demand` in tests) | number in W; optional attribute `seq` matching the fast message |
| `grid_scale_entity`, `load_entity`, `max_charge_entity`, `max_discharge_entity` | none | no | numbers |

Grid estimate (`estimate_grid` in `regulator.py`, positive on import):

- `grid_source: clamp`: `scale * fast grid + correction`. Correction is 0 when no entity is set.
- `grid_source: estimate`: `correction - fast battery power`. Correction older than 90 s (`CORRECTION_MAX_AGE_S`) is a gap.
- Readings over 20000 W or not finite are dropped (`MAX_PLAUSIBLE_W`).

## Who decides what

| Decision | Owner |
|---|---|
| Watts, direction and SOC each step; keepalive; release on disable or stale input | ctl `Regulator` |
| What is allowed (charge, discharge, hold, fixed, normal, limits) | the policy entity, outside the repo (unknown) |
| Power-only vs full block write | firmware `dispatchPowerOnly`, when the request says `powerOnly: 1` |
| Lease length | request `duration`: ctl 120 s (`Settings.duration_s`), scripts 14400 s by default |
| Battery idle while HA is gone | firmware `holdIdleWhileHaGone`: charge 0 W to 100 % for 120 s, rewritten every 60 s, after 180 s gone |
| Hiding a lone PV 0 | firmware `readPvMeterPower`; pkg PV sensors bridge the gap |
| House load figure | firmware `REG_CUSTOM_LOAD`; the controller does not use it |

Writers of the dispatch block, any of which can override another:

1. Controller (`_publish`), every command.
2. Package scripts `alpha2mqtt_set_*`, by hand.
3. Firmware idle hold, only while HA counts as gone.
4. Raw register writes to 0x0880 to 0x0888.

The controller does not watch for other writers. It logs them through `request/#` when the step log is on, and
its next command or keepalive overwrites them. A raw write or `set/normal` clears the firmware's `_activeDispatch`,
so the next charge or discharge writes the full block.

## Mismatches

- Published, no consumer in repo: `response/read/register/*`, `response/write/register/*`.
- Firmware subscribes to `request/read/register/handled`, `request/read/register/raw`, `request/write/register/raw/single`,
  `request/write/register/raw/data` and `request/read/register/handled/all`; nothing in repo publishes them. README documents all five.
- `state/day/one` has an empty array since 99a1bdf, so it is never published. `state/minute/five` carries battery keys since c2e334d.
- `haIdle` in the fast state: no consumer in the package or controller.
- Fast `REG_DISPATCH_RW_DISPATCH_START`: the controller logs it in the step log but never acts on it.
- Response `frames` and `ms`: the controller ignores both; it times acks with its own monotonic clock.
- README set/charge example omits `frames`, which the firmware sends on every block write.
- `setDishargeSuccess` typo is in the firmware and the README alike.
- Response topic defines are named `MQTT_SUB_RESPONSE_SET_*` though the firmware publishes them, not subscribes.
- Package "Dispatch Power" has unit W but shows the raw 32000 offset value.

## History of the package (`git log -- "Home Assistant/alpha2mqtt.yaml"`)

- 2fba12f: package added. Sensors read `Alpha2MQTT/state/*` directly, so Node-RED is no longer needed. Scripts publish set requests.
- 99a1bdf: energy totals moved to the one minute schedule; five minute and day schedules emptied; hybrid only PV1 to PV6 reads dropped.
- a01e05d: `state/device` with VSYS and USB power; two diagnostic sensors.
- 518f8e5: fast state sensors added, without `state_class`, with a recorder exclude hint.
- 70fe0e2: PV meter sensors hold the last value through a left out lone 0; controller holds through short gaps.

## Gotchas

- A script charge runs 14400 s by default. With the controller off, nothing ends it early except `set/normal`.
- With the controller on, a script command lasts only until the controller's next command or keepalive.
- HA's birth is not retained by default. The firmware treats any `set/*` request as HA being up to cover that.
- The firmware leaves failed reads out of the JSON, so every package template uses `| default(None)`.
  The controller drops a fast message that lacks grid or battery power, and keeps the last SOC when SOC is missing.

## Open questions

- Where `sensor.battery_auto_action` and the net demand correction are defined, and their exact templates: unknown, not in this repo.
- Whether the deployed HA has the birth and will retained, as the README advises: unknown.
- Whether any automation outside the repo calls the `alpha2mqtt_set_*` scripts while the controller is on: unknown.
