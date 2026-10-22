Modbus RTU transport, frame checks and the AlphaESS register map as the firmware reads and writes them.

Last verified: 2026-10-22 on fix/firmware, date format in `Registers.h`, slave id resync capped, every fault bit named, AL bits 13 to 16 mapped, grid current in 0.1 A, register comments corrected. Earlier: working tree on 75e08a8 plus the 2026-10-21 cleanup. `tests/firmware/run` passed all eight suites (`test_dispatch`, `test_idle_hold`, `test_json_members`, `test_modbus_frame`, `test_pv_meter`, `test_registers`, `test_request`, `test_timer` each print `OK`, exit 0).

## Files

| File | Owns |
|---|---|
| `Alpha2MQTT/RS485Handler.cpp/.h` | UART setup, send, receive, CRC, line drain |
| `Alpha2MQTT/ModbusFrame.h` | Pure frame checks: `readReplyFits`, `readRequestFits`, `replyMatchesRequest`, `rawDataList`. Host tested in `tests/firmware/test_modbus_frame.cpp` |
| `Alpha2MQTT/RegisterHandler.cpp/.h` | Bus reads for handled registers, the custom registers, `readPvMeterPower`, `readTotalSolarPower`; raw read and write helpers; `writeDispatchBlock` |
| `Alpha2MQTT/Registers.h` | Pure descriptor table `handledRegisters`, 18 lookup tables, `findRegister`, `describeRegister`, `decodeRegisterValue`, `formatRegisterValue`. Host tested in `tests/firmware/test_registers.cpp` |
| `Alpha2MQTT/Definitions.h` | Register addresses, lookup values, status enum, `modbusRequestAndResponse` struct, timing knobs. Without `ARDUINO` it includes `stdint.h` and skips `Secrets.h`, so host tests can use it |
| `Alpha2MQTT/Dispatch.h` | `dispatchBlockFrame` layout of 0x0880 to 0x0888 (owned by the dispatch facts file) |
| `Alpha2MQTT/PvMeter.h` | `pvMeterZeroUnconfirmed`, used by `readPvMeterPower` |

## Transport

| Item | Value | Where |
|---|---|---|
| Pico W UART | `Serial1`, RX GP1, TX GP0, driver enable GP2 (HIGH = transmit) | `RS485Handler.h`, constructor |
| Framing | 8N1 at construction (`SERIAL_8N1`). `setBaudRate` calls `begin(baudRate)` with no config argument, so default framing (guess: 8N1) | `RS485Handler` ctor, `setBaudRate` |
| Start baud | `DEFAULT_BAUD_RATE` 9600 | `RS485Handler.h` |
| Baud search at boot | Tries 9600, 115200, 19200, 57600, 38400, 14400, 4800 in turn by reading `REG_SAFETY_TEST_RW_GRID_REGULATION` (0x1000); 1 s between failures | `knownBaudRates` in `Alpha2MQTT.ino` |
| Slave address | `ALPHA_SLAVE_ID` 0x55, compile time only | `Definitions.h` |
| Function codes | Read 0x03, Write Single 0x06, Write Data (multiple) 0x10 | `MODBUS_FN_*` |
| Byte timeout | `RS485_TRIES` 8 x 50 ms = 400 ms, polled every 1 ms. Applies to each byte wait, not to the whole frame | `checkForData` |
| Optional pre-send gap | `REQUIRE_DELAY_DUE_TO_INCONSISTENT_RETRIEVAL` (commented out) adds 80 ms before each send | `sendModbus` |
| Post-failure drain | After a failed exchange, the next send first waits for 10 ms of silence (`RS485_QUIET_MS`), at most 150 ms (`RS485_DRAIN_MAX_MS`) | `flushRS485`, `_lineDirty` |
| Receive buffer | `MAX_FRAME_SIZE_ZERO_INDEXED` 63 bytes (`inFrame`, `resp->data`) | `Definitions.h` |
| Max registers per read | 29 (58 data bytes + 5 overhead = 63). Enforced for raw reads by `readRequestFits`; handled reads use the fixed `registerCount` of their descriptor, 1 to 10 | `ModbusFrame.h`, `mqttCallback` |
| Retries | None at the transport. One extra listen when the first reply is `unexpectedResponse`. Callers own retries | `sendModbus` |
| Watchdog | `feedWatchdog()` once per `sendModbus`, not per byte | `sendModbus` |

