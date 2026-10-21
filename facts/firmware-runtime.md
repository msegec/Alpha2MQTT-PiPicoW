# Firmware runtime and state publishing

One single threaded Arduino loop on the Pico W. It polls the inverter on fixed schedules, publishes JSON state over MQTT and runs commands inline, between bus reads.

Last verified: working tree on 75e08a8 plus the 2026-10-21 cleanup. Sources: `Alpha2MQTT/Alpha2MQTT.ino`, `Alpha2MQTT/Timer.h`, `Alpha2MQTT/IdleHold.h`, `Alpha2MQTT/JsonMembers.h`, `Alpha2MQTT/Definitions.h`, `Alpha2MQTT/RS485Handler.cpp`, git history. `tests/firmware/run` passed: `test_dispatch`, `test_idle_hold`, `test_json_members`, `test_modbus_frame`, `test_pv_meter`, `test_registers`, `test_timer`, all `OK`, exit 0.

## Boot sequence (`setup`)

1. `rp2040.wdt_begin(WATCHDOG_TIMEOUT)`, 8000 ms. It comes first, so a hang in setup also reboots.
2. `Serial.begin(9600)` (debug only), LED pin, `delay(100)`.
3. `WiFi.setTimeout(WIFI_JOIN_TIMEOUT)` (3000 ms), then `setupWifi`. `WiFi.begin` blocks for up to about 3 s.
4. OTA: `ArduinoOTA` with hostname `DEVICE_NAME` and password `OTA_PASSWORD`. `onProgress` feeds the watchdog. It starts before any inverter work, so a bad build can always be replaced.
5. MQTT client setup: `setServer`, `_wifi.setTimeout(MQTT_CLIENT_TIMEOUT)` (2500 ms), `setSocketTimeout(MQTT_REPLY_TIMEOUT)` (2 s).
6. Buffer allocation. It tries `MAX_MQTT_PAYLOAD_SIZE + MQTT_HEADER_SIZE` and steps down 1024 bytes at a time. Then it allocates three payload buffers of `_maxPayloadSize`: `_mqttPayload`, `_commandPayload` and `_fastPayload`.
7. `setCallback(mqttCallback)`. Creates `RS485Handler` and `RegisterHandler`.
8. Baud scan: it reads `REG_SAFETY_TEST_RW_GRID_REGULATION` at 9600, 115200, 19200, 57600, 38400, 14400 and 4800, cycling until one answers. It waits `waitServicingOta(1000)` after each failure. This loop never gives up.
9. `getSerialNumber` reads `REG_SYSTEM_INFO_R_EMS_SN_BYTE_1_2`, sets the serial prefix, then `waitServicingOta(4000)`.
10. One `mqttReconnect` attempt.

Boot takes at least about 4.1 s plus the WiFi join. During setup only WiFi and OTA are serviced (`waitServicingOta`). MQTT and commands start in `loop`.

## Loop structure (`loop`, one pass, in order)

| # | Call | Period / trigger | Bus reads |
|---|---|---|---|
| 1 | `feedWatchdog` | every pass | 0 |
| 2 | `setupWifi` | every pass, returns at once unless a begin is due | 0 |
| 3 | `ArduinoOTA.handle` | every pass | 0 |
| 4 | `_mqtt.loop()`, or `mqttReconnect` if not connected or loop failed | every pass | 0 itself; `mqttCallback` runs here |
| 5 | `holdIdleWhileHaGone` | every pass; writes only when `idleHoldDue` says so | 0, or one dispatch write |
| 6 | `sendFastState` | `STATUS_INTERVAL_FAST` 1000 ms | 5 |
| 7 | `flashLed` | `RUNSTATE_INTERVAL` 5000 ms; a 4 ms LED pulse | 0 |
| 8 | `sendData` | runs each slow schedule that is due, in order: 10 s, 1 min, 5 min, 1 h, 1 day | see schedules |
| 9 | `FORCE_RESTART` reboot (`rp2040.reboot`) | `FORCE_RESTART_HOURS`, only if `FORCE_RESTART` is defined (off by default) | 0 |

`serviceBetweenReads` runs between consecutive registers of a slow schedule. It is not called after the last one. It calls `feedWatchdog`, `ArduinoOTA.handle`, `_mqtt.loop()` and `sendFastState`. The fast state and commands therefore cut into slow schedules (4fb5457, 1d2e5fc).

