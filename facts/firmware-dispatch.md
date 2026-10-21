# Firmware command path: MQTT requests, dispatch writes, idle hold, PV meter zero filter

Last verified: 2026-10-21, past-full SOC 101 added; the Pico W build compiles. `tests/firmware/run` passed: test_dispatch, test_idle_hold, test_json_members, test_modbus_frame, test_pv_meter, test_registers, test_timer all print OK, exit 0.

## Files and symbols

| Symbol | File | Role |
|---|---|---|
| `mqttCallback` | `Alpha2MQTT/Alpha2MQTT.ino` | Parses every request, writes, publishes the response |
| `writeFullDispatchBlock` | same | One frame write, five write fallback, refused frame latch |
| `writeDispatchStop` | same | Writes 0 to 0x0880; shared by `set/normal` and idle release |
| `holdIdleWhileHaGone` | same | Called once per `loop()` pass; writes the idle hold or its release |
| `closeSetResponse` | same | Appends `seq` and `ms` to every set response |
| `addStateInfo` | same | Clears `_activeDispatch` when a read of 0x0880 is not 1 |
| `_activeDispatch`, `_blockFrameRefused`, `_idleHold` | same | Globals, RAM only, reset by reboot |
| `requestPayloadFits`, `dispatchRequestValid`, `dispatchSocFromPercent`, `dispatchPowerOnly`, `dispatchBlockFrame` | `Alpha2MQTT/Dispatch.h` | Pure helpers, host tested |
| `IdleHold`, `idleHoldStatus`, `idleHoldCommand`, `idleHoldDue`, `idleHoldWritten`, `haGone`, `haStatusFromPayload` | `Alpha2MQTT/IdleHold.h` | Pure state machine, host tested |
| `pvMeterZeroUnconfirmed` | `Alpha2MQTT/PvMeter.h` | Pure filter, used by `RegisterHandler::readPvMeterPower` |
| `RegisterHandler::writeDispatchBlock`, `writeRawDataRegister` | `Alpha2MQTT/RegisterHandler.cpp` | Build frames, call `sendModbus` |

## Topics

`<device>` is `DEVICE_NAME`, default `Alpha2MQTT` (`Definitions.h`). `mqttReconnect` subscribes to all eight request topics plus `homeassistant/status` (`MQTT_SUB_HA_STATUS`, not under `<device>`, overridable by `#define`).

| Request suffix | Keys used | Response suffix |
|---|---|---|
| `/request/set/charge` | `watts`, `socPercent`, `duration` required; `seq`, `powerOnly` optional | `/response/set/charge` |
| `/request/set/discharge` | same | `/response/set/discharge` |
| `/request/set/normal` | `seq` optional; payload must be non-empty | `/response/set/normal` |
| `/request/read/register/handled` | `registerAddress` (hex) | `/response/read/register/handled` |
| `/request/read/register/raw` | `registerAddress` (hex), `dataBytes` (decimal, registers = dataBytes/2) | `/response/read/register/raw` |
| `/request/write/register/raw/single` | `registerAddress` (hex), `value` (decimal, 16 bit) | `/response/write/register/raw/single` |
| `/request/write/register/raw/data` | `registerAddress` (hex), `dataBytes` (2 or 4), `value` (decimal) | `/response/write/register/raw/data` |
| `/request/read/register/handled/all` | `start`, `end` (decimal indexes into `_mqttAllHandledRegisters`, end clamped) | `/response/read/register/handled/all` |
| `homeassistant/status` | raw `online` or `offline`, exact bytes | none |

## Parsing and validation (`mqttCallback`)

Order of checks:
1. `requestPayloadFits`: length over 127 (`MQTT_REQUEST_PAYLOAD_MAX`) is dropped silently. No response. Applies to the status topic too. (3637105)
2. Status topic: `idleHoldStatus(haStatusFromPayload(...))`, return. No write here.
3. Unknown topic: `notValidIncomingTopic`, no response published.
4. Empty payload: `noMQTTPayload`, generic response, nothing written.
5. Hand rolled JSON parser: splits on `:`, `,`, `{`, `}`. A key or value over 31 chars fails the whole request as `invalidMQTTPayload` (1ff3b82).
6. Key cleaning keeps only `[A-Za-z0-9]`. Value cleaning keeps only `-`, `x`, `0-9`, `a-f`, `A-F`. Then `strtoul`.
7. Side effects before any branch runs, whatever the result so far:
   - raw single, raw data or `set/normal` topic: `_activeDispatch.valid = false`.
   - `set/charge`, `set/discharge`, `set/normal` topic: `idleHoldCommand(_idleHold)`.