Vendor spec (PDF in `Pics/`, section 2.2): response time under 300 ms, command interval over 300 ms, timeout over 10 s, 8 data bits, no parity, 1 stop bit, default address 0x55. The firmware comment on `RS485_TRIES` says 300 ms gave timeouts and 400 ms works. The firmware does not enforce the 300 ms command interval; back to back reads are normal (fast schedule).

## Send and receive path

`RS485Handler::sendModbus(frame, size, resp)`:
1. `feedWatchdog()`, then `calcCRC` overwrites the last two bytes. Callers pass the frame with two trailing zero bytes.
2. `flushRS485`: flush TX, discard RX, drain the line if `_lineDirty`.
3. Driver pin to TX, write, `flush()`, pin back to RX.
4. `listenResponse(frame, resp)`; if it returns `unexpectedResponse`, listen once more.
5. `_lineDirty` is set for any result except the three successes and `slaveError`.

`listenResponse` reads byte by byte:
- Byte 0 must equal `ALPHA_SLAVE_ID`. Any other byte is discarded and the slot reread.
- Byte 1 is the function code. 0x03: next byte is the byte count, checked with `readReplyFits` before any data is stored. 0x06 or 0x10: 8 byte reply expected. Anything else: exception, expects up to 6 bytes.
- A 5 byte exception that times out on the optional 6th byte is accepted as complete (f5de99e).
- Then: too short, CRC, `replyMatchesRequest`, and status by function code.

`resp->data` holds the payload only: for reads, the data bytes after the byte count; for writes, the echoed address and count or value; for exceptions, the 1 or 2 code bytes. `resp->dataSize` is the byte count.

### Status results (`modbusRequestAndResponseStatusValues`)

| Status | Cause in this layer |
|---|---|
| `readDataRegisterSuccess` | Good 0x03 reply that matches |
| `writeDataRegisterSuccess` | Good 0x10 echo |
| `writeSingleRegisterSuccess` | Good 0x06 echo |
| `slaveError` | Good CRC, code not 0x03/0x06/0x10, matches request |
| `noResponse` | Nothing arrived within 400 ms |
| `responseTooShort` | Fewer than 5 bytes |
| `invalidFrame` | Bad CRC, byte count would overrun the 63 byte buffer, or `RS485_RESYNC_MAX_BYTES` (64) bytes came without the slave id |
| `unexpectedResponse` | Good CRC but reply answers another request (twice in a row) |
| `notHandledRegister` | `findRegister` has no descriptor for the address; nothing is sent |
| `unconfirmedZero` | PV meter returned 0 right after a reading 10 W or more from 0 |

`statusMqttMessage` carries the enum name as text (from the `*_MQTT_DESC` defines).

### Exception codes

The firmware does not decode exception codes. It keeps the 1 or 2 code bytes in `resp->data`. The raw response path prints them through `rawDataList`. Vendor exception code list: unknown (not in PDF pages 1 to 9; appendix not read).

### ModbusFrame.h API

| Function | Returns |
|---|---|
| `readReplyFits(byteCount, bufferSize)` | `byteCount + 5 <= bufferSize` |
| `readRequestFits(registerCount, bufferSize)` | true for 1 to `(bufferSize - 5) / 2` registers; false for 0 |
| `replyMatchesRequest(request, reply)` | Read: same code and byte count `== 2 x requested registers`. Write: same code and bytes 2 to 5 echoed. Exception: `asked | 0x80`. Any code without bit 7 that is not a success is accepted as an exception |
| `rawDataList(out, outSize, data, count)` | Decimal bytes, comma separated; stops before a byte that would not fit; empty for 0 bytes |