## Timers (`Timer.h`, `checkTimer`)

- `timerDue(lastRun, now, interval)` uses the unsigned elapsed time, so it stays correct across the 49.7 day `millis()` wrap (73d2899).
- A late run keeps the phase: `lastRun = now - elapsed % interval`. It allows at most one catch up run, and missed whole intervals are dropped (9452d4a). After a stall, the next fast sample can come less than 1 s after the late one.
- Every `lastRun` starts at 0, so each schedule is phased to multiples of its interval from boot. The first hour publish comes 1 h after boot. The first day publish would come 24 h after boot, but that schedule is empty.
- At t = 60 s, the 10 s and 1 min schedules fall due in the same pass and run back to back.
- `retryDue(tried, lastTry, now, interval)` spaces the reconnects and idle hold retries. The first try is due at once.
- `test_timer.cpp` covers: not due early, due at interval, phase kept after 300 ms late, one catch up after several intervals, wrap, `lastRun + interval` overflow, zero interval, retry spacing across the wrap.

## Published topics

All go through `sendMqtt`, which calls `_mqtt.publish(topic, payload)`: not retained, QoS 0. Topics are `DEVICE_NAME` (default `Alpha2MQTT`) plus a suffix. The payload is JSON with `\r\n` line breaks and 4 space indent. A register's key is its macro name, from `rs->mqttName`. A value is `dataValueFormatted`, quoted when the type is character or the register has a lookup (`hasLookup`). There is no LWT, birth, availability or heartbeat topic: `_mqtt.connect` passes no will. The client ID is `DEVICE_NAME`. Keepalive is not set in code. The locally installed PubSubClient 2.8 defaults `MQTT_KEEPALIVE` to 15 s.

| Topic suffix | Define | Trigger | Bus reads (all succeed) |
|---|---|---|---|
| `/state/fast` | `MQTT_MES_STATE_FAST` | every 1000 ms | 5 |
| `/state/second/ten` | `MQTT_MES_STATE_SECOND_TEN` | every 10000 ms | about 33 (guess: 15 single + 2 + 9 + 7) |
| `/state/minute/one` | `MQTT_MES_STATE_MINUTE_ONE` | every 60000 ms | 11 |
| `/state/device` | `MQTT_MES_STATE_DEVICE` | every 60000 ms, right after the minute schedule | 0 (ADC) |
| `/state/minute/five` | `MQTT_MES_STATE_MINUTE_FIVE` | every 300000 ms; list empty, so never published | 0 |
| `/state/hour/one` | `MQTT_MES_STATE_HOUR_ONE` | every 3600000 ms | 5 |
| `/state/day/one` | `MQTT_MES_STATE_DAY_ONE` | every 86400000 ms; list empty, so never published | 0 |
| `/response/...` | see `mqttCallback` facts | once per request | per request |

### `/state/fast` (`sendFastState`, list `_mqttFastStatusRegisters`)

Keys, in order:

| Key | Format |
|---|---|
| `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1` | int W, minus = feeding, plus = drawing |
| `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1` | int W, positive = generating |
| `REG_BATTERY_HOME_R_BATTERY_POWER` | int W, minus = charge, plus = discharge |
| `REG_BATTERY_HOME_R_SOC` | float percent, `%0.02f` (raw x 0.1) |
| `REG_DISPATCH_RW_DISPATCH_START` | string `"Start"`, `"Stop"` or `"Unknown"` |
| `seq` | unsigned, +1 per message from boot, restarts at 1 |
| `ms` | `millis()` when the reads started |
| `read_ms` | ms the five reads took |
| `haIdle` | `1` while `haGone(_idleHold)`, else `0` |

- A register that fails to read is left out. The stamp is always the last member and closes the object.
- A PV meter 0 straight after a reading at least 10 W from 0 fails as `unconfirmedZero` and is left out (`pvMeterZeroUnconfirmed`, `PV_METER_ZERO_CONFIRM_FROM_W`). `_lastPvMeterPower` is shared by every PV meter read, in any schedule.
- It publishes even while MQTT is down. The publish fails silently and `seq` still counts, so a consumer sees a `seq` gap.
- Consumer: `controller.py` `DEFAULT_STATE_TOPIC` and `FAST_FIELDS` read `seq`, `ms`, `read_ms` and the five register keys. `Home Assistant/alpha2mqtt.yaml` also reads it.