Charge and discharge checks, in order. Both refusals write nothing:

| Condition | Response `responseStatus` | `failureDetail` |
|---|---|---|
| `watts`, `socPercent` or `duration` missing | `invalidMQTTPayload` | `missing` |
| `!dispatchRequestValid(watts, socPercent)`: watts > 32000 or SOC > 105, checked as unsigned long before narrowing | `invalidMQTTPayload` | `range` |

`duration` has no range check. It is a `uint32_t` from `strtoul`.

## Encoding of the dispatch block

Registers from `Definitions.h`. Function code 0x10 (`MODBUS_FN_WRITEDATAREGISTER`), slave `ALPHA_SLAVE_ID` 0x55.

| Register | Name | Width | Firmware value |
|---|---|---|---|
| 0x0880 | `REG_DISPATCH_RW_DISPATCH_START` | 1 reg | 1 start (`DISPATCH_START_START`), 0 stop |
| 0x0881-0x0882 | `REG_DISPATCH_RW_ACTIVE_POWER_1` | 2 regs | 32000 + W discharge, 32000 - W charge |
| 0x0883-0x0884 | `REG_DISPATCH_RW_REACTIVE_POWER_1` | 2 regs | 32000 (zero), frame path only |
| 0x0885 | `REG_DISPATCH_RW_DISPATCH_MODE` | 1 reg | always 2, SOC control |
| 0x0886 | `REG_DISPATCH_RW_DISPATCH_SOC` | 1 reg | `dispatchSocFromPercent(pct)` = (pct*5+1)/2, 0.4 %/bit, rounded up; above 100 it is 150 + pct, so 101 to 105 send 251 to 255 |
| 0x0887-0x0888 | `REG_DISPATCH_RW_DISPATCH_TIME_1` | 2 regs | duration in seconds, 1 s/bit |

- Sign: `multiplier` is -1 for charge, +1 for discharge. 0 W in either kind is 32000.
- SOC examples pinned by tests: 0 to 0, 1 to 3, 10 to 25, 25 to 63, 90 to 225, 100 to 250, 101 to 251, 105 to 255. Never below the target, under one step above (3637105).
- Charge 2200 W is 29800. 14400 s spans both time registers.

Dispatch mode lookup (`Note7` in `Definitions.h`). Only mode 2 is ever written. Meanings beyond the names: unknown.

| Value | Name |
|---|---|
| 1 | Battery only charged via PV |
| 2 | State of charge control (used) |
| 3 | Load following |
| 4 | Maximise output |
| 5 | Normal mode |
| 6 | Optimise consumption |
| 7 | Maximise consumption |
| 8 | ECO mode |
| 9 | FCAS mode |
| 10 | PV power setting |

## Write paths for set/charge and set/discharge

`dispatchPowerOnly(allowed, last, charge, socReg, durationS, nowMs)` decides first. True only when all hold:
- payload `powerOnly` cleans to exactly `1`;
- `_activeDispatch.valid`;
- same kind (charge vs discharge), same SOC register value, same duration;
- `(now - writtenMs) / 1000 < durationS / 2`, wrap safe. A duration under 2 s never qualifies.

Half lease replaced a third in 9452d4a. Introduced in 1a09f99.

`_activeDispatch.valid` is cleared before any write and restored only on full acknowledgement.

Power only path: one `writeRawDataRegister(0x0881, power)` with 2 registers. Success restores the previous `_activeDispatch` unchanged, so `writtenMs` is not renewed. Response `"write": "power"`.

Full block path, `writeFullDispatchBlock` (9c3a983 moved it out of `mqttCallback`):
1. If `_blockFrameRefused` is false: one 27 byte frame from `dispatchBlockFrame` (slave, 0x10, 0x0880, 9 regs, 18 bytes, start, power, reactive 32000, mode 2, SOC, time, CRC). About 50 ms at 9600 baud. Success sets `_activeDispatch = {true, charge, soc, duration, writeStartedMs}`. `frames` = 1.
2. On any frame failure, or when the latch is set: five single writes, 400 ms apart (`delay(RS485_TRIES * 50)`, `RS485_TRIES` = 8). `frames` = 5. Order is fixed; the comment says no other is proven on hardware:
   START 0x0880 = 1, ACTIVE_POWER 0x0881 (2 regs), TIME 0x0887 (2 regs), SOC 0x0886, MODE 0x0885 = 2.