`RS485Handler::checkCRC` and `calcCRC` are public. CRC-16/Modbus (poly 0xA001, init 0xFFFF), low byte first. `checkCRC` writes the computed CRC over the received one in the buffer as a side effect.

## RegisterHandler structure

Public API (all return `modbusRequestAndResponseStatusValues`):

| Method | Does |
|---|---|
| `readHandledRegister(addr, rs)` | Typed, scaled, formatted read of a known register |
| `readRawRegister(addr, rs)` | 0x03 of `rs->registerCount` registers, no decoding |
| `writeRawSingleRegister(addr, value, rs)` | 0x06, one 16 bit value |
| `writeRawDataRegister(addr, value, rs)` | 0x10; `rs->registerCount` must be 1 (2 bytes) or 2 (4 bytes). Any other count returns `preProcessing` and sends nothing |
| `writeDispatchBlock(start, power, mode, soc, durationS, rs)` | One 0x10 frame of 9 registers at 0x0880, built by `dispatchBlockFrame` |
| `setSerialNumberPrefix(c1, c2)` | Picks AL or AE fault bit text for `REG_SYSTEM_OP_R_SYSTEM_FAULT_1` |

`readHandledRegister` flow:
1. `findRegister(addr)` scans the const table `handledRegisters` in `Registers.h`. NULL: `notHandledRegister`, nothing sent.
2. `describeRegister` sets `returnDataType`, `mqttName`, `registerCount`, and `hasLookup` for the `lookup`, `systemFault` and `ipAddress` formats.
3. Custom addresses compute from several reads (below). `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1` goes through `readPvMeterPower`. `REG_CUSTOM_SYSTEM_DATE_TIME` reads from 0x0740. Everything else sends one 0x03 frame.
4. On success, `decodeRegisterValue` decodes `rs->data` big endian by type into one of `unsignedShortValue` (U16), `signedShortValue` (S16), `unsignedIntValue` (U32), `signedIntValue` (S32) or `characterValue`. 32 bit registers are high word first.
5. `formatRegisterValue` writes `dataValueFormatted` by the descriptor's `registerFormat`: `value` (`%u`/`%d`), a scaled `%0.02f` (`tenths`, `hundredths`, `gridVoltage`, `cellVoltage`, `inverterTemp`, `totalEnergy`), `dispatchSoc`, `ipAddress`, `text`, `textMoved`, `lookup`, `systemFault` or `dateTime`. `dateTime` takes bytes YY MM DD HH mm ss as `dd/MMM/20yy HH:mm:ss`; a month outside 1 to 12 prints `---` (before 2026-10-22 it indexed the month names out of bounds), and a one digit year keeps its leading zero (it printed `205`).

A descriptor is `{address, name, returnDataType, registerCount, format, lookup}`. The `HANDLED_REGISTER` and `HANDLED_LOOKUP` macros stringify the define, so the JSON key always equals the define name. A `registerLookup` maps a value to text; with no match it writes its `unknown` text, or leaves the field as it was when that is NULL. With `bits` set, `formatBitLookup` names every set bit, lowest first, joined by `, `, cut at the 128 character field; a bit with no entry shows as `Bit N` and a clear word as `""`. Before 2026-10-22 it gave only the lowest set bit, and battery fault bits 0, 1 and 30, which have no text in the vendor table, gave `""`, the same as no fault. `systemFault` picks the AL or AE table from the serial prefix, and writes `Unknown` for any other prefix. The AL table maps bits 0 to 16 to `SYSTEM_ERROR_AL_BIT_0` to `_16`; the vendor table (V1.23) lists 0 to 12 only, and the text for 13 to 16 comes from upstream. Before 2026-10-22 bits 13 to 15 showed the next bit's text and `SYSTEM_ERROR_AL_BIT_13` never showed.