### `/state/second/ten` (`_mqttTenSecondStatusRegisters`)

Keys: `REG_BATTERY_HOME_R_SOC`, `REG_BATTERY_HOME_R_BATTERY_POWER`, `REG_BATTERY_HOME_R_VOLTAGE`, `REG_BATTERY_HOME_R_CURRENT`, `REG_BATTERY_HOME_R_MAX_CELL_TEMPERATURE`, `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1`, `REG_CUSTOM_GRID_CURRENT_A_PHASE` (2 reads), `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1`, `REG_INVERTER_HOME_R_CURRENT_L1`, `REG_INVERTER_HOME_R_POWER_L1_1`, `REG_INVERTER_HOME_R_INVERTER_TEMP`, `REG_CUSTOM_LOAD` (PV meter + 6 PV strings + grid + battery, 9 reads), `REG_DISPATCH_RW_DISPATCH_START`, `REG_DISPATCH_RW_DISPATCH_MODE`, `REG_DISPATCH_RW_ACTIVE_POWER_1`, `REG_DISPATCH_RW_DISPATCH_SOC`, `REG_DISPATCH_RW_DISPATCH_TIME_1`, `REG_CUSTOM_TOTAL_SOLAR_POWER` (PV meter + 6 strings, 7 reads).

### `/state/minute/one` (`_mqttOneMinuteStatusRegisters`)

Keys: `REG_GRID_METER_R_VOLTAGE_OF_A_PHASE`, `REG_PV_METER_R_VOLTAGE_OF_A_PHASE`, `REG_INVERTER_HOME_R_VOLTAGE_L1`, `REG_BATTERY_HOME_R_BATTERY_CHARGE_ENERGY_1`, `REG_BATTERY_HOME_R_BATTERY_DISCHARGE_ENERGY_1`, `REG_BATTERY_HOME_R_BATTERY_ENERGY_CHARGE_FROM_GRID_1`, `REG_SYSTEM_OP_R_SYSTEM_TOTAL_PV_ENERGY_1`, `REG_GRID_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1`, `REG_GRID_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1`, `REG_PV_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1`, `REG_PV_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1`. The energy totals moved here from daily and five minute schedules in 99a1bdf.

### `/state/device` (`sendDeviceState`)

`{"vsys_mv": <unsigned mV>, "usb_power": true|false}`. VSYS comes from 16 ADC samples on GPIO29 (`PICO_VSYS_PIN`) taken under `cyw43_thread_enter`. USB comes from `CYW43_WL_GPIO_VBUS_PIN` (a01e05d).

### `/state/hour/one` (`_mqttOneHourStatusRegisters`)

Keys: `REG_GRID_METER_R_FREQUENCY`, `REG_PV_METER_R_FREQUENCY`, `REG_INVERTER_HOME_R_FREQUENCY`, `REG_SYSTEM_OP_R_SYSTEM_FAULT_1`, `REG_BATTERY_HOME_R_BATTERY_FAULT_1`.

### Overflow

If a payload would exceed `_maxPayloadSize - 1`, `addToPayload` replaces it with `{"mqttError": "Length of payload exceeds N bytes.  Length would be M bytes."}`. That object is still published.

## Subscriptions (made in `mqttReconnect`)

There are 8 under `DEVICE_NAME` (`MQTT_SUB_REQUEST_*`: read handled, read raw, set charge, set discharge, set normal, write raw single, write raw data, read handled all), plus `MQTT_SUB_HA_STATUS` (`homeassistant/status`, not under `DEVICE_NAME`). If any subscribe fails, the client disconnects and retries, because `loop` only reconnects a dropped connection (66e7f49).

## How requests interleave with polling

- `mqttCallback` runs only inside `_mqtt.loop()`. That happens at step 4 of `loop` and in `serviceBetweenReads` between slow schedule reads. PubSubClient 2.8 (installed locally) reads one packet per `loop()` call, so queued requests are handled one per service point.
- The callback runs to completion, bus writes included, before the loop resumes. It builds its response in `_commandPayload`, so a half built schedule payload in `_mqttPayload` survives. `sendFastState` uses `_fastPayload` the same way.
- The callback never calls `_mqtt.loop()` or `sendFastState`, so it cannot nest. The fast state pauses for as long as the callback runs.
- `homeassistant/status` is only noted (`idleHoldStatus`). The write it causes happens later in `holdIdleWhileHaGone`, at step 5 of the next pass, never mid schedule.
- `read/register/handled/all` loops over a range of `_mqttAllHandledRegisters` with no servicing. A long range stalls the fast state, MQTT and OTA for its whole run.

