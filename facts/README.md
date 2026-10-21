# Facts: start here

The control system in one page, and which facts file to read for a task.
Each file states what its author read or ran, anchors code by symbol, and
marks the rest "unknown".

Last verified: working tree on 75e08a8 plus the 2026-10-21 cleanup; the Pico W build
compiles and `tests/firmware/run` 7 suites OK. On 1e945f6 plus the diagnosis
attributes: `python3 -m pytest "Home Assistant/tests"` 187 passed.

## Keeping it true

- Change code, update the facts file that owns that area in the same commit,
  and bump its "Last verified" line.
- One owner per fact. Link to the owning file instead of copying.
- A fact you could not verify says "unknown".

## The loop in one page

```
inverter --RS485 Modbus RTU 9600 8N1, slave 0x55--> Pico W firmware
  every 1 s: read grid, PV meter, battery, SOC, dispatch start
  publish <base>/state/fast {seq, ms, read_ms, haIdle, 5 register keys}
                                   |
                                   v
HA custom component alpha2mqtt_controller
  controller.py: estimate grid, read policy entity, call Regulator.step
  regulator.py:  pure control law, no HA imports
  publish <base>/request/set/{charge|discharge|normal} {watts, socPercent, duration, seq[, powerOnly]}
                                   |
                                   v
Pico W mqttCallback: write dispatch block 0x0880..0x0888 as one 9-register frame
  (fallback: five single writes 400 ms apart), reply on the response topic with seq
```

Control speed is set by the inverter's ramp, not the bus. A block write is
about 50 ms; a step takes seconds to reach 80 %, and starting a discharge
from idle ramps at about 10 W/s. See `design-history.md`.

## Signs and units (the most confused facts)

| Quantity | Positive means | Where |
| --- | --- | --- |
| Grid power 0x0021 | import | wire, controller, regulator |
| Battery power 0x0126 | discharge | wire, controller, regulator |
| Request `watts` | always positive; the topic sets direction | MQTT |
| Dispatch power 0x0881 | 32000 + W discharge, 32000 - W charge | register, and the raw value HA shows |
| Dispatch SOC 0x0886 | 0.4 % per bit, firmware rounds up | register |
| Battery SOC 0x0102 | 0.1 % per bit | register |

## Read this for that task

| Task | Read |
| --- | --- |
| Change how the battery is commanded (gains, floor, refusal, keep warm, slew) | `regulator.md`, then `design-history.md` |
| Change triggers, inputs, entities, config options, ack handling | `ha-controller.md` |
| Diagnose a live behaviour from logs | `steplog-and-response.md` |
| Change a request topic, dispatch write, idle hold | `firmware-dispatch.md` |
| Change loop timing, schedules, publishes, reconnects, watchdog | `firmware-runtime.md` |
| Touch Modbus framing, timeouts, register scaling | `modbus-and-registers.md` |
| Add or rename any topic or JSON key | `mqtt-contract.md` |
| Any tuning idea | `design-history.md` "Tried and reverted" first |

## Truths that cut across files

- Only the controller should drive dispatch. Four writers exist: the
  controller, package scripts `alpha2mqtt_set_*`, the firmware idle hold and
  raw register writes. Nothing arbitrates; the last write wins.
  `mqtt-contract.md`.
- Nothing is retained and the firmware sets no MQTT will. Freshness comes
  from `seq` and age checks. `firmware-runtime.md`.
- The firmware never retries a write. The controller resends; a refused or
  unanswered command goes to `regulator.fail()`. `ha-controller.md`.
- If HA is gone for 180 s, the firmware holds 0 W charge itself and writes
  `set/normal` when HA returns. `firmware-dispatch.md` "IdleHold".
- A discharge that delivers under 30 W for 20 s counts as refused. Near the
  floor the refusal holds until SOC rises 1 %. `regulator.md`.
- `powerOnly` must be exactly `1`; a `.` in any value is stripped
  (`150.5` becomes `1505`). `firmware-dispatch.md`.
- Two entities the controller needs live outside this repo: the policy
  entity and the correction entity. `mqtt-contract.md`.
- The SMILE-B3 ignores function 0x06, and settings in 0x0850..0x0859 need
  the 0x084F flag bit. `modbus-and-registers.md`.

## Code map

| Area | Files |
| --- | --- |
| Request parsing, schedules, publishes | `Alpha2MQTT/Alpha2MQTT.ino` (`mqttCallback`, `loop`) |
| Register table and formatting | `Alpha2MQTT/Registers.h`; I/O and custom registers in `RegisterHandler.cpp` |
| Modbus transport | `RS485Handler.cpp`, `ModbusFrame.h` |
| Pure helpers with host tests | `Dispatch.h`, `IdleHold.h`, `PvMeter.h`, `Timer.h`, `JsonMembers.h`, `Registers.h` |
| Control law | `regulator.py`; HA glue `controller.py`; logs `steplog.py`, `response.py` |

The firmware is Pico W only. The ESP boards, the OLED display, Node-RED
flows and the legacy HA txt files were removed on 2026-10-21.

## Known defects (verified in code)

- The AL system fault lookup tests bit 12 twice, so faults on bits 13 to 15
  show the next bit's text and `SYSTEM_ERROR_AL_BIT_13` never shows. Kept
  as upstream had it; see `systemFaultAlEntries` in `Registers.h`.

Each file ends with its own open questions.