3. The first failing single write returns at once and names its register in `failureDetail`. Earlier writes in the sequence stay on the inverter.
4. Latch: `_blockFrameRefused` becomes true only if the frame got `slaveError` and all five singles then landed (d49930f). A timeout never sets it (2f5e1c7). Only a reboot clears it.

The fallback does not write reactive power 0x0883.

## Responses

Set responses are built by hand and end with `closeSetResponse`:

```
{ "responseStatus": ..., "failureDetail": ..., ["write": "power"] | ["write": "block", "frames": 1|5], "seq": N, "ms": M }
```

| Case | responseStatus | failureDetail | write keys |
|---|---|---|---|
| charge ok | `setChargeSuccess` | `""` | yes |
| discharge ok | `setDishargeSuccess` (sic, missing c) | `""` | yes |
| missing / range | `invalidMQTTPayload` | `missing` / `range` | no |
| power only write failed | Modbus status of that write | `REG_DISPATCH_RW_ACTIVE_POWER_1` | yes |
| block failed | Modbus status of the last write tried | `REG_DISPATCH_RW_<name>` of the failing single write | yes |
| normal ok | `setNormalSuccess` | `""` | no |
| normal failed | Modbus status | `REG_DISPATCH_RW_DISPATCH_START` | no |

- `seq` is `strtoul(seq, 10)`, 0 when absent (c86f28c). `ms` is `millis()` after the writes.
- Refusals carry `seq` so the sender fails the command at once (5f0aff3).
- A key or value too long, or an empty payload, on a set topic gets the generic response instead, with no `seq`.
- Generic response (raw and read topics, and the two failures above): `responseStatus`, then when present `registerAddress`, `functionCode`, `registerName`, `dataType`, `dataValue`, `formattedDataValue`, `slaveErrorCode`, `rawDataSize`, `rawData`, and always `"end": "true"`.
- `handled/all` returns an object of register name to value, no `responseStatus`.
- Responses are built in `_commandPayload`, since a callback can run inside a schedule via `serviceBetweenReads`.

## set/normal

`writeDispatchStop`: clears `_activeDispatch`, writes 0 to 0x0880 as one register. Mode, power, SOC and time are left as they were. Success is reported only when the write succeeded (7916bb4).

## Failure behaviour

| Event | Behaviour |
|---|---|
| Modbus write fails | No retry inside the firmware. `sendModbus` has no write retry; timeout is `RS485_TRIES * 50` = 400 ms. `_activeDispatch` stays invalid, so the next request writes the full block. The sender must resend. |
| Partial five write sequence | Registers written before the failure stay set. Nothing rolls back. |
| Malformed request | Nothing written. Set topics with missing or out of range fields get a `seq` refusal. Other malformed requests get the generic `invalidMQTTPayload` or `noMQTTPayload` response. Oversized or unknown topic: silence. |
| Dispatch expiry | The inverter ends dispatch itself after `duration` seconds. The firmware never renews a controller dispatch. The fast state reads 0x0880 every second (`_mqttFastStatusRegisters`); any value other than 1 clears `_activeDispatch` (c90b7f3), so the next command writes the full block. |
| Power only write ignored by inverter | Unknown. Not documented by AlphaESS and untested on hardware per 1a09f99 and `README.md`. |

## IdleHold

Problem: when Home Assistant stopped, the battery fell back to the inverter's own self consumption mode. Now it sits idle (5bd64cc). Window raised from 60 s to 180 s because a planned HA restart is back in about two minutes (3ed37e9).

Constants: `HA_GONE_AFTER_MS` 180000, `IDLE_HOLD_DURATION_S` 120, `IDLE_HOLD_REWRITE_MS` 60000, `IDLE_HOLD_RETRY_MS` 10000.

Hold write: `writeFullDispatchBlock(charge=true, power 32000, SOC register 250, 120 s)`, so 0 W charge to 100 %. Solar surplus exports while held. Release write: `writeDispatchStop`, the same as `set/normal`.

Gone rules (`haGone` = `offlineGone || mqttGone`):
- `offlineGone`: status `offline` and more than 180 s since the first will. A repeat will does not restart the clock. Cleared by `online` or a command.
- `mqttGone`: broker down more than 180 s since the last pass that saw it connected, or since boot. Reconnect attempts do not restart it. Cleared by a connected pass.
- Both latch, so absences longer than a `millis()` wrap stay gone.
- A reconnect does not clear `offlineGone`. Only `online` or a command does.