## Latency budget

| Item | Figure | Source |
|---|---|---|
| RS485 reply timeout | `RS485_TRIES` x 50 = 400 ms, polled every 1 ms | `checkForData`, 9102d0c |
| Silent inverter, per exchange | about 0.6 s, including line drain up to `RS485_DRAIN_MAX_MS` 150 ms | 458e9a4, `RS485Handler.h` |
| Power only dispatch write | about 30 to 40 ms at 9600 baud | README |
| One frame dispatch block | about 50 ms | 9452d4a |
| Five write fallback | four `delay(RS485_TRIES * 50)` gaps, about 1.8 s | `writeFullDispatchBlock`, 9452d4a |
| Fast state `read_ms` | unknown on hardware; README example shows 142 | README |
| Fast state with all five reads failing | about 3 s (guess: 5 x 0.6 s) | derived |
| Broker attempt | up to 2.5 s connect + 2 s CONNACK + 0.3 s delays; 7 s if `MQTT_SERVER` is a hostname | 66e7f49, 65f3526 |
| WiFi join | up to 3 s, longer if the driver is mid negotiation | 3a6e951 |

- Request pickup: the wait is the time to the next `_mqtt.loop()`. Outside a slow schedule, the worst case is one fast state plus the 4 ms LED pulse. Inside a schedule, it is one register: up to 9 bus reads for `REG_CUSTOM_LOAD`, plus a fast state.
- Fast state jitter: the longest uninterruptible block. That is a five write fallback (about 1.8 s), a broker attempt (up to about 5 s), a WiFi join (3 s or more) or a `handled/all` request. The schedules alone add at most one `REG_CUSTOM_LOAD`.
- Control loop speed limits: the 1 s fast period, the bus time per read at the scanned baud rate, and the inverter's own ramp. 7a07eab (Home Assistant side only) times steps over 500 W to 80 % of the rise and counts steps slower than 4 s, because the inverter can answer a discharge step with a slow ramp. The controller's `ACK_TIMEOUT_S` is 10 s against a block ack of about 2 s.
- Timing history: 4fb5457 service between schedule reads; 1d2e5fc fast state every 1 s; 9102d0c 1 ms reply polling; 99a1bdf dropped PV1 to PV6 from the 10 s list; 9452d4a one frame write and timer phase.

## Failure handling

- **WiFi loss.** `wifiJoined` reads `cyw43_wifi_link_status`, because `WiFi.status()` stays `WL_CONNECTED` while lwIP keeps its address after the access point is lost. A lost link begins again at once. A pending join is retried every `WIFI_RETRY_INTERVAL` 10000 ms. A join with DHCP pending (`WL_DISCONNECTED` while joined) is left alone (86412e8). Polling and the fast state go on meanwhile.
- **MQTT loss.** `mqttReconnect` returns at once while WiFi is down. Otherwise it makes one attempt per `MQTT_RETRY_INTERVAL` 5000 ms. It never blocks in a loop (66e7f49). Publishes fail silently.
- **Home Assistant gone.** 180 s after its will, or 180 s off the broker, the idle hold writes (`IdleHold.h`, 5bd64cc, 3ed37e9). The details belong to the dispatch facts.
- **Modbus timeout.** The register is left out of that payload. Nothing at the runtime level retries it.
- **Reboots.** These happen when the watchdog is not fed for 8000 ms (458e9a4), when an accepted OTA upload completes inside `ArduinoOTA.handle`, every `FORCE_RESTART_HOURS` if `FORCE_RESTART` is defined, and on power loss. An OTA upload that stalls for 8 s reboots into the old image.
- **Watchdog feeds.** Top of `loop`, `waitServicingOta`, `serviceBetweenReads`, start of every `sendModbus`, `sendMqtt`, before each broker attempt, before each `WiFi.begin`, and OTA progress. One feed per exchange, not per byte, so a line that never stops sending still reboots.
- **Inverter absent at boot.** The baud scan loops forever. WiFi and OTA keep being serviced, but there is no MQTT and no publishing.
- **State lost on reboot.** `seq`; `_activeDispatch`, so the next command writes a full block; `_blockFrameRefused`, so the frame is tried again; `_idleHold`, so the broker rule counts from boot; `_lastPvMeterPower`; timer phases; the baud rate, which is rescanned; the serial prefix. Whether the inverter keeps running a dispatch until its TIME lease ends: guess yes, from the lease comments; not verified here.

