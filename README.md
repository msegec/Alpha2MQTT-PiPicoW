# Alpha2MQTT (A2M)
## A smart home interface for AlphaESS solar and battery inverters.

_**NEW!** For a complete accompanying guide to integrating your Alpha2MQTT with Home Assistant on a Raspberry Pi or Virtual Machine, check [here](https://github.com/dxoverdy/Integrating-Alpha2MQTT-with-Home-Assistant)._

### About this fork
This is a fork of [dxoverdy/Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) that runs on a Raspberry Pi Pico W and is driven by a Home Assistant controller. Fork version 1.29, 21 October 2026, on top of upstream v1.27 (June 2024). The changes are summarised here:

- **Pico W only.** UART0 on GP0 and GP1, built with arduino-pico 6.2.0. Upstream's other boards and its display are gone. See [Steps](#steps).
- **Credentials in Secrets.h.** WiFi, MQTT and the OTA password live in a gitignored `Secrets.h`, copied from `Secrets.example.h`, instead of Definitions.h. An empty MQTT username connects without credentials.
- **Over the air updates.** Password protected ArduinoOTA on the Pico W, with `scripts/ota-upload` to build and send a new image over WiFi. USB is only needed for the first flash.
- **A fast state every second.** `Alpha2MQTT/state/fast` carries grid power, PV power, battery power, SOC and dispatch start with a sequence number and timing, for a closed loop controller. See [States](#states).
- **Set responses carry a seq and ms.** `set/charge`, `set/discharge` and `set/normal` accept an optional `seq` and echo it with the Pico's millis(), so a controller can match replies to requests.
- **One frame dispatch writes.** `set/charge` and `set/discharge` write the whole dispatch block, registers `0x0880` to `0x0888`, as one Write Data Register, about 50 ms on the bus instead of five writes two seconds apart. An inverter that refuses the nine register write gets the five writes as before; the response says which with `"frames": 1` or `5`. A `set/charge` or `set/discharge` with `"powerOnly": 1` rewrites only the power register when the dispatch Alpha2MQTT last wrote in full has the same kind, SOC and duration and less than half its duration has passed. See [Force Charge From Grid](#force-charge-from-grid).
- **Idle while Home Assistant is gone.** Alpha2MQTT subscribes to `homeassistant/status`, Home Assistant's default birth and will topic (`MQTT_SUB_HA_STATUS` in Definitions.h). Home Assistant counts as gone 3 minutes after its `offline` will, until the next `online`, or after 3 minutes off the broker, counted from the last time Alpha2MQTT was connected or from boot, until it reconnects. A planned Home Assistant restart sends the will and is back in about 2 minutes, so an `online` or any set request inside the 3 minutes cancels the pending `offline` and nothing is written. While it is gone, Alpha2MQTT writes a full dispatch block every 60 s, charge at 0 W to 100 % SOC for 120 s, so the battery sits idle instead of following the inverter's own mode. A charge of 0 W stores nothing, so solar surplus is exported while the hold is in place. When Home Assistant is back, a hold Alpha2MQTT wrote is ended at once with the same write as `set/normal`, so the inverter returns to its own mode until the controller's first command. A failed hold or release is tried again after 10 s. Any `set/charge`, `set/discharge` or `set/normal` request counts as Home Assistant being up, even after an `offline`, and is never overwritten by the hold: Home Assistant sends its birth non-retained by default, so Alpha2MQTT misses one sent while it is off the broker itself. For the hold to survive a reboot of Alpha2MQTT while Home Assistant is down, and to end after a birth Alpha2MQTT was off the broker for, set Home Assistant's MQTT birth and will messages to retained (MQTT integration, Configure). A `set/normal` sent while Home Assistant is up stays normal. The fast state's `haIdle` is 1 while Home Assistant counts as gone.
- **Device supply every minute.** `Alpha2MQTT/state/device` reports VSYS and whether USB power is present.
- **Schedules tuned for a battery only inverter.** Energy totals move from daily to every minute so the Home Assistant Energy dashboard stays current, and the hybrid only PV1 to PV6 reads are dropped from the ten second schedule. Battery health reports every minute and slow battery facts every five minutes. The daily schedule is empty by default.
- **Dispatch SOC as a percent.** `socPercent` is converted with the register's 0.4 % per bit scale and rounded up, so a charge reaches at least the target and a discharge stops at or above it, and the response reports the percent rather than 0. At the top the scale is wrong on a SMILE-B3: 100 % writes 250, which stopped at 99.2 %, and 251 stopped at 99.6 %; 252 is full. Above 100, `socPercent` is a register step: 101 writes 251, 102 writes 252 and 105, the most allowed, writes 255, which charges until the battery stops taking current.
- **Responsive while busy.** MQTT and OTA are serviced between the register reads of a schedule, RS485 replies are polled every millisecond, and requests over 127 bytes are rejected rather than truncated.
- **Modbus exceptions reported.** A five byte exception reply is reported as a slave error instead of a timeout.
- **SMILE-B3 settings writes.** The settings block `0x084F` to `0x0859` only takes a write while the matching bit of the time period control flag is set. The rule and the write sequence are under [Write Raw Data Register](#write-raw-data-register).
- **Home Assistant package.** `Home Assistant/alpha2mqtt.yaml` defines MQTT sensors for every state topic, ready for the Energy dashboard, in place of the upstream dashboard guide. Its scripts Alpha2MQTT Charge, Discharge and Normal send `set/charge`, `set/discharge` and `set/normal` for 600 s by default. One writer drives dispatch at a time: each script first turns off the battery controller's Enabled switch, `switch.alpha2mqtt_battery_controller_enabled` by default, so the controller's next command does not overwrite the script's. Turn Enabled on again to hand control back. If you renamed that switch, set the script's `controller_switch` field.
- **Battery controller for Home Assistant.** `Home Assistant/custom_components/alpha2mqtt_controller` is a custom integration that regulates the battery against the grid reading in the fast state, sending `set/charge` and `set/discharge` with a seq every few seconds. A policy entity says what is allowed (release, hold, fixed, or regulate within charge and discharge limits and an SOC target; `both: true` with `other_soc` allows the other direction too, so a load edge turns the battery round in the next step), and the regulator chooses the watts. A correction entity rendered from the fast state can carry the message's `seq` as an attribute; the controller then steps each sample on the correction rendered from it, waiting up to 0.5 s and then taking the value it has. Without the attribute it steps at once, as before. The correction entity may carry an `age_s` attribute, the age of its oldest input; over 90 s the correction counts as missing, as if the entity were unavailable. It may also carry `west` and `east` attributes, solar in W, and `west_fallback`, true when West came from the 10 s message, which drops solar for that sample; Solar feed forward gain, 0 by default, then takes that gain times each solar change of more than 20 W off the next setpoint, so a solar ramp is met before the inverter's 2 to 4 s lag. Feed forward minimum solar, 0 by default, gives no feed forward when either reading is under it, for a West reading that flips across a template floor at dawn and dusk. The Feed forward guard switch, off by default, stops the feed forward from turning the battery round on its own; it still leads a ramp in the running direction. A policy SOC outside 0 to 105, which the firmware would refuse, counts as an unreadable policy. A reading or policy that drops out for a moment, as in a template reload, holds the last command; after Stale after (30 s) with no good reading or policy it hands the inverter back. A policy state it does not know, anything but charge, discharge, hold or normal, releases the inverter and the Status sensor says why, as `released: unknown policy state 'boost'`; the warning is logged once per value. A refused write, or one with no response within 10 s from firmware that echoes `seq`, is sent again in full after the minimum interval, and so is a failed `set/normal`. A release at start sends `set/normal` once, in case a crash left a lease running. Within 1.5 % above the policy's discharge SOC, read from the fast state, it holds the battery at 0 W instead of asking for a discharge the inverter refuses a little above its floor, until the SOC rises or the limit drops; it does the same for a minute when a discharge of 100 W or more draws under 30 W for 20 s, whatever the SOC says, and within 3 % of the discharge SOC until the SOC rises 1 % or the limit changes. Charging, and the turn to charge under `both`, still go through. A charge or discharge limit below 0 counts as 0 W, never as a push the other way, and an unreadable limit keeps its last value; only before its first read is it 3000 W. At the charge SOC it holds 0 W the same way instead of asking a full battery to charge, so the surplus it cannot store does not wind the integral. 0 W goes out in the running block's kind, so idling between loads never costs a block write. The integral waits, for up to 15 s, while the battery is more than 100 W short of the last setpoint: that gap is the inverter on its way, not an error to trim. Arrival restart step, 0 by default, restarts that 15 s each time the battery closes that many watts on the setpoint, so a slow start's ramp is not trimmed either. A battery past the setpoint, such as a DC reading over an AC setpoint, is a steady offset and is trimmed. Offset correction, 0 by default, takes that share of the inverter's measured steady offset (about -25 W at 100 to 1500 W of discharge, +198 W at 2750 W and +220 W at 3000 W) off each regulated command, so the battery lands on the watts the regulator chose and the integral does not have to hold a different offset at each level. Power write gap, 0 by default, turns on power-only writes: above 0 a power change in the running block is sent with `powerOnly`, and after the firmware acks one as `"write": "power"` the next command may follow that long after it instead of waiting the minimum interval. Keepalives, new kinds and new SOCs still write the whole block, and older firmware just writes the block every time. Keep warm, 0 by default, is a ready floor for an inverter that starts a discharge slowly: above 0, while a discharge that big is allowed, the command never goes under that many watts of discharge, so the battery never idles and the next load step comes from a running discharge. Idle, a smaller discharge or a charge under 50 W is lifted to the floor; only a bigger surplus the battery can store takes it into a charge. A surplus it cannot store, or one the turn round holds under the margin, goes out with the floor on top, so a little is exported. Out of a charge the floor waits out the turn round dwell, and a rise to the floor is sent however small. A charge to 100 % goes out as the Full charge register setting, 252 by default (`socPercent` 102), since 250 leaves the battery short of full. Learn offsets, a switch, off by default, is a long term trim like an engine's learned fuel trims: the integral is the short term trim, and while the command has been steady for Offset learning settle (20 s), the grid within Offset learning band (50 W) and the setpoint at least Offset learning minimum (50 W), Offset learning rate of the integral per second moves into a table of learned offsets by setpoint, so the next visit to that level starts on target. The rate is 0 by default, which learns nothing. Each learned point stays within Learned offset limit (100 W); nothing is learned at a cap, the floor, full, over Offset learning top SOC (97 %) or under a policy grid offset. The table is saved across restarts, shown as the `learned_offsets` attribute of the Integral sensor, and the Forget learned offsets button zeroes it. Spike confirm, 0 by default, holds the output for one sample when net demand jumps by more than that many watts, so a load of a second or two is not answered after it has gone; a jump that lasts goes out a sample late. Integral import factor, 1 by default, multiplies the integral gain on import errors only, so above 1 the trim leans on import first. Integral reset step, 0 by default, starts the integral from 0 when a command moves by that many watts or more, since what it held trimmed the old level. Import trim wait, 6 s by default, is how long a trim under 50 W that cuts import waits since the last command; a trim towards export always waits 6 s. The Discharge needs a known SOC switch, off by default, allows no discharge until the first SOC is read after a start, since the floor cannot be checked without it. Sensors Limit (what holds the output: floor, soc_unknown, refused, full, warm, a cap, turn_round, spike or none), Battery, Sent and Integral are recorded, so history shows why the grid sits off target. The diagnostic sensor Loop version, and the `start` record of the step log, carry a 12 character hash of `regulator.py` and `controller.py`, so a log says which loop code ran. Set it up from the integrations page or import it from YAML; Configure on the integration shows the same form filled in, and saving it reloads the controller, so a changed entity or topic takes effect without a restart. The tuning numbers are entities and change at once; each has a `group` attribute, Basic, Safety or Advanced, so a dashboard can build its sections from them. Every limit quoted here is one of them, with the value given as its default: the floor margin, the refusal power, time, hold, minimum ask, latch margin and rearm rise, the integral band and limit, the dispatch lease, the arrival lag and wait, the small trim and its wait, the keep warm band, the solar feed forward deadband and gap, the early send gap, the integration gap limit, the 10 W command rounding, the 3000 W default cap, the 20000 W plausible limit, the 90 s correction age, the 10 s response timeout, the 0.5 s correction match wait, and the step response monitor's 500 W minimum step, 4 s target and 30 s timeout. The core in `regulator.py` is pure Python.
- **Controller step log.** The controller writes one JSON line per record to `<config>/alpha2mqtt_controller/<base topic>.jsonl`, or to the Step log directory option. Each line has `k`, its kind, and `t`, Unix time. `step`: one per fast message, enabled or not, with the firmware's `seq`, `ms`, `read_ms`, `grid`, `pv`, `battery`, `soc` and `dispatch`, then the controller's `status`, `grid_w` estimate, `load_w`, `error_w`, `integral_w`, `ff_w`, `request_w`, `output_w`, `wait_ms` spent waiting for the correction, and `sent`, the command it sent or null. `cmd`: every message on `<base>/request/#`, as `topic` and raw payload `p`, so writes by hand show too. `ack`: every set response, the same way. `e`: an entity from the Entities to log option on every change, as `e`, state `s` and attributes `a`. `cfg`: every tuning setting, at start and on each change. `gap`: an input going missing or coming back, as `what` and `missing`, or a policy state the controller does not know, as `what: policy state` with its `reason`. `start` and `stop`. At local midnight the day's file is gzipped to `<file>.YYYY-MM-DD.gz` and the newest 400 are kept. A restart appends; a file left from an earlier day rolls over on the first write.
- **Host tests.** `tests/firmware/run` builds the pure helpers in `Dispatch.h`, `Timer.h`, `ModbusFrame.h`, `PvMeter.h`, `IdleHold.h` and `JsonMembers.h` with the system compiler and checks them without hardware. `python3 -m pytest "Home Assistant/tests"` checks the controller's regulator, response handling and step log.

Commands, topics and JSON are otherwise unchanged from upstream, so its documentation below applies.

### Supported devices are:
```
SMILE5,
SMILE-B3,
SMILE-T10,
Storion T30,
SMILE-B3-PLUS (Confirmed by a user of Alpha2MQTT.)
*Others are likely to work too.
```
![Alpha2MQTT](Pics/Normal.jpg)


Watch the video of the boot up sequence here:

[![Watch the boot up sequence](https://img.youtube.com/vi/3hRjaeRWqJ4/0.jpg)](https://youtu.be/3hRjaeRWqJ4)

Alpha2MQTT (A2M) is a remote control interface for AlphaESS inverters which support Modbus via RS485.

It allows full remote control of the inverter (i.e. forcibly charge the battery from the grid on demand / forcibly discharge the battery to the grid on demand / return to normal mode on demand / setting date / time / PV capacity as displayed on the Alpha Cloud / and a **lot** more and can report various information based on repeating schedules of every 10 seconds, every 1 minute, every 5 minutes, every hour and every day.

Communication with Alpha2MQTT is via MQTT and can be driven by home integration solutions such as [Home Assistant](https://www.home-assistant.io/), [Node-Red](https://nodered.org/) and anything else which is MQTT compatible.

It runs on a Raspberry Pi Pico W with a TTL to RS485 module such as the MAX3485.

Alpha2MQTT honours 1.23 AlphaESS Modbus documentation:

[AlphaESS Modbus Documentation](https://alphaess.de/Public/Uploads/uploadfile/files/20230926/AlphaESS-Handbuch_SMILE_ModBus_RTU_TCP_V21.pdf)

[AlphaESS Register List](https://alphaess.de/Public/Uploads/uploadfile/files/20230926/AlphaESS_Register_Parameter_List.pdf)


[Alternate AlphaESS Modbus Documentation Link](Pics/AlphaESS-Handbuch_SMILET30_ModBus_RTU_V123-DE.pdf)

It is only available in German however Google offers PDF translation which I have used to good effect for English.


## States
To receive information from any of the repeating schedules, subscribe your MQTT client to:
```
Alpha2MQTT/state/fast
Alpha2MQTT/state/second/ten
Alpha2MQTT/state/minute/one
Alpha2MQTT/state/minute/five
Alpha2MQTT/state/hour/one
Alpha2MQTT/state/day/one
```

Alpha2MQTT also publishes its own supply every minute to `Alpha2MQTT/state/device`: VSYS in millivolts and whether USB power is present, for example `{"vsys_mv": 4870, "usb_power": false}`. Use it to watch a battery feeding VSYS.

`Alpha2MQTT/state/fast` is for a controller such as a Home Assistant automation. Every second Alpha2MQTT reads grid power, PV meter power, battery power, SOC and dispatch start back to back and publishes them straight away, with the same keys as the ten second schedule plus four of its own:
```
{
    "REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1": -120,
    "REG_PV_METER_R_TOTAL_ACTIVE_POWER_1": 2310,
    "REG_BATTERY_HOME_R_BATTERY_POWER": -1950,
    "REG_BATTERY_HOME_R_SOC": 64.40,
    "REG_DISPATCH_RW_DISPATCH_START": "Stop",
    "seq": 5123,
    "ms": 5123456,
    "read_ms": 142,
    "haIdle": 0
}
```
seq counts up by one per message and restarts at 1 after a reboot. ms is the Pico's millis() when the reads started and read_ms is how long they took. haIdle is 1 while Home Assistant counts as gone, after 3 minutes without it, and Alpha2MQTT keeps the battery idle, see [About this fork](#about-this-fork). A register that fails to read is left out of that message. So is a PV meter power of exactly 0 straight after a reading 10 W or more from 0: the inverter now and then reports a lone 0 while the PV is generating, so a 0 is only published once a second read confirms it. This applies to every schedule. The other schedules run between fast cycles, so they no longer hold the fast state up.

Each of the schedules can be customised to report a number* / any combination of register values.  Alpha2MQTT has been developed this way to give flexibility.  For example, there is probably no need to report grid voltage every ten seconds, instead once every five minutes and in doing so we are limiting network traffic over MQTT and onward processing in software such as Home Assistant.


### By default

#### Every ten seconds:
```
Battery State Of Charge (%)
Battery Power (+/-) (W)
Battery Voltage (V)
Battery Current (A)
Battery Temp (DegC)
Grid Power (+/-) (W)
Grid Current (+/-) (A)
PV Power (+/-) (W)
Inverter Current (Phase A) (A)
Inverter Power (W)
Inverter Temp (DegC)
Load (W)

Dispatch Start (description as per documentation)
Dispatch Mode (description as per documentation)
Dispatch Power (W) (32000 Offset, < 32000 charge, > 32000 discharge)
Dispatch SOC (%)
Dispatch Time (S)

Total Solar Power (+/-) (W) - Sum of PV Power (CT gathered) and Hybrid PV Power gathered
```

#### Every minute:
```
Grid Voltage (Phase A) (V)
PV Voltage (Phase A) (V)
Inverter Voltage (Phase A) (V)

Battery Total Charge (kWh)
Battery Total Discharge (kWh)
Battery Total Charged From Grid (kWh)
Total System PV Energy Generated (kWh)
Total Energy Fed To Grid (kWh)
Total Energy Consumed From Grid (kWh)
Total PV Energy Generated (kWh)
Total PV Energy Consumed (kWh)

Battery Status, Battery Relay Status (description as per documentation)
Battery Min and Max Cell Voltage (V), with the pack and cell holding each
Battery Min Cell Temperature (DegC), with the pack and cell holding it
Battery Max Cell Temperature pack and cell (the temperature itself is in the ten second schedule)
Battery Max Charge and Discharge Current (A) and Power (W)
Battery Remaining Time (minutes)
Battery Implementation and Remaining Charge and Discharge SOC (%)
Battery Fault (description as per documentation)
Battery Warning (raw)
```

The energy totals report every minute so the Home Assistant Energy dashboard stays current. The one day schedule is empty by default and publishes nothing until you add registers to it.

The HOME series (the SMILE-B3 included) reports the battery as a whole. It names the pack that holds the lowest and highest cell, but has no registers for each pack's own SOC, voltage or temperature; the per BMU registers from `0xA200` are for the INDUSTRY series.

#### Every five minutes:
```
Battery Modules (count)
Battery Capacity (kWh)
Battery Type (description as per documentation)
Battery SOH (%)
Battery Charge and Discharge Cut Off Voltage (V)
Battery BMU, LMU and ISO Software Version
Battery MOS Control, Battery SOC Calibration (description as per documentation)
Battery Single Cut Error Code (raw)
Battery Fault 1 to 6 and Battery Warning 1 to 6 (raw)
```

PV1 to PV6 power (REG_INVERTER_HOME_R_PV1_POWER_1 onwards) are left out because they exist on hybrid inverters only. On a hybrid inverter, add them back to the ten second schedule.

#### Every one hour:
```
Grid Frequency (Hz)
PV Frequency (Hz)
Inverter Frequency (Hz)
System Fault (description as per documentation)
```

You can customise the schedules by modifying Alpha2MQTT.ino.  Search for 'Schedules' and add or remove registers as you see fit from each schedule.  The supported registers are the table in Registers.h; their addresses are in Definitions.h.  A register name which contains _R_ is read only, one which contains _RW_ is read/write, and one which contains _W_ is write only.

An example response for any subscribed state is a JSON of name/value pairs which are separated by commas, for example:
```
{
    "REG_BATTERY_HOME_R_BATTERY_POWER": 2845,
    "REG_INVERTER_HOME_R_VOLTAGE_L1": 238.4
}
```

## Advanced Read Registers
Appreciating that some people may want to take inverter values in raw form with extra information, Alpha2MQTT supports request and responses for individual registers.  It does this by offering two ways, handled and raw.  A handled register and raw register is essentially the same request to the inverter, however when requesting via the handled route, checks, calculations and balances are done in Alpha2MQTT and the response includes both raw and formatted (as per Modbus documentation) data and information.  For example, where the Modbus documentation indicated a number should undergo manipulation to return something of value, i.e. frequency which needs to be multiplied by 0.01 to return Hz, then a handled read request will return the raw data, as well as the formatted data which underwent calculations.  A handled request for the EMS serial number (ALxxxxxxxxxxxxxxx) will return just that, rather than a series of numbers which need manipulation by you.

### Handled Read
Publish MQTT messages to:
```
Alpha2MQTT/request/read/register/handled
```

With the following JSON
```
{
    "registerAddress": "0x0010"
}
```
where
registerAddress is the hex address of the register as per the documentation, and where the register is a handled register in Definitions.h

Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/read/register/handled
```

An example response for the above request could be:
```
{
    "responseStatus": "readDataRegisterSuccess",
    "registerAddress": "0x0010",
    "functionCode": 3,
    "registerName": "REG_GRID_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1",
    "dataType": "unsignedInt",
    "dataValue": 123515,
    "formattedDataValue": 12351.50,
    "rawDataSize": 4,
    "rawData": [0,1,226,123],
    "end": "true"
}
```

* If the register returns a character dataType then dataValue and formattedDataValue will be in double quotes.

* If the register is a lookup, i.e. 0x1000 (REG_SAFETY_TEST_RW_GRID_REGULATION) Grid_Regulation, formattedDataValue will bring back the appropriate textual lookup, i.e dataValue of 8, formattedDataValue of "CEB".

* 0x0743 (REG_SYSTEM_INFO_R_EMS_SN_BYTE_1_2) EMS SN byte1-2 is the only register which undergoes custom processing in Alpha2MQTT different to spec.  It returns the full 15 character ALxxxxxxxxxxxxxxx serial number in characterValue in one go, and the remaining EMS SN byte-x-y registers are not implemented as they are essentially pointless.

* There is a custom handled register address of 0xFFFF (REG_CUSTOM_SYSTEM_DATE_TIME) which returns the full system date/time in UK dd/MMM/yyyy HH:mm:ss format in formattedDataValue.

* There is a custom handled register address of 0xFFFE (REG_CUSTOM_LOAD) which returns current consumption, also known as load.  It returns in watts.


### Raw Read
Alpha2MQTT supports over 200 registers via the handled route, however it does not cater for registers in the Safety TEST, ATE TEST, CT calibration and Battery - INDUSTRY series categories (with the exception of 0x1000 Grid_Regulation.)  This is because there are many more hundreds of registers in these categories, they are rather niche and on my inverter (SMILE B3) most I cannot query and test.  As such, by providing a raw read functionality Alpha2MQTT can expose any of these registers to advanced users and it will return the raw data bytes for onward processing as you see fit.

Publish MQTT messages to:
```
Alpha2MQTT/request/read/register/raw
```
With the following JSON
```
{
    "registerAddress": "0x0743",
    "dataBytes": 2
}
```
where

registerAddress is the hex address of the register as per the documentation.

dataBytes is the number of bytes to request, as per the Data format column in the Alpha documentation.  Usually follow the documentation, however for registers such as serial numbers / date times you can actually pass more and the Alpha system will duly return more.  For example, the documentation for EMS SN byte1-2 (0x0743) is two bytes
however you can request 16.
```
{
    "registerAddress": "0x0743",
    "dataBytes": 16
}
```

Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/read/register/raw
```

An example response for the above request could be:
```
{
    "responseStatus": "readDataRegisterSuccess",
    "registerAddress": "0x0743",
    "functionCode": 3,
    "rawDataSize": 16,
    "rawData": [65,76,55,48,48,49,48,50,49,48,54,48,51,50,49,0],
    "end": "true"
}
```

## Writing
The AlphaESS documentation mentions two methods to write to registers, "Write Single Register" and "Write Data Register."  Alpha2MQTT supports both.  That said, when I started developing Alpha2MQTT I presumed Write Single Register was an easier way to write a two-byte register, requiring only a value, however my inverter never responds to any Write Single Register request.  The documentation on how to make a request and obtain the response is below, however it is only provided as *Guidance*.  Write Data Register works with any two or four byte register and so for me, for my Smile B3 at least, is the go-to function.

### Write Raw Single Register
Publish MQTT messages to:
```
Alpha2MQTT/request/write/register/raw/single
```
With the following JSON
```
{
    "registerAddress": "0x084F",
    "value": 1
}
```
where

registerAddress is the hex address of the register as per the documentation.

value is the base 10 integer you wish to write to the register.  For example, the above will write 1 to the Time period control flag register and Enable charge time period control and disable discharge time period control.

Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/write/register/raw/single
```

An example response for the above request could be:
```
{
    "responseStatus": "writeSingleRegisterSuccess",
    "registerAddress": "0x084F",
    "functionCode": 6,
    "rawDataSize": 4,
    "rawData": [8,79,0,1]
}
```

In reality, you will probably only ever get the following back:
```
{
    "responseStatus": "noResponse",
    "registerAddress": "0x084F"
}
```
In raw data, the Alpha documentation says byte one is the high byte of the register address, byte two is the low byte of the register address, byte three is the high byte of the value and byte four is the low byte of the value.
Again, for me, this function does nothing and I only provide the above as guidance as to how it should work.  Instead, I recommend just using Write Raw Data Register as documented below.

The same applies to the inverter's own discharge floor, register `0x0850`. An inverter that ignores Write Raw Single Register gives no reply, so the Pico reports `noResponse` after its timeout and the floor stays as it was. Write it as a two byte Write Raw Data Register instead. Some units hold whole percent in this register, so `20` is a 20 % floor, even though the protocol document says 0.1 % per bit. Read it back first to learn which your inverter does. Then read it back after the write as well. A SMILE-B3 on EMS 1.03.78 answers a Write Raw Data Register to `0x0850` with `writeDataRegisterSuccess` and keeps the old value unless the matching bit of the time period control flag `0x084F` is set at the time of the write: bit 1 (value 2, discharge time control) unlocks `0x0850` to `0x0854`, bit 0 (value 1, charge time control) unlocks `0x0855` to `0x0859`. The flag itself always takes a write. So read `0x084F`, write it back with bit 1 set, write `0x0850`, then write `0x084F` back to what it was. The new floor stays after the flag is cleared. The dispatch block `0x0880` to `0x0887` needs none of this.


### Write Raw Data Register
Publish MQTT messages to:
```
Alpha2MQTT/request/write/register/raw/data
```
With the following JSON
```
{
    "registerAddress": "0x0881",
    "dataBytes": 4,
    "value": 30000
}
```
where

registerAddress is the hex address of the register as per the documentation.

dataBytes is the number of bytes to write as appropriate for the register in question as per the Data Format column.

value is the base 10 integer you wish to write to the register.  For example, the above will write 30000 to the Dispatch active power register to essentially set battery charging to 2000W.

* If the register has documentation which suggests values are in hex, keep in mind they need to be written in a single base 10 value (standard numbers you count with) regardless of whether you are writing to a single two-byte register or a double four-byte register.  And all hex values are read out in base 10 bytes, each between 0 and 255.  What does this mean exactly?  Take register 0x080F, the Modbus address.  The documentation refers to the value being 0x55 in hex.  This is true, however this will be read out in rawData as 0, 85.  If you wanted to change the Modbus address to 0x56, you would use a value of 86.  This would be read out in rawData as 0, 86.

* There is a hex starter for ten contained in the README.txt file.


Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/write/register/raw/data
```

An example response for the above request could be:
```
{
    "responseStatus": "writeDataRegisterSuccess",
    "registerAddress": "0x0881",
    "functionCode": 16,
    "rawDataSize": 4,
    "rawData": [8,129,0,2],
    "end": "true"
}
```
In raw data, the Alpha documentation says byte one is the high byte of the register address, byte two is the low byte of the register address, byte three is the high byte of the number of registers and byte four is the low byte of the number of registers.  As this was a four byte register, in reality this is a double register, hence 2 in the last byte.

You can technically be smart and write two registers at once if they are next to each other, but things are complex enough as they are so I advise you don't.

If you take registerAddress of 0x0851 (Discharge Start Time 1,) it is next to registerAddress 0x0852 (Discharge Stop Time 1) and they are two bytes each.  So technically you can get away with writing a single value in to four dataBytes.

If done individually:
```
{
    "registerAddress": "0x0851",
    "value":1,
    "dataBytes":2
}
```
```
{
    "registerAddress": "0x0852",
    "value":4,
    "dataBytes":2
}
```
These two instructions will set start time 1 to 01:00 and stop time 1 to 04:00 respectively.

But combined:
```
{
    "registerAddress": "0x0851",
    "value":65540,
    "dataBytes":4
}
```

Why 65540?
```

                1AM             4AM
00000000 00000001 00000000 00000100 = 65540 in base 10 decimal
```

# WARNING
Double and triple check the dataBytes you send corresponds to the register in the Alpha Documentation.  Writing more or less may have unexpected outcomes!

Double and triple check the value you send.  Writing an invalid or incompatible value may have unexpected outcomes!

I have written to most of the R/W registers where I have had an interest in doing so, and it appears AlphaESS have developed this to be pretty robust where it just disregards junk requests, however please, employ discretion at all times!


## Slave Errors
If the request and response was successful, but the inverter failed to carry out the task for some reason, it will return a slave error.  The response to any request above will be of the following format if so:
```
{
    "responseStatus": "slaveError",
    "functionCode": 16,
    "slaveErrorCode": 321
    "end": "true"
}
```
where

slaveErrorCode is an unknown number.  Note that slave errors are not covered in the AlphaESS documentation, and not having received a slave error response I cannot give guidance as to what would cause errors, or what potential slaveErrorCodes could be.




## Force Charge From Grid
As we now have nifty functionality to command the inverter as we see fit, Alpha2MQTT has some MQTT topics to send messages to to instruct it to charge from the grid.  All this is doing is essentially batching up some appropriate Write Data Register commands and exposing them as a single MQTT topic for ease of use.

Publish MQTT messages to:
```
Alpha2MQTT/request/set/charge
```

With the following JSON
```
{
    "watts": 2500,
    "socPercent": 100,
    "duration": 14400,
    "seq": 42
}
```
where

watts is the power at which you want to charge the batteries with.  You can specify a number greater than the inverter/battery supports, the system will limit accordingly.  They provide this functionality in case you don't want to hammer your batteries at full power.  Note that it will charge at this rate utilising PV power too.  As such, if you set watts to 2500 and PV is generating 1000, it will pull 1500 from the grid.  As I suspect this will be largely used during the night when electricity rates are cheaper (such as Octopus Go / Economy 7) then this is largely irrelevant.

socPercent is where you want charging to stop.  If you want to stop charging at 80% as you believe the weather tomorrow will do the rest, set this to 80.  Otherwise, usually it'll be 100.

duration is how long in seconds to put the inverter in this mode.  Charging from the grid uses 'Dispatch' mode where an external device ala Alpha2MQTT can, for a specified time period, instruct what it does, however I believe for safety Alpha require you to specify how long it'll be in dispatch mode so it can always end up back in the inverter's control.  As such, just put a duration in seconds for how long usually it takes to charge the battery.  If you set to 14400 (4 hours) that'll cover off most domestic systems.  Ultimately it's a calculation based on kWh of battery capacity divided by the rate you want to charge plus some tolerance, say 5%.  For my 11.47kWh B3 which can charge at circa 3000W I am going to be utilising 3000 as the watts and duration as (11000 / 2800 (allowing for usable capacity / actual max charge)) + 5% for 3.92 hours + 5% = 4.125 hours = 15000 seconds.  After the 15000 seconds control will be released and the battery will be able to discharge to the load of the house.  You can alternatively write 0 to register 0x0880 to turn off dispatch mode manually using Write Data Register when you have read an SOC of 100 via register 0x0102 using the Read Handled Register functionality of Alpha2MQTT via some periodically driven automated process in Node Red or Home Assistant.

seq is optional, on this and the other set requests.  Alpha2MQTT returns it in the response, with ms, its millis() when the writes finished, so a controller can match each response to its request and time the round trip.  Without it the response carries a seq of 0.  Charge and discharge write the block as one Write Data Register of nine registers, `0x0880` to `0x0888` (start 1, active power, reactive power 32000 for none, mode 2, SOC, time), so expect the response about 100 ms after the request.  If the inverter refuses that write, the five single writes it always had go out 400 ms apart, the response takes about two seconds and carries `"frames": 5`; a one frame write carries `"frames": 1`.

powerOnly is optional, on charge and discharge only.  With `"powerOnly": 1`, Alpha2MQTT writes just the power register (`0x0881`, one Write Data Register of two registers, 32000 offset) if the dispatch it last wrote in full has the same kind, SOC and duration, every write since has succeeded, and less than half the duration has passed since that full write.  That takes one bus transaction, about 30 to 40 ms at 9600 baud.  Otherwise it writes the whole block as above.  A power-only write does not rewrite the dispatch time, and power-only writes stop half way into the duration, so the inverter returns to normal within about one and a half durations of the last full write even if it restarts its timer on a power write.  Any failed write, any raw register write, a set/normal and a reboot all make the next request a full write.  Whether an inverter acts on a power write while a dispatch runs is not in the AlphaESS documentation, so check it on yours before relying on it.


Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/set/charge
```

An example response for the above request could be:
```
{
    "responseStatus": "setChargeSuccess",
    "failureDetail": "",
    "write": "block",
    "seq": 42,
    "ms": 5123456
}
```
where

failureDetail will document at what point in the dispatch process the failure occurred.

write is `power` when only the power register was written and `block` when the whole block was.


## Force Discharge To Grid
As we now have nifty functionality to command the inverter as we see fit, Alpha2MQTT has some MQTT topics to send messages to to instruct it to discharge to the grid.  All this is doing is essentially batching up some appropriate Write Data Register commands and exposing them as a single MQTT topic for ease of use.

Publish MQTT messages to:
```
Alpha2MQTT/request/set/discharge
```

With the following JSON
```
{
    "watts": 2500,
    "socPercent": 10,
    "duration": 1800
}
```
where

watts is the power at which you want to discharge the batteries with.  You can specify a number greater than the inverter/battery supports, the system will limit accordingly.  They provide this functionality in case you don't want to hammer your batteries at full power.

socPercent is where you want discharging to stop.  If you want to stop discharging at 80% as you want to retain the bulk of your stored power, set this to 80.

duration is how long in seconds to put the inverter in this mode.  Discharging to the grid uses 'Dispatch' mode so carries the same caveat as described above in Force Charge From Grid.  I suspect this mode will be leveraged for certain times of day where payments per kWh can be quite rewarding.  I suspect setting a duration in the order of an hour or two will do for the bulk, however I would fully expect Dispatch mode to be terminated by writing 0 to register 0x0880 manually using Write Data Register at the moment you want charging to stop.  It is up to you.


Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/set/discharge
```

An example response for the above request could be:
```
{
    "responseStatus": "setDishargeSuccess",
    "failureDetail": "",
    "write": "block",
    "seq": 0,
    "ms": 5123456
}
```
where

failureDetail will document at what point in the dispatch process the failure occurred.


## Force Back To Normal
And if you want to come out of Force Charge From Grid / Force Discharge To Grid / Any other Dispatch mode, Alpha2MQTT has a similar topic you can leverage.  This topic essentially does a Write to register 0x0880 with value 0, to stop dispatch.

Publish MQTT messages to:
```
Alpha2MQTT/request/set/normal
```

With the following JSON
```
{
    "void": 0
}
```
where

void is nothing, it just ensures there is some JSON to parse.


Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/set/normal
```

An example response for the above request could be:
```
{
    "responseStatus": "setNormalSuccess",
    "failureDetail": "",
    "seq": 0,
    "ms": 5123456
}
```
where

failureDetail will document at what point in the dispatch process the failure occurred.


## Read All Handled Registers
Finally, an option similar to state, however is done on a request/response basis, is to request batches of handled registers in Alpha2MQTT.  This is intensive and so is only recommended to be pulled when absolutely necessary.  Due to memory limitations as previously explained, it is recommended you pull in batches of 70.

Publish MQTT messages to:
```
Alpha2MQTT/request/read/register/handled/all
```

With the following JSON
```
{
    "start": 0,
    "end": 70
}
```
where

start is the index of the first register in the handled register list to get.  They start at zero and end around 200.

end is the index of the last register in the handled register list to get.  You don't need to worry about specifying an exact end position, Alpha2MQTT will stop at the last one.

It is recommended you pull 0-70, 71,140, and so on, until you have obtained them all.

Alpha2MQTT will do the rest and will return the response via the following topic which you can subscribe to:
```
Alpha2MQTT/response/read/register/handled/all
```

An example response for the above request will be in the likes of:
```
{
    "REG_BATTERY_HOME_R_BATTERY_POWER": 2845,
    ... repeat 40 or so times ...
    "REG_INVERTER_HOME_R_VOLTAGE_L1": 238.4
}
```

## Buffer Problems
If at any point you request too much data for the MQTT buffer, you will receive the following payload back:
```
{
  "mqttError": "Length of payload exceeds X bytes.  Length would be Y bytes."
}
```
where

X is the number of bytes available in the buffer (max payload size - 1).

Y is the number of bytes ended up being requested.



I will endeavour to help you if you are struggling.  Try email or [Facebook](https://www.facebook.com/mydan100/).

(c) Daniel Young 2022 [daniel@mydan.com](daniel@mydan.com)


Thanks to Colin McGerty's (colin@mcgerty.co.uk) work on Sofar2MQTT which brought about my intrigue and on which some of Alpha2MQTT's program logic and circuitry is based.

Thanks to the AlphaESS DE Team (https://www.alpha-ess.de/) for latest Modbus documentation and email support.

calcCRC by angelo.compagnucci@gmail.com and jpmzometa@gmail.com


# How To Build
## Parts List
1. Raspberry Pi Pico W

2. MAX3485 or MAX485 TTL to RS485 board*

3. A small project board

4. A few wires and a little solder


*The MAX3485 (red) is preferred as it is much more stable because it uses 3.3v logic, just like the Pico W. The MAX485 (blue) uses 5v logic but is somewhat tolerant of 3.3v and is generally cheaper and more widely available.  I use a MAX3485 and I recommend you do too.  MAX3485 boards do not have DR and RE flow control pins; the wiring below covers both.

![Prototyping Board](Pics/PrototypingBoard.png)

I bought [this prototyping board](https://www.ebay.co.uk/itm/265235487263)


![MAX3485](Pics/MAX3485.png)

I bought [this MAX3845](https://www.ebay.co.uk/itm/403668533766)


![Wire](Pics/Wire.png)

For wire I suggest you strip a standard network cable and use wires from that.  You will probably have plenty of 1m cables spare which came with your broadband router, there are plenty of colours to choose from and it is a thin robust wire which will bend easily - and you can source them from the offcuts of the RS485 cable you'll be making - more on that later.

## Steps
### Cut the project board to a convenient size if you like.  I kept mine at 5cm x 7cm
### Wire the components to these pins.

MAX3485 to Pico W
VCC to 3V3 OUT (pin 36)
GND to GND (pin 38)
RXD to GP0 (pin 1, UART0 TX)
TXD to GP1 (pin 2, UART0 RX)
EN or DR+RE to GP2 (pin 4) (only for devices with flow control pins)

On the 845, DI goes where RXD does and RO where TXD does.  If using devices with DR and RE pins, connect them both to GP2.

If using the 845, you may want to invest in a 3.3V -> 5V bi directional logic shifter in-between GP0, GP1 and GP2 so that output from those pins is lifted to 5V to be in tolerance of the 845 and likewise, 5V reduced back to 3.3V for input.  If you do use a logic shifter 5V would be used to power the 845, not 3.3V.

I don't recommend soldering the anything permenantly to the prototyping board, besides headers and wiring.  This way, if you want to transfer the boards elsewhere it is just a case of unplugging them.

Build with [arduino-pico](https://github.com/earlephilhower/arduino-pico) 6.2.0 and the 1 MB sketch, 1 MB filesystem flash split. OTA updates stage the new image in that filesystem:
```
arduino-cli compile --fqbn rp2040:rp2040:rpipicow:flash=2097152_1048576 --output-dir out Alpha2MQTT
```
The first time, and whenever the flash split changes, flash over USB: hold BOOTSEL while plugging in the Pico W, then copy `out/Alpha2MQTT.ino.uf2` onto the RPI-RP2 drive.

After that, update over WiFi. Set `OTA_PASSWORD` in Secrets.h, then run:
```
scripts/ota-upload              # or: scripts/ota-upload 192.0.2.20
```
It builds with the settings above and uploads to `alpha2mqtt.local` (the device name) with the core's `espota.py`, reading the password from Secrets.h. The Pico stops polling while it receives the image, then reboots into the new firmware. A dispatch already running on the inverter carries on until its duration runs out.


Here's how it looks when completed.  I ended up using an RJ45 socket and soldered the appropriate pair directly to the MAX3845 RX and TX pins so that I could simply plug/unplug the ethernet cable, without needing to unscrew/screw terminals on the MAX3845 every time.

![Finished](Pics/Connecting.jpg)
![Finished](Pics/Underside.jpg)

If you are not comfortable soldering, appreciating the rather insignificant cost of the parts involved, for a small fee I will happily build and post you a working unit, and it will be a lot nicer looking than my tesbed unit!

# Flashing
## Configuration
Download the source files into a folder called Alpha2MQTT.  The source files needed are Alpha2MQTT.ino, Definitions.h, Dispatch.h, IdleHold.h, JsonMembers.h, ModbusFrame.h, PvMeter.h, Secrets.example.h, Timer.h, RegisterHandler.cpp, RegisterHandler.h, RS485Handler.cpp, RS485Handler.h.

Configure Alpha2MQTT by opening up Definitions.h and verifying/customising the following definitions for your need:
```
- Copy Secrets.example.h to Secrets.h. Git ignores Secrets.h, so your credentials stay out of commits.
- In Secrets.h set your WiFi name and password, MQTT broker server and port (default 1883).
- Set your MQTT username and password in Secrets.h if you use security (you should), or leave the username blank to connect without credentials.
- Set OTA_PASSWORD in Secrets.h to a long random string. It guards firmware uploads over WiFi.
- Set your Alpha2MQTT device name in DEVICE_NAME.  This is the device name presented on your network and is also how MQTT topics begin.  This document assumes Alpha2MQTT.
- Set your AlphaESS inverter's slave id in ALPHA_SLAVE_ID.  By default this is 0x55 and shouldn't need changing unless you've changed it via Modbus or via inverters which have an integrated display.  Don't change it.
- Set your maximum payload size in MAX_MQTT_PAYLOAD_SIZE.  4096 bytes is enough for 70 to 80 registers on any schedule or request.
- Set whether the device should auto restart every so many hours.  This is for specific routers only.  Uncomment FORCE_RESTART if you want to use this feature
- Set the number of hours for an automatic restart in FORCE_RESTART_HOURS
```

## Software Configuration
### Arduino IDE
Download and install the [Arduino IDE](https://www.arduino.cc/en/software).
### Pico W board package
Once installed and set up, add the [arduino-pico](https://github.com/earlephilhower/arduino-pico) board package, version 6.2.0.
### Other Libraries
Add PubSubClient using the Tools / Manage Libraries menu...

...and upload.

After uploading, you should see activity on your device.  Ideally it will connect to WiFi (confirming those details are right) and connect to MQTT (confirming those details are right).  Congratulations.  Unplug from your computer and source a power supply for the same USB cable.

## Connecting to your AlphaESS
You now need a suitable wire to connect to your AlphaESS system.  Luckily, all documented systems in the Modbus documentation indicate that it is a standard RJ45 (the same port you find on your broadband router) and my Smile B3 was no exception.

![B3 Ports](Pics/B3Circuitry.jpg)

In the Modbus documentation it indicates that the CAN/RS485 port uses a twisted pair (will always be a pair for insulation) of A+/B- pins, and the Installation manual indicates pins 3 and 6 for RS485, with no indication of which is + and which is negative.  I took wires 3 and 6 as per standard RJ45 below (Green White / Green), plugged them in... and got nothing.  I reversed them around (not knowing which is + or -) and got... nothing.

![RJ45](Pics/RJ45Pinout.PNG)

At first there were lots of questions.

Do Alpha enable Modbus out of the box?

Is my coding right?

Is my circuit wired up right?

After lots of checking and debugging, for my B3, I took a spare network cable from my router box, and cut off one end and trimmed back the sheath.  I split the pairs into Orange White + Orange / Green White + Green / Blue White + Blue / Brown White + Brown, and plugged the other end in the inverter.  I used a multi-meter against all of the pairs to see which one (if any) brought back a voltage.  I found a steady voltage (4.6V) across the Blue White + Blue pins, with Blue White being positive.  For clarity, Blue (Pin 4) was B-, Blue White (Pin 5) was A+.
I cannot guarantee this is the same for every model, but it *probably* is.

So what I ended up with was a hybrid cable looking like so:

![Cable](Pics/B3cable.jpg)

Connect the Alpha2MQTT unit to the 5v micro USB power supply you just found.  Connect the RJ45 to the CAN/RS485 port on your inverter and connect Blue White to A+ and Blue to B-.  (If your inverter used a different set of colours, please let me know so I can update the documentation.)


# Troubleshooting
## No communication
Check A+/B- are correct using your hybrid cable.  The colours used will always be a pair for shielding, so you could always try individual pairs one at a time, and if it doesn't work one way, swap them around and see if it works the other.

Make sure the Slave IDs match.  Alpha systems use 0x55 by default but if you have changed it on an inverter with an on device interface, change the appropriate line in Definitions.h as documented above.

## Latest version
Check that you have the latest version at https://github.com/msegec/Alpha2MQTT-PiPicoW, or upstream at https://github.com/dxoverdy/Alpha2MQTT

### Alpha2MQTTs Around The World
If you have your own Alpha2MQTT, be sure to send me a picture of it in action so I can add it to this page!



AC from UK - Even got a box sorted for it!

<img src="Pics/AC.jpg" width="500"/>



GS from UK - Wall mounted using grips!

<img src="Pics/GS.jpg" width="500"/>



DS from UK - Used his Alpha2MQTT together with a self-build ePaper device to report stats in his house in real time!

<img src="Pics/DSUK1.jpg" width="500"/>

<img src="Pics/DSUK2.jpg" width="500"/>



MK from AU - Very much a minimalist build, but demonstrates that just two cheap components can get a unit up and running!

<img src="Pics/MK.png" width="500"/>



DC from UK - Thanks for your input regardings stubborn Smile 5 debugging!

<img src="Pics/dc1.jpg" width="500"/>
<img src="Pics/dc2.jpg" width="500"/>
<img src="Pics/dc3.jpg" width="500"/>



DM from UK!

<img src="Pics/dm.jpg" width="500"/>



PN from CR!

<img src="Pics/pn1.jpg" width="500"/>
<img src="Pics/pn2.jpg" width="500"/>
<img src="Pics/pn3.jpg" width="500"/>