`idleHoldDue` result each pass:

| State | Result |
|---|---|
| gone | `hold` now, then every 60 s after an acknowledged write, every 10 s after a failed one |
| not gone and `held` | `release` now, retried every 10 s until acknowledged |
| otherwise | `none` |

- `held` is set by any hold attempt, even a failed one, since it may have landed in part. Cleared only by an acknowledged release or a command.
- `idleHoldCommand` (any set topic, even a malformed one): status online, clears both gone flags, `held` and `tried`. No release is written over the command.
- `haStatusFromPayload`: exact `online` or `offline`, length bounded, anything else is `unknown` and ignored.
- `holdIdleWhileHaGone` runs only from `loop()`, at most one write per pass, never waits. Not from `serviceBetweenReads`.
- The hold sets `_activeDispatch` to charge, 250, 120 s. A later command with `powerOnly` and the same kind, SOC and duration could therefore go out power only.
- Fast state key `haIdle` is 1 while `haGone`.
- `README.md` advises retained HA birth and will so a hold survives a Pico reboot and ends after a missed birth.

## PvMeter

`pvMeterZeroUnconfirmed(previous, value)`: true when `value == 0` and `|previous| >= 10` (`PV_METER_ZERO_CONFIRM_FROM_W`). `readPvMeterPower` then returns `unconfirmedZero` and the register is left out of that payload. `_lastPvMeterPower` is updated to 0 anyway, so a second 0 publishes. Why: the inverter sometimes returns a valid frame holding exactly 0 for one read while PV generates; real readings reach 0 through small values (672066a). `readTotalSolarPower` starts with this read, so an unconfirmed 0 also drops the totals built on it. Read path only; no effect on dispatch.

## Invariants pinned by tests

- 127 bytes fit, 128 rejected (`test_dispatch.cpp`).
- 32000 W and 105 accepted; 32001 W, 106, 65636 and a negative watts parsed as unsigned refused.
- SOC register is never below the requested percent and less than one step above it, for 0 to 100.
- Power only: true at 59.999 s of a 120 s lease, false at 60 s; false on kind, SOC or duration change, cleared block, `allowed` false; wrap safe.
- Exact 27 byte frame layout for discharge 150 W, SOC 25, 120 s.
- `IDLE_HOLD_DURATION_S * 1000 == 2 * IDLE_HOLD_REWRITE_MS`; retry gap shorter than rewrite.
- 180 s exactly is not gone; 180001 ms is. Holds, releases, retries and latches across a `millis()` wrap (`test_idle_hold.cpp`).
- PV zero: held after |prev| >= 10, published after a 0 or |prev| <= 9 (`test_pv_meter.cpp`).

## Gotchas

- Decimal points are stripped by the value cleaner: `"watts": 150.5` becomes 1505.
- `"powerOnly": true` cleans to `e`, so it does not enable power only. Send `1`.
- `"watts": "0x10"` parses as 0 (base 10 `strtoul` stops at `x`).
- Discharge success string is `setDishargeSuccess`. Do not fix it without updating every consumer.
- `set/normal` with an empty payload writes nothing and still clears `_activeDispatch` and counts as an HA command. Send `{}` or `{"seq": N}`.
- Any raw write, to any register, clears `_activeDispatch`.
- `writeRawDataRegister` builds a frame only for `dataBytes` 2 or 4. Other values send nothing; the reported status then is unknown.
- `README.md` example responses omit `frames`; the code always adds it on the block path.
- A power only write does not renew the lease timestamp; the controller must send a full block before half the duration passes.

## Open questions

- Does the inverter apply a power only write mid dispatch, and does it restart its timer? Unknown.
- Inverter behaviour for `duration` 0 or very large values: unknown. No firmware check.
- Does mode 2 end dispatch when the SOC target is reached, or hold? The vendor dispatch guide says the charge stops at the target and the battery idles for the rest of the duration. Live on the B3: register 250 stopped at 99.2 %, with battery power at 0 W within one sample while the BMS still allowed full charge current; 251 stopped at 99.6 %, cells 3.42 V; 252 is full; 255 charges on until the BMS ends it. So the 0.4 % scale is wrong at the top, and 100 % sent as 250 left the battery short of full for days. The controller sends a charge to 100 % as its Full charge register setting (252, socPercent 102). 105 (255) is the balance target.
- Effect of the fallback leaving reactive power 0x0883 unwritten: unknown.