206 registers are handled; `test_registers.cpp` pins the count, unique addresses, names and a few formats. `Definitions.h` has 208 `#define REG_` lines including the four custom ones. The two not handled are `REG_AUXILIARY_W_EMS_DO0` (0x08B0) and `REG_AUXILIARY_W_EMS_DO1` (0x08B1), the EMS digital outputs (bypass control and system fault output, per their comments). The table replaced two per register switches in the cleanup.

### Custom registers (not on the inverter)

| Define | Addr | Built from |
|---|---|---|
| `REG_CUSTOM_TOTAL_SOLAR_POWER` | 0xFFFC | `readTotalSolarPower`: PV meter + PV1 to PV6 string power, 7 reads, stops at first failure |
| `REG_CUSTOM_GRID_CURRENT_A_PHASE` | 0xFFFD | `gridCurrentTenths`: phase A grid power / (phase A voltage x 0.1), held as tenths of an amp and published with the `tenths` format, as `4.20`; 0 if voltage is 0. Before 2026-10-22 the voltage was cut to whole volts and the result to whole amps |
| `REG_CUSTOM_LOAD` | 0xFFFE | Solar total, then grid total, then battery power: `pv - export + import - charge + discharge`. 9 reads |
| `REG_CUSTOM_SYSTEM_DATE_TIME` | 0xFFFF | 3 registers from 0x0740, formatted `dd/MMM/yyyy HH:mm:ss` |

### MQTT JSON

`addStateInfo` in `Alpha2MQTT.ino` writes one member per register: `"<mqttName>": <dataValueFormatted>`, after `jsonMemberSeparator` (`JsonMembers.h`). The value is quoted when the type is `character` or `hasLookup` is true. So the JSON key is the define name, numbers are bare, lookups are strings such as `"Start"`. A failed read leaves the key out of that payload, and the JSON stays valid.

## Registers the control path uses

Fast schedule `_mqttFastStatusRegisters` (every second, topic `<device>/state/fast`): grid, PV meter, battery power, SOC, dispatch start. The controller (`FAST_FIELDS` in `controller.py`) reads exactly these keys. Signs per `regulator.py` docstring: grid positive on import, battery positive on discharge.

| Addr | Define | Type | Scale, unit | JSON value | Sign / meaning | R/W |
|---|---|---|---|---|---|---|
| 0x0021 | `REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1` | S32 | 1 W | `%d` | + import (drawing), - export (feeding) | R |
| 0x00A1 | `REG_PV_METER_R_TOTAL_ACTIVE_POWER_1` | S32 | 1 W | `%d` | + generating; read via `readPvMeterPower` | R |
| 0x0126 | `REG_BATTERY_HOME_R_BATTERY_POWER` | S16 | 1 W | `%d` | - charge, + discharge | R |
| 0x0102 | `REG_BATTERY_HOME_R_SOC` | U16 | 0.1 % | `%0.02f` percent, e.g. `55.20` | | R |
| 0x0880 | `REG_DISPATCH_RW_DISPATCH_START` | U16 | lookup | `"Start"` (1), `"Stop"` (0), `"Unknown"` | | RW |
| 0x0881 | `REG_DISPATCH_RW_ACTIVE_POWER_1` | S32 (U32 on the wire) | 1 W, offset 32000 | `%d` raw register | < 32000 charge, > 32000 discharge, 32000 none | RW |
| 0x0883 | `REG_DISPATCH_RW_REACTIVE_POWER_1` | S32 | 1 var, offset 32000 | `%d` | block write always sends 32000 (0x7D00) | RW |
| 0x0885 | `REG_DISPATCH_RW_DISPATCH_MODE` | U16 | lookup | string; 2 = `"State of charge control"` | firmware writes 2 only | RW |
| 0x0886 | `REG_DISPATCH_RW_DISPATCH_SOC` | U16 | 0.4 %/bit | `%u` whole percent, `(v*4+5)/10` | written as `dispatchSocFromPercent`, rounded up | RW |
| 0x0887 | `REG_DISPATCH_RW_DISPATCH_TIME_1` | U32 | 1 s | `%u` | lease length | RW |

