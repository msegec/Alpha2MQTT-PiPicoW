// Host tests for Registers.h. Run with tests/firmware/run.
#include "Registers.h"
#include <cstdio>

static int failures = 0;

static void expect(bool ok, const char* what)
{
	if (!ok)
	{
		printf("FAIL: %s\n", what);
		failures++;
	}
}

// What readHandledRegister does with a register's response bytes, without the bus
static modbusRequestAndResponse handled(uint16_t address, const uint8_t* bytes, const char* serialNumberPrefix = "AL")
{
	modbusRequestAndResponse rs;
	const registerDescriptor* reg = findRegister(address);
	describeRegister(reg, &rs);
	rs.dataSize = rs.registerCount * 2;
	memcpy(rs.data, bytes, rs.dataSize);
	decodeRegisterValue(&rs);
	formatRegisterValue(reg, &rs, serialNumberPrefix);
	return rs;
}

static bool reads(uint16_t address, const char* name, modbusReturnDataType type, uint8_t count, bool hasLookup)
{
	const uint8_t zero[4] = { 0, 0, 0, 0 };
	modbusRequestAndResponse rs = handled(address, zero);
	return strcmp(rs.mqttName, name) == 0 && rs.returnDataType == type && rs.registerCount == count && rs.hasLookup == hasLookup;
}

static bool formats(uint16_t address, const uint8_t* bytes, const char* expected)
{
	return strcmp(handled(address, bytes).dataValueFormatted, expected) == 0;
}

int main()
{
	const unsigned int count = sizeof(handledRegisters) / sizeof(handledRegisters[0]);
	expect(count == 206, "206 handled registers");
	for (unsigned int i = 0; i < count; i++)
	{
		for (unsigned int j = i + 1; j < count; j++)
		{
			expect(handledRegisters[i].address != handledRegisters[j].address, "every address is handled once");
		}
		// HANDLED_REGISTER stringifies the define, so a name can only be a REG_ define's own name
		expect(strncmp(handledRegisters[i].name, "REG_", 4) == 0, "every name is a REG_ define");
		expect(strlen(handledRegisters[i].name) < MAX_MQTT_NAME_LENGTH, "every name fits mqttName");
		expect(findRegister(handledRegisters[i].address) == &handledRegisters[i], "every register is found by its address");
	}
	expect(findRegister(0x0002) == NULL, "an address between registers is not handled");

	expect(reads(0x0021, "REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1", modbusReturnDataType::signedInt, 2, false), "grid power");
	expect(reads(0x00A1, "REG_PV_METER_R_TOTAL_ACTIVE_POWER_1", modbusReturnDataType::signedInt, 2, false), "PV meter power");
	expect(reads(0x0126, "REG_BATTERY_HOME_R_BATTERY_POWER", modbusReturnDataType::signedShort, 1, false), "battery power");
	expect(reads(0x0102, "REG_BATTERY_HOME_R_SOC", modbusReturnDataType::unsignedShort, 1, false), "SOC");
	expect(reads(0x0880, "REG_DISPATCH_RW_DISPATCH_START", modbusReturnDataType::unsignedShort, 1, true), "dispatch start");
	expect(reads(0x0881, "REG_DISPATCH_RW_ACTIVE_POWER_1", modbusReturnDataType::signedInt, 2, false), "dispatch active power");

	const uint8_t gridExport[] = { 0xFF, 0xFF, 0xFC, 0x18 };
	expect(formats(0x0021, gridExport, "-1000"), "grid power -1000 W is feeding");
	const uint8_t pv[] = { 0x00, 0x00, 0x0B, 0xB8 };
	expect(formats(0x00A1, pv, "3000"), "PV meter 3000 W");
	const uint8_t batteryCharge[] = { 0xFC, 0x18 };
	expect(formats(0x0126, batteryCharge, "-1000"), "battery -1000 W is charging");
	const uint8_t soc[] = { 0x03, 0x8E };
	expect(formats(0x0102, soc, "91.00"), "SOC counts 0.1 % per bit");
	const uint8_t start[] = { 0x00, 0x01 };
	expect(formats(0x0880, start, "Start"), "dispatch start 1 is Start");
	const uint8_t stop[] = { 0x00, 0x00 };
	expect(formats(0x0880, stop, "Stop"), "dispatch start 0 is Stop");
	const uint8_t startMiss[] = { 0x00, 0x07 };
	expect(formats(0x0880, startMiss, "Unknown"), "a dispatch start with no description is Unknown");
	const uint8_t discharge[] = { 0x00, 0x00, 0x7E, 0xF4 };
	expect(formats(0x0881, discharge, "32500"), "dispatch active power keeps the 32000 offset");

	// System date and time: 0xYYMM, 0xDDHH, 0xmmss read as one custom register
	const uint8_t when[] = { 0x11, 0x09, 0x11, 0x09, 0x05, 0x3B };
	modbusRequestAndResponse dated = handled(REG_CUSTOM_SYSTEM_DATE_TIME, when);
	expect(strcmp(dated.dataValueFormatted, "17/Sep/2017 09:05:59") == 0, "the system time reads as dd/MMM/yyyy HH:mm:ss");
	expect(dated.characterValue[0] == 0, "the system time leaves no character value");
	const uint8_t earlyYear[] = { 0x05, 0x01, 0x01, 0x00, 0x00, 0x00 };
	expect(formats(REG_CUSTOM_SYSTEM_DATE_TIME, earlyYear, "01/Jan/2005 00:00:00"), "a one digit year keeps its leading zero");
	const uint8_t monthZero[] = { 0x18, 0x00, 0x01, 0x00, 0x00, 0x00 };
	expect(formats(REG_CUSTOM_SYSTEM_DATE_TIME, monthZero, "01/---/2024 00:00:00"), "month 0 does not index before the month names");
	const uint8_t monthHigh[] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
	expect(formats(REG_CUSTOM_SYSTEM_DATE_TIME, monthHigh, "255/---/20255 255:255:255"), "month 255 does not index past the month names");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