## Compile time knobs

- The code builds for the Pico W only. There is no board select define.
- `Definitions.h`: `DEVICE_NAME`, `ALPHA_SLAVE_ID`, `MAX_MQTT_PAYLOAD_SIZE`, `MIN_MQTT_PAYLOAD_SIZE`, `MQTT_HEADER_SIZE`, `RS485_TRIES`, `REQUIRE_DELAY_DUE_TO_INCONSISTENT_RETRIEVAL`, `REQUIRED_DELAY_DUE_TO_INCONSISTENT_RETRIEVAL`, `FORCE_RESTART`, `FORCE_RESTART_HOURS`, `WATCHDOG_TIMEOUT`, `MQTT_SUB_HA_STATUS` (`#ifndef`, so it can be overridden), `DEBUG` (on), `DEBUG_LEVEL2`, `DEBUG_OUTPUT_TX_RX`, the `*_MULTIPLIER` scale factors, and the `MQTT_MES_*` and `MQTT_SUB_*` topic suffixes. Without `ARDUINO` (the host tests) it skips `Secrets.h` and `feedWatchdog` does nothing.
- `Alpha2MQTT.ino` (local `#define`s): `RUNSTATE_INTERVAL`, `STATUS_INTERVAL_FAST`, `STATUS_INTERVAL_TEN_SECONDS`, `STATUS_INTERVAL_ONE_MINUTE`, `STATUS_INTERVAL_FIVE_MINUTE`, `STATUS_INTERVAL_ONE_HOUR`, `STATUS_INTERVAL_ONE_DAY`, `WIFI_JOIN_TIMEOUT`, `WIFI_RETRY_INTERVAL`, `MQTT_RETRY_INTERVAL`, `MQTT_CLIENT_TIMEOUT`, `MQTT_REPLY_TIMEOUT`. The schedule lists are the `_mqtt*StatusRegisters` arrays.
- Other headers: `RS485_QUIET_MS` and `RS485_DRAIN_MAX_MS` (`RS485Handler.h`); `HA_GONE_AFTER_MS`, `IDLE_HOLD_DURATION_S`, `IDLE_HOLD_REWRITE_MS`, `IDLE_HOLD_RETRY_MS` (`IdleHold.h`); `MQTT_REQUEST_PAYLOAD_MAX` (`Dispatch.h`); `PV_METER_ZERO_CONFIRM_FROM_W` (`PvMeter.h`).
- `Secrets.example.h` names: `WIFI_SSID`, `WIFI_PASSWORD`, `MQTT_SERVER`, `MQTT_PORT`, `MQTT_USERNAME` (empty means no credentials), `MQTT_PASSWORD`, `OTA_PASSWORD`. A missing or empty `OTA_PASSWORD` fails the build.

## Gotchas

- Fixed in the cleanup: a failed last read no longer leaves invalid JSON. `addStateInfo` puts `jsonMemberSeparator` before each member and the object ends with `jsonObjectEnd` (`JsonMembers.h`, `test_json_members.cpp`).
- The 10 s list comment says PV1 to PV6 were dropped to save six reads. `REG_CUSTOM_LOAD` and `REG_CUSTOM_TOTAL_SOLAR_POWER` still read all six strings through `readTotalSolarPower`, which stops at the first failure.
- With `MQTT_SERVER` as a hostname, a broker attempt can take 7 s against an 8 s watchdog. Use a dotted address (65f3526).
- Nothing is retained, and there is no LWT. A new subscriber waits up to one period for state. A dead Pico shows only as the fast state stopping and `seq` gaps.
- `DEBUG` is on, and `checkForData` prints a timeout line on every RS485 timeout whatever the setting.
- If every buffer allocation fails, `_mqttPayload` stays unset. There is no guard.

## Open questions

- Real `read_ms` and loop pass time on hardware: unknown.
- Whether the AC coupled inverter answers PV1 to PV6 reads, which sets the real 10 s read count: unknown.
- Whether a `handled/all` over a wide range can delay an OTA start past espota's 10 s limit: unknown.
- Whether the PubSubClient used in the real build is the locally installed 2.8: guess yes.