0x0882, 0x0884 and 0x0888 are the low words of the 32 bit registers above; their `_2` defines are commented out.

Supporting registers (ten second, one minute or on request; not in the fast loop):

| Addr | Define | Type | Scale | Note | R/W |
|---|---|---|---|---|---|
| 0x041F, 0x0423, 0x0427, 0x042B, 0x042F, 0x0433 | `REG_INVERTER_HOME_R_PV1_POWER_1` to `PV6` | U32 | 1 W | Hybrid strings; left out of schedules on an AC coupled SMILE-B3; summed into custom solar | R |
| 0x001B | `REG_GRID_METER_R_ACTIVE_POWER_OF_A_PHASE_1` | S32 | 1 W | Input to custom grid current | R |
| 0x0014 | `REG_GRID_METER_R_VOLTAGE_OF_A_PHASE` | U16 | 0.1 V (`GRID_VOLTAGE_MULTIPLIER`) | Spec says 1 V | R |
| 0x0100 | `REG_BATTERY_HOME_R_VOLTAGE` | U16 | 0.1 V | | R |
| 0x0101 | `REG_BATTERY_HOME_R_CURRENT` | S16 | 0.1 A | Always zero on the upstream author's unit, per a comment removed in the cleanup (still in 75e08a8 `RegisterHandler.cpp`). A SMILE-B3 read nonzero values on 2026-10-21, so it is not zero on every unit | R |
| 0x0111 / 0x0112 | `REG_BATTERY_HOME_R_MAX_CHARGE_CURRENT` / `MAX_DISCHARGE_CURRENT` | U16 | 0.1 A | Limits; not read by the controller | R |
| 0x012C / 0x012D | `REG_BATTERY_HOME_R_BATTERY_MAX_CHARGE_POWER` / `MAX_DISCHARGE_POWER` | U16 | 1 W | Limits; only in `_mqttAllHandledRegisters`; discharge read 5350 on a B3, per a comment removed in the cleanup (still in 75e08a8) | R |
| 0x0805 | `REG_SYSTEM_CONFIG_RW_SYSTEM_MODE` | U16 | lookup | 1 AC, 2 DC, 3 Hybrid; only in `_mqttAllHandledRegisters` | RW |
| 0x0807 | `REG_SYSTEM_CONFIG_RW_BATTERY_READY` | U16 | lookup | 0 Off, 1 On | RW |
| 0x084F | `REG_TIMING_RW_TIME_PERIOD_CONTROL_FLAG` | U16 | lookup | 0 off, 1 charge, 2 discharge, 3 both | RW |
| 0x0850 | `REG_TIMING_RW_UPS_RESERVE_SOC` | U16 | 0.1 % per spec, 1 % on some units | Inverter discharge floor | RW |

## Inverter quirks recorded in code and commits

- Write Single Register (0x06) does nothing on the author's SMILE-B3: no change, no reply. Use 0x10 for 1 or 2 register writes (`RegisterHandler.h` header; README). f5de99e notes some inverters ignore 0x06 on 0x0850.
- SMILE-B3 on EMS 1.03.78 applies writes to 0x0850 to 0x0854 only while bit 1 of 0x084F is set, and 0x0855 to 0x0859 only while bit 0 is set. The flag always writes; values stay after it clears (05cd6b2, comment on the defines). 53b10d3 earlier recorded that a B3 acknowledges and ignores these writes.
- 0x0850 holds whole percent on some units (20 = 20 %), against the spec's 0.1 % (Definitions.h comment).
- The PV meter (0x00A1) now and then returns a CRC valid frame of exactly 0 for one read while generating. A 0 right after a reading 10 W or more from 0 fails as `unconfirmedZero` (672066a, `PvMeter.h`).
- Late replies: a grid reply landed after the next read's flush and passed as the battery reading. Fixed by `replyMatchesRequest`, the second listen and the quiet drain (9268ba4).
- A corrupt byte count could overrun the 63 byte buffers; now `invalidFrame` (95f3733).
- Before 2026-10-22, `listenResponse` skipped bytes that were not the slave id with no limit, so a line that never went quiet held the loop until the watchdog. It now gives up after `RS485_RESYNC_MAX_BYTES` as `invalidFrame`.
- A standard 5 byte exception used to time out and fail CRC as `invalidFrame` (f5de99e).
- Spec scales the firmware overrides, from author readings: grid voltage x 0.1 not 1 V, cell voltage x 0.01 not 0.001, inverter temperature x 0.01 not 0.1, total energy x 0.01 not 0.1 kWh (`Definitions.h` multiplier comments).
- PV string powers were wrongly scaled by 0.1 before 48591d3 (2022).
- `checkCRC` used `=` instead of `==` and so always passed before 7bb3666 (2023).
- 80 ms pre-send gap fixed sporadic no responses for one user at 115200 baud (`REQUIRE_DELAY_DUE_TO_INCONSISTENT_RETRIEVAL` comment). Off by default.
- 9102d0c: reply polling went from 50 ms steps to 1 ms. A 9600 baud write exchange measured about 63 ms then.
- 9452d4a: the 9 register dispatch write is one frame of about 50 ms instead of five frames 400 ms apart. If the inverter refuses it, `Alpha2MQTT.ino` falls back to five writes, with `delay(RS485_TRIES * 50)` between them.

## Gotchas

- Before 2026-10-22 the `RegisterHandler.h` and `RegisterHandler.cpp` headers said Write Data Register is 0x0A; the code uses 0x10 (`MODBUS_FN_WRITEDATAREGISTER`), and the headers now say so.
- Min and max cell temperature (0x010D, 0x0110) use `tenths`, 0.1 deg C, as the vendor table says. Their define comments said 0.001D/bit until 2026-10-22. The pack and cell ID defines beside them, 0x0105 to 0x010F, carried voltage or temperature scales; they are IDs with no scale.
- The 400 ms timeout restarts for each byte. A line that keeps sending non 0x55 bytes keeps `listenResponse` reading; only the hardware watchdog bounds it.
- `slaveError` does not mark the line dirty; `noResponse`, `invalidFrame`, `responseTooShort` and `unexpectedResponse` do, so the next send can wait up to 150 ms more.
- `_lastPvMeterPower` is one value shared by the fast schedule, the ten second schedule, `REG_CUSTOM_TOTAL_SOLAR_POWER` and `REG_CUSTOM_LOAD`. An `unconfirmedZero` also fails both custom registers for that read.
- `REG_DISPATCH_RW_ACTIVE_POWER_1` is published as the raw offset value, not watts. Subtract 32000; negative is charge.
- `REG_DISPATCH_RW_DISPATCH_START` arrives as a string, not 0 or 1.
- SOC from 0x0102 is a percent with two decimals; dispatch SOC from 0x0886 is a whole percent. Different scales on the wire (0.1 % vs 0.4 %).
- Frame register count is one byte (`0, rs->registerCount`), so `registerCount` is a `uint8_t`.
- `REG_CUSTOM_LOAD` costs 9 bus reads; `REG_CUSTOM_TOTAL_SOLAR_POWER` 7.

## Open questions

- Vendor exception code meanings: unknown. Not decoded in firmware.
- Whether 0x0881 really is signed on the wire: unknown. With the 32000 offset it is never negative in practice.
- `REG_INVERTER_HOME_R_POWER_TOTAL_1` (0x040C) read 65526 on the author's B3, per a comment removed in the cleanup (still in 75e08a8); cause unknown (guess: a 16 bit negative read into a 32 bit register).
- Whether `setBaudRate` keeps 8N1 on the arduino-pico core: guess yes (Arduino default), not verified.
- Whether the SMILE-B3 honours the spec's 300 ms command interval: unknown. The fast schedule reads back to back and its observed behaviour is not recorded here.
