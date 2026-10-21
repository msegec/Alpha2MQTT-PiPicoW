// The registers readHandledRegister handles: name, type and size to read each one, and how to format its
// value. Pure, no Arduino types, so tests/firmware builds it on the host. RegisterHandler does the reading.
#ifndef _Registers_h
#define _Registers_h

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "Definitions.h"

// How formatRegisterValue writes dataValueFormatted
enum class registerFormat : uint8_t
{
	value,			// The typed value as an integer
	tenths,			// The typed value times a scale, two decimals
	hundredths,
	gridVoltage,
	cellVoltage,
	inverterTemp,
	totalEnergy,
	dispatchSoc,	// The 0.4 % per bit SOC register as a whole percent
	ipAddress,		// Four bytes as a dotted quad
	text,			// characterValue
	textMoved,		// characterValue, which is then cleared
	lookup,			// The description the register's lookup gives the value
	systemFault,	// The description of the lowest fault bit, which differs between AL and AE systems
	dateTime		// Formatted by RegisterHandler
};

// A value, or for a bit lookup a bit number, and its description
struct registerLookupEntry
{
	uint16_t value;
	const char* desc;
};

// With no matching entry formatRegisterValue writes unknown, or leaves dataValueFormatted as it was if unknown is NULL.
// A bit lookup gives the first entry whose bit is set.
struct registerLookup
{
	const registerLookupEntry* entries;
	uint8_t count;
	bool bits;
	const char* unknown;
};

#define REGISTER_LOOKUP(entries, bits, unknown) { entries, sizeof(entries) / sizeof(entries[0]), bits, unknown }

static const registerLookupEntry batteryStatusEntries[] =
{
	{ BATTERY_STATUS_CHARGE0_DISCHARGE0, BATTERY_STATUS_CHARGE0_DISCHARGE0_DESC },
	{ BATTERY_STATUS_CHARGE0_DISCHARGE1, BATTERY_STATUS_CHARGE0_DISCHARGE1_DESC },
	{ BATTERY_STATUS_CHARGE1_DISCHARGE0, BATTERY_STATUS_CHARGE1_DISCHARGE0_DESC },
	{ BATTERY_STATUS_CHARGE1_DISCHARGE1, BATTERY_STATUS_CHARGE1_DISCHARGE1_DESC },
	{ BATTERY_STATUS_CHARGE2_DISCHARGE0, BATTERY_STATUS_CHARGE2_DISCHARGE0_DESC },
	{ BATTERY_STATUS_CHARGE2_DISCHARGE1, BATTERY_STATUS_CHARGE2_DISCHARGE1_DESC },
};
static const registerLookup batteryStatusLookup = REGISTER_LOOKUP(batteryStatusEntries, false, "Unknown");

static const registerLookupEntry batteryRelayStatusEntries[] =
{
	{ BATTERY_RELAY_STATUS_CHARGE_AND_DISCHARGE_RELAYS_CLOSED, BATTERY_RELAY_STATUS_CHARGE_AND_DISCHARGE_RELAYS_CLOSED_DESC },
	{ BATTERY_RELAY_STATUS_CHARGE_DISCHARGE_RELAYS_NOT_CONNECTED, BATTERY_RELAY_STATUS_CHARGE_DISCHARGE_RELAYS_NOT_CONNECTED_DESC },
	{ BATTERY_RELAY_STATUS_ONLY_CHARGE_RELAY_CLOSED, BATTERY_RELAY_STATUS_ONLY_CHARGE_RELAY_CLOSED_DESC },
	{ BATTERY_RELAY_STATUS_ONLY_DISCHARGE_RELAY_CLOSED, BATTERY_RELAY_STATUS_ONLY_DISCHARGE_RELAY_CLOSED_DESC },
};
static const registerLookup batteryRelayStatusLookup = REGISTER_LOOKUP(batteryRelayStatusEntries, false, "Unknown");

static const registerLookupEntry batteryTypeEntries[] =
{
	{ BATTERY_TYPE_M4860, BATTERY_TYPE_M4860_DESC },
	{ BATTERY_TYPE_M48100, BATTERY_TYPE_M48100_DESC },
	{ BATTERY_TYPE_48112_P, BATTERY_TYPE_48112_P_DESC },
	{ BATTERY_TYPE_SMILE5_BAT, BATTERY_TYPE_SMILE5_BAT_DESC },
	{ BATTERY_TYPE_M4856_P, BATTERY_TYPE_M4856_P_DESC },
	{ BATTERY_TYPE_SMILE_BAT_10_3P, BATTERY_TYPE_SMILE_BAT_10_3P_DESC },
	{ BATTERY_TYPE_SMILE_BAT_10_1P, BATTERY_TYPE_SMILE_BAT_10_1P_DESC },
	{ BATTERY_TYPE_SMILE_BAT_5_8P, BATTERY_TYPE_SMILE_BAT_5_8P_DESC },
	{ BATTERY_TYPE_SMILE_BAT_5_JP, BATTERY_TYPE_SMILE_BAT_5_JP_DESC },
	{ BATTERY_TYPE_SMILE_BAT_13_7P, BATTERY_TYPE_SMILE_BAT_13_7P_DESC },
};
static const registerLookup batteryTypeLookup = REGISTER_LOOKUP(batteryTypeEntries, false, "Unknown");

static const registerLookupEntry batteryFaultEntries[] =
{
	{ 0, BATTERY_ERROR_BIT_0 },
	{ 1, BATTERY_ERROR_BIT_1 },
	{ 2, BATTERY_ERROR_BIT_2 },
	{ 3, BATTERY_ERROR_BIT_3 },
	{ 4, BATTERY_ERROR_BIT_4 },
	{ 5, BATTERY_ERROR_BIT_5 },
	{ 6, BATTERY_ERROR_BIT_6 },
	{ 7, BATTERY_ERROR_BIT_7 },
	{ 8, BATTERY_ERROR_BIT_8 },
	{ 9, BATTERY_ERROR_BIT_9 },
	{ 10, BATTERY_ERROR_BIT_10 },
	{ 11, BATTERY_ERROR_BIT_11 },
	{ 12, BATTERY_ERROR_BIT_12 },
	{ 13, BATTERY_ERROR_BIT_13 },
	{ 14, BATTERY_ERROR_BIT_14 },
	{ 15, BATTERY_ERROR_BIT_15 },
	{ 16, BATTERY_ERROR_BIT_16 },
	{ 17, BATTERY_ERROR_BIT_17 },
	{ 18, BATTERY_ERROR_BIT_18 },
	{ 19, BATTERY_ERROR_BIT_19 },
	{ 20, BATTERY_ERROR_BIT_20 },
	{ 21, BATTERY_ERROR_BIT_21 },
	{ 22, BATTERY_ERROR_BIT_22 },
	{ 23, BATTERY_ERROR_BIT_23 },
	{ 24, BATTERY_ERROR_BIT_24 },
	{ 25, BATTERY_ERROR_BIT_25 },
	{ 26, BATTERY_ERROR_BIT_26 },
	{ 27, BATTERY_ERROR_BIT_27 },
	{ 28, BATTERY_ERROR_BIT_28 },
	{ 29, BATTERY_ERROR_BIT_29 },
	{ 30, BATTERY_ERROR_BIT_30 },
	{ 31, BATTERY_ERROR_BIT_31 },
};
static const registerLookup batteryFaultLookup = REGISTER_LOOKUP(batteryFaultEntries, true, NULL);

static const registerLookupEntry batteryMosControlEntries[] =
{
	{ BATTERY_MOS_CONTROL_CLOSE, BATTERY_MOS_CONTROL_CLOSE_DESC },
	{ BATTERY_MOS_CONTROL_OPEN, BATTERY_MOS_CONTROL_OPEN_DESC },
};
static const registerLookup batteryMosControlLookup = REGISTER_LOOKUP(batteryMosControlEntries, false, NULL);

static const registerLookupEntry batterySocCalibrationEntries[] =
{
	{ BATTERY_SOC_CALIBRATION_DISABLE, BATTERY_SOC_CALIBRATION_DISABLE_DESC },
	{ BATTERY_SOC_CALIBRATION_ENABLE, BATTERY_SOC_CALIBRATION_ENABLE_DESC },
};
static const registerLookup batterySocCalibrationLookup = REGISTER_LOOKUP(batterySocCalibrationEntries, false, NULL);

static const registerLookupEntry inverterWorkingModeEntries[] =
{
	{ INVERTER_OPERATION_MODE_WAIT_MODE, INVERTER_OPERATION_MODE_WAIT_MODE_DESC },
	{ INVERTER_OPERATION_MODE_ONLINE_MODE, INVERTER_OPERATION_MODE_ONLINE_MODE_DESC },
	{ INVERTER_OPERATION_MODE_UPS_MODE, INVERTER_OPERATION_MODE_UPS_MODE_DESC },
	{ INVERTER_OPERATION_MODE_BYPASS_MODE, INVERTER_OPERATION_MODE_BYPASS_MODE_DESC },
	{ INVERTER_OPERATION_MODE_ERROR_MODE, INVERTER_OPERATION_MODE_ERROR_MODE_DESC },
	{ INVERTER_OPERATION_MODE_DC_MODE, INVERTER_OPERATION_MODE_DC_MODE_DESC },
	{ INVERTER_OPERATION_MODE_SELF_TEST_MODE, INVERTER_OPERATION_MODE_SELF_TEST_MODE_DESC },
	{ INVERTER_OPERATION_MODE_CHECK_MODE, INVERTER_OPERATION_MODE_CHECK_MODE_DESC },
	{ INVERTER_OPERATION_MODE_UPDATE_MASTER_MODE, INVERTER_OPERATION_MODE_UPDATE_MASTER_MODE_DESC },
	{ INVERTER_OPERATION_MODE_UPDATE_SLAVE_MODE, INVERTER_OPERATION_MODE_UPDATE_SLAVE_MODE_DESC },
	{ INVERTER_OPERATION_MODE_UPDATE_ARM_MODE, INVERTER_OPERATION_MODE_UPDATE_ARM_MODE_DESC },
};
static const registerLookup inverterWorkingModeLookup = REGISTER_LOOKUP(inverterWorkingModeEntries, false, NULL);

static const registerLookupEntry systemModeEntries[] =
{
	{ SYSTEM_MODE_AC, SYSTEM_MODE_AC_DESC },
	{ SYSTEM_MODE_DC, SYSTEM_MODE_DC_DESC },
	{ SYSTEM_MODE_HYBRID, SYSTEM_MODE_HYBRID_DESC },
};
static const registerLookup systemModeLookup = REGISTER_LOOKUP(systemModeEntries, false, "Unknown");

static const registerLookupEntry meterCtSelectEntries[] =
{
	{ METER_CT_SELECT_GRID_AND_PV_USE_CT, METER_CT_SELECT_GRID_AND_PV_USE_CT_DESC },
	{ METER_CT_SELECT_GRID_AND_PV_USE_METER, METER_CT_SELECT_GRID_AND_PV_USE_METER_DESC },
	{ METER_CT_SELECT_GRID_USE_CT_PV_USE_METER, METER_CT_SELECT_GRID_USE_CT_PV_USE_METER_DESC },
	{ METER_CT_SELECT_GRID_USE_METER_PV_USE_CT, METER_CT_SELECT_GRID_USE_METER_PV_USE_CT_DESC },
};
static const registerLookup meterCtSelectLookup = REGISTER_LOOKUP(meterCtSelectEntries, false, "Unknown");

static const registerLookupEntry batteryReadyEntries[] =
{
	{ BATTERY_READY_OFF, BATTERY_READY_OFF_DESC },
	{ BATTERY_READY_ON, BATTERY_READY_ON_DESC },
};
static const registerLookup batteryReadyLookup = REGISTER_LOOKUP(batteryReadyEntries, false, "Unknown");

static const registerLookupEntry ipMethodEntries[] =
{
	{ IP_METHOD_DHCP, IP_METHOD_DHCP_DESC },
	{ IP_METHOD_STATIC, IP_METHOD_STATIC_DESC },
};
static const registerLookup ipMethodLookup = REGISTER_LOOKUP(ipMethodEntries, false, "Unknown");

static const registerLookupEntry modbusBaudRateEntries[] =
{
	{ MODBUS_BAUD_RATE_9600, MODBUS_BAUD_RATE_9600_DESC },
	{ MODBUS_BAUD_RATE_115200, MODBUS_BAUD_RATE_115200_DESC },
	{ MODBUS_BAUD_RATE_256000, MODBUS_BAUD_RATE_256000_DESC },
	{ MODBUS_BAUD_RATE_19200, MODBUS_BAUD_RATE_19200_DESC },
};
static const registerLookup modbusBaudRateLookup = REGISTER_LOOKUP(modbusBaudRateEntries, false, "Unknown");

static const registerLookupEntry timePeriodControlFlagEntries[] =
{
	{ TIME_PERIOD_CONTROL_FLAG_DISABLE, TIME_PERIOD_CONTROL_FLAG_DISABLE_DESC },
	{ TIME_PERIOD_CONTROL_FLAG_ENABLE, TIME_PERIOD_CONTROL_FLAG_ENABLE_DESC },
	{ TIME_PERIOD_CONTROL_FLAG_ENABLE_CHARGE, TIME_PERIOD_CONTROL_FLAG_ENABLE_CHARGE_DESC },
	{ TIME_PERIOD_CONTROL_FLAG_ENABLE_DISCHARGE, TIME_PERIOD_CONTROL_FLAG_ENABLE_DISCHARGE_DESC },
};
static const registerLookup timePeriodControlFlagLookup = REGISTER_LOOKUP(timePeriodControlFlagEntries, false, "Unknown");

static const registerLookupEntry dispatchStartEntries[] =
{
	{ DISPATCH_START_START, DISPATCH_START_START_DESC },
	{ DISPATCH_START_STOP, DISPATCH_START_STOP_DESC },
};
static const registerLookup dispatchStartLookup = REGISTER_LOOKUP(dispatchStartEntries, false, "Unknown");

static const registerLookupEntry dispatchModeEntries[] =
{
	{ DISPATCH_MODE_BATTERY_ONLY_CHARGED_VIA_PV, DISPATCH_MODE_BATTERY_ONLY_CHARGED_VIA_PV_DESC },
	{ DISPATCH_MODE_ECO_MODE, DISPATCH_MODE_ECO_MODE_DESC },
	{ DISPATCH_MODE_FCAS_MODE, DISPATCH_MODE_FCAS_MODE_DESC },
	{ DISPATCH_MODE_LOAD_FOLLOWING, DISPATCH_MODE_LOAD_FOLLOWING_DESC },
	{ DISPATCH_MODE_MAXIMISE_CONSUMPTION, DISPATCH_MODE_MAXIMISE_CONSUMPTION_DESC },
	{ DISPATCH_MODE_NORMAL_MODE, DISPATCH_MODE_NORMAL_MODE_DESC },
	{ DISPATCH_MODE_OPTIMISE_CONSUMPTION, DISPATCH_MODE_OPTIMISE_CONSUMPTION_DESC },
	{ DISPATCH_MODE_PV_POWER_SETTING, DISPATCH_MODE_PV_POWER_SETTING_DESC },
	{ DISPATCH_MODE_STATE_OF_CHARGE_CONTROL, DISPATCH_MODE_STATE_OF_CHARGE_CONTROL_DESC },
};
static const registerLookup dispatchModeLookup = REGISTER_LOOKUP(dispatchModeEntries, false, "Unknown");

// Bits 13 to 15 show SYSTEM_ERROR_AL_BIT_14 to _16, as they always have; SYSTEM_ERROR_AL_BIT_13 is never shown.
static const registerLookupEntry systemFaultAlEntries[] =
{
	{ 0, SYSTEM_ERROR_AL_BIT_0 },
	{ 1, SYSTEM_ERROR_AL_BIT_1 },
	{ 2, SYSTEM_ERROR_AL_BIT_2 },
	{ 3, SYSTEM_ERROR_AL_BIT_3 },
	{ 4, SYSTEM_ERROR_AL_BIT_4 },
	{ 5, SYSTEM_ERROR_AL_BIT_5 },
	{ 6, SYSTEM_ERROR_AL_BIT_6 },
	{ 7, SYSTEM_ERROR_AL_BIT_7 },
	{ 8, SYSTEM_ERROR_AL_BIT_8 },
	{ 9, SYSTEM_ERROR_AL_BIT_9 },
	{ 10, SYSTEM_ERROR_AL_BIT_10 },
	{ 11, SYSTEM_ERROR_AL_BIT_11 },
	{ 12, SYSTEM_ERROR_AL_BIT_12 },
	{ 13, SYSTEM_ERROR_AL_BIT_14 },
	{ 14, SYSTEM_ERROR_AL_BIT_15 },
	{ 15, SYSTEM_ERROR_AL_BIT_16 },
};
static const registerLookup systemFaultAlLookup = REGISTER_LOOKUP(systemFaultAlEntries, true, NULL);

static const registerLookupEntry systemFaultAeEntries[] =
{
	{ 0, SYSTEM_ERROR_AE_BIT_0 },
	{ 1, SYSTEM_ERROR_AE_BIT_1 },
	{ 2, SYSTEM_ERROR_AE_BIT_2 },
	{ 3, SYSTEM_ERROR_AE_BIT_3 },
	{ 4, SYSTEM_ERROR_AE_BIT_4 },
	{ 5, SYSTEM_ERROR_AE_BIT_5 },
	{ 6, SYSTEM_ERROR_AE_BIT_6 },
	{ 7, SYSTEM_ERROR_AE_BIT_7 },
	{ 8, SYSTEM_ERROR_AE_BIT_8 },
	{ 9, SYSTEM_ERROR_AE_BIT_9 },
	{ 10, SYSTEM_ERROR_AE_BIT_10 },
	{ 11, SYSTEM_ERROR_AE_BIT_11 },
	{ 12, SYSTEM_ERROR_AE_BIT_12 },
	{ 13, SYSTEM_ERROR_AE_BIT_13 },
	{ 14, SYSTEM_ERROR_AE_BIT_14 },
	{ 15, SYSTEM_ERROR_AE_BIT_15 },
	{ 16, SYSTEM_ERROR_AE_BIT_16 },
	{ 17, SYSTEM_ERROR_AE_BIT_17 },
	{ 18, SYSTEM_ERROR_AE_BIT_18 },
	{ 19, SYSTEM_ERROR_AE_BIT_19 },
	{ 20, SYSTEM_ERROR_AE_BIT_20 },
	{ 21, SYSTEM_ERROR_AE_BIT_21 },
	{ 22, SYSTEM_ERROR_AE_BIT_22 },
	{ 23, SYSTEM_ERROR_AE_BIT_23 },
	{ 24, SYSTEM_ERROR_AE_BIT_24 },
	{ 25, SYSTEM_ERROR_AE_BIT_25 },
	{ 26, SYSTEM_ERROR_AE_BIT_26 },
	{ 27, SYSTEM_ERROR_AE_BIT_27 },
};
static const registerLookup systemFaultAeLookup = REGISTER_LOOKUP(systemFaultAeEntries, true, NULL);

static const registerLookupEntry gridRegulationEntries[] =
{
	{ GRID_REGULATION_AL_0, GRID_REGULATION_AL_0_DESC },
	{ GRID_REGULATION_AL_1, GRID_REGULATION_AL_1_DESC },
	{ GRID_REGULATION_AL_2, GRID_REGULATION_AL_2_DESC },
	{ GRID_REGULATION_AL_3, GRID_REGULATION_AL_3_DESC },
	{ GRID_REGULATION_AL_4, GRID_REGULATION_AL_4_DESC },
	{ GRID_REGULATION_AL_5, GRID_REGULATION_AL_5_DESC },
	{ GRID_REGULATION_AL_6, GRID_REGULATION_AL_6_DESC },
	{ GRID_REGULATION_AL_7, GRID_REGULATION_AL_7_DESC },
	{ GRID_REGULATION_AL_8, GRID_REGULATION_AL_8_DESC },
	{ GRID_REGULATION_AL_9, GRID_REGULATION_AL_9_DESC },
	{ GRID_REGULATION_AL_10, GRID_REGULATION_AL_10_DESC },
	{ GRID_REGULATION_AL_11, GRID_REGULATION_AL_11_DESC },
	{ GRID_REGULATION_AL_12, GRID_REGULATION_AL_12_DESC },
	{ GRID_REGULATION_AL_13, GRID_REGULATION_AL_13_DESC },
	{ GRID_REGULATION_AL_14, GRID_REGULATION_AL_14_DESC },
	{ GRID_REGULATION_AL_15, GRID_REGULATION_AL_15_DESC },
	{ GRID_REGULATION_AL_16, GRID_REGULATION_AL_16_DESC },
	{ GRID_REGULATION_AL_17, GRID_REGULATION_AL_17_DESC },
	{ GRID_REGULATION_AL_18, GRID_REGULATION_AL_18_DESC },
	{ GRID_REGULATION_AL_19, GRID_REGULATION_AL_19_DESC },
	{ GRID_REGULATION_AL_20, GRID_REGULATION_AL_20_DESC },
	{ GRID_REGULATION_AL_21, GRID_REGULATION_AL_21_DESC },
	{ GRID_REGULATION_AL_22, GRID_REGULATION_AL_22_DESC },
	{ GRID_REGULATION_AL_23, GRID_REGULATION_AL_23_DESC },
	{ GRID_REGULATION_AL_24, GRID_REGULATION_AL_24_DESC },
	{ GRID_REGULATION_AL_25, GRID_REGULATION_AL_25_DESC },
	{ GRID_REGULATION_AL_26, GRID_REGULATION_AL_26_DESC },
	{ GRID_REGULATION_AL_27, GRID_REGULATION_AL_27_DESC },
	{ GRID_REGULATION_AL_28, GRID_REGULATION_AL_28_DESC },
	{ GRID_REGULATION_AL_29, GRID_REGULATION_AL_29_DESC },
	{ GRID_REGULATION_AL_30, GRID_REGULATION_AL_30_DESC },
	{ GRID_REGULATION_AL_31, GRID_REGULATION_AL_31_DESC },
	{ GRID_REGULATION_AL_32, GRID_REGULATION_AL_32_DESC },
	{ GRID_REGULATION_AL_33, GRID_REGULATION_AL_33_DESC },
	{ GRID_REGULATION_AL_34, GRID_REGULATION_AL_34_DESC },
	{ GRID_REGULATION_AL_35, GRID_REGULATION_AL_35_DESC },
	{ GRID_REGULATION_AL_36, GRID_REGULATION_AL_36_DESC },
	{ GRID_REGULATION_AL_37, GRID_REGULATION_AL_37_DESC },
};
static const registerLookup gridRegulationLookup = REGISTER_LOOKUP(gridRegulationEntries, false, NULL);

struct registerDescriptor
{
	uint16_t address;
	const char* name;
	uint8_t returnDataType;
	uint8_t registerCount;
	registerFormat format;
	const registerLookup* lookup;
};

// The name is the define's own name, so the two cannot differ
#define HANDLED_REGISTER(address, type, count, format) { address, #address, modbusReturnDataType::type, count, registerFormat::format, NULL }
#define HANDLED_LOOKUP(address, type, count, table) { address, #address, modbusReturnDataType::type, count, registerFormat::lookup, &table }

// Every register readHandledRegister handles. const, so it stays in flash.
static const registerDescriptor handledRegisters[] =
{
	HANDLED_REGISTER(REG_GRID_METER_RW_GRID_METER_CT_ENABLE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_GRID_METER_RW_GRID_METER_CT_RATE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1, unsignedInt, 2, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1, unsignedInt, 2, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_VOLTAGE_OF_A_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_GRID_METER_R_VOLTAGE_OF_B_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_GRID_METER_R_VOLTAGE_OF_C_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_GRID_METER_R_CURRENT_OF_A_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_GRID_METER_R_CURRENT_OF_B_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_GRID_METER_R_CURRENT_OF_C_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_GRID_METER_R_FREQUENCY, unsignedShort, 1, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_ACTIVE_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_ACTIVE_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_ACTIVE_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_REACTIVE_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_REACTIVE_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_REACTIVE_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_REACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_APPARENT_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_APPARENT_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_APPARENT_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_APPARENT_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_GRID_METER_R_POWER_FACTOR_OF_A_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_POWER_FACTOR_OF_B_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_POWER_FACTOR_OF_C_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_GRID_METER_R_TOTAL_POWER_FACTOR, signedShort, 1, hundredths),

	HANDLED_REGISTER(REG_PV_METER_RW_PV_METER_CT_ENABLE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_PV_METER_RW_PV_METER_CT_RATE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1, unsignedInt, 2, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1, unsignedInt, 2, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_VOLTAGE_OF_A_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_PV_METER_R_VOLTAGE_OF_B_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_PV_METER_R_VOLTAGE_OF_C_PHASE, unsignedShort, 1, gridVoltage),
	HANDLED_REGISTER(REG_PV_METER_R_CURRENT_OF_A_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_PV_METER_R_CURRENT_OF_B_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_PV_METER_R_CURRENT_OF_C_PHASE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_PV_METER_R_FREQUENCY, unsignedShort, 1, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_ACTIVE_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_ACTIVE_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_ACTIVE_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_ACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_REACTIVE_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_REACTIVE_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_REACTIVE_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_REACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_APPARENT_POWER_OF_A_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_APPARENT_POWER_OF_B_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_APPARENT_POWER_OF_C_PHASE_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_APPARENT_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_PV_METER_R_POWER_FACTOR_OF_A_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_POWER_FACTOR_OF_B_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_POWER_FACTOR_OF_C_PHASE, signedShort, 1, hundredths),
	HANDLED_REGISTER(REG_PV_METER_R_TOTAL_POWER_FACTOR, signedShort, 1, hundredths),

	HANDLED_REGISTER(REG_BATTERY_HOME_R_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CURRENT, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_SOC, unsignedShort, 1, tenths),
	HANDLED_LOOKUP(REG_BATTERY_HOME_R_STATUS, unsignedShort, 1, batteryStatusLookup),
	HANDLED_LOOKUP(REG_BATTERY_HOME_R_RELAY_STATUS, unsignedShort, 1, batteryRelayStatusLookup),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_PACK_ID_OF_MIN_CELL_VOLTAGE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CELL_ID_OF_MIN_CELL_VOLTAGE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MIN_CELL_VOLTAGE, unsignedShort, 1, cellVoltage),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_PACK_ID_OF_MAX_CELL_VOLTAGE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CELL_ID_OF_MAX_CELL_VOLTAGE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MAX_CELL_VOLTAGE, unsignedShort, 1, cellVoltage),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_PACK_ID_OF_MIN_CELL_TEMPERATURE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CELL_ID_OF_MIN_CELL_TEMPERATURE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MIN_CELL_TEMPERATURE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_PACK_ID_OF_MAX_CELL_TEMPERATURE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CELL_ID_OF_MAX_CELL_TEMPERATURE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MAX_CELL_TEMPERATURE, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MAX_CHARGE_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_MAX_DISCHARGE_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_CHARGE_CUT_OFF_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_DISCHARGE_CUT_OFF_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BMU_SOFTWARE_VERSION, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_LMU_SOFTWARE_VERSION, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_ISO_SOFTWARE_VERSION, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_NUMBER, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_CAPACITY, unsignedShort, 1, tenths),
	HANDLED_LOOKUP(REG_BATTERY_HOME_R_BATTERY_TYPE, unsignedShort, 1, batteryTypeLookup),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_SOH, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_1, unsignedInt, 2, value),
	HANDLED_LOOKUP(REG_BATTERY_HOME_R_BATTERY_FAULT_1, unsignedInt, 2, batteryFaultLookup),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_CHARGE_ENERGY_1, unsignedInt, 2, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_DISCHARGE_ENERGY_1, unsignedInt, 2, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_ENERGY_CHARGE_FROM_GRID_1, unsignedInt, 2, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_POWER, signedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_REMAINING_TIME, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_IMPLEMENTATION_CHARGE_SOC, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_IMPLEMENTATION_DISCHARGE_SOC, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_REMAINING_CHARGE_SOC, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_REMAINING_DISCHARGE_SOC, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_MAX_CHARGE_POWER, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_MAX_DISCHARGE_POWER, unsignedShort, 1, value),
	HANDLED_LOOKUP(REG_BATTERY_HOME_RW_BATTERY_MOS_CONTROL, unsignedShort, 1, batteryMosControlLookup),
	HANDLED_LOOKUP(REG_BATTERY_HOME_R_BATTERY_SOC_CALIBRATION, unsignedShort, 1, batterySocCalibrationLookup),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_SINGLE_CUT_ERROR_CODE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_1_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_2_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_3_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_4_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_5_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_FAULT_6_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_1_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_2_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_3_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_4_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_5_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_BATTERY_HOME_R_BATTERY_WARNING_6_1, unsignedInt, 2, value),

	HANDLED_REGISTER(REG_INVERTER_HOME_R_VOLTAGE_L1, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_VOLTAGE_L2, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_VOLTAGE_L3, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_CURRENT_L1, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_CURRENT_L2, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_CURRENT_L3, signedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_POWER_L1_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_POWER_L2_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_POWER_L3_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_POWER_TOTAL_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L1, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L2, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L3, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_CURRENT_L1, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_CURRENT_L2, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_CURRENT_L3, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_POWER_L1_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_POWER_L2_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_POWER_L3_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_BACKUP_POWER_TOTAL_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_FREQUENCY, unsignedShort, 1, hundredths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV1_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV1_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV1_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV2_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV2_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV2_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV3_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV3_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV3_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV4_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV4_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV4_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV5_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV5_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV5_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV6_VOLTAGE, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV6_CURRENT, unsignedShort, 1, tenths),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_PV6_POWER_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_TEMP, unsignedShort, 1, inverterTemp),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_WARNING_1_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_WARNING_2_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_FAULT_1_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_FAULT_2_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_INVERTER_HOME_R_INVERTER_TOTAL_PV_ENERGY_1, unsignedInt, 2, tenths),
	HANDLED_LOOKUP(REG_INVERTER_HOME_R_WORKING_MODE, unsignedShort, 1, inverterWorkingModeLookup),

	HANDLED_REGISTER(REG_INVERTER_INFO_R_MASTER_SOFTWARE_VERSION_1, character, 5, text),
	HANDLED_REGISTER(REG_INVERTER_INFO_R_SLAVE_SOFTWARE_VERSION_1, character, 5, text),
	HANDLED_REGISTER(REG_INVERTER_INFO_R_SERIAL_NUMBER_1, character, 10, text),

	HANDLED_REGISTER(REG_SYSTEM_INFO_RW_SYSTEM_TIME_YEAR_MONTH, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_RW_SYSTEM_TIME_DAY_HOUR, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_RW_SYSTEM_TIME_MINUTE_SECOND, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_R_EMS_SN_BYTE_1_2, character, 8, textMoved),
	HANDLED_REGISTER(REG_SYSTEM_INFO_R_EMS_VERSION_HIGH, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_R_EMS_VERSION_MIDDLE, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_R_EMS_VERSION_LOW, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_INFO_R_PROTOCOL_VERSION, unsignedShort, 1, value),

	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_MAX_FEED_INTO_GRID_PERCENT, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_PV_CAPACITY_STORAGE_1, unsignedInt, 2, value),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_PV_CAPACITY_OF_GRID_INVERTER_1, unsignedInt, 2, value),
	HANDLED_LOOKUP(REG_SYSTEM_CONFIG_RW_SYSTEM_MODE, unsignedShort, 1, systemModeLookup),
	HANDLED_LOOKUP(REG_SYSTEM_CONFIG_RW_METER_CT_SELECT, unsignedShort, 1, meterCtSelectLookup),
	HANDLED_LOOKUP(REG_SYSTEM_CONFIG_RW_BATTERY_READY, unsignedShort, 1, batteryReadyLookup),
	HANDLED_LOOKUP(REG_SYSTEM_CONFIG_RW_IP_METHOD, unsignedShort, 1, ipMethodLookup),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_LOCAL_IP_1, unsignedInt, 2, ipAddress),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_SUBNET_MASK_1, unsignedInt, 2, ipAddress),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_GATEWAY_1, unsignedInt, 2, ipAddress),
	HANDLED_REGISTER(REG_SYSTEM_CONFIG_RW_MODBUS_ADDRESS, unsignedShort, 1, value),
	HANDLED_LOOKUP(REG_SYSTEM_CONFIG_RW_MODBUS_BAUD_RATE, unsignedShort, 1, modbusBaudRateLookup),

	HANDLED_LOOKUP(REG_TIMING_RW_TIME_PERIOD_CONTROL_FLAG, unsignedShort, 1, timePeriodControlFlagLookup),
	HANDLED_REGISTER(REG_TIMING_RW_UPS_RESERVE_SOC, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_DISCHARGE_START_TIME_1, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_DISCHARGE_STOP_TIME_1, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_DISCHARGE_START_TIME_2, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_DISCHARGE_STOP_TIME_2, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_CHARGE_CUT_SOC, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_CHARGE_START_TIME_1, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_CHARGE_STOP_TIME_1, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_CHARGE_START_TIME_2, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_TIMING_RW_TIME_CHARGE_STOP_TIME_2, unsignedShort, 1, value),

	HANDLED_LOOKUP(REG_DISPATCH_RW_DISPATCH_START, unsignedShort, 1, dispatchStartLookup),
	HANDLED_REGISTER(REG_DISPATCH_RW_ACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_REGISTER(REG_DISPATCH_RW_REACTIVE_POWER_1, signedInt, 2, value),
	HANDLED_LOOKUP(REG_DISPATCH_RW_DISPATCH_MODE, unsignedShort, 1, dispatchModeLookup),
	HANDLED_REGISTER(REG_DISPATCH_RW_DISPATCH_SOC, unsignedShort, 1, dispatchSoc),
	HANDLED_REGISTER(REG_DISPATCH_RW_DISPATCH_TIME_1, unsignedInt, 2, value),

	HANDLED_REGISTER(REG_AUXILIARY_R_EMS_DI0, unsignedShort, 1, value),
	HANDLED_REGISTER(REG_AUXILIARY_R_EMS_DI1, unsignedShort, 1, value),

	HANDLED_REGISTER(REG_SYSTEM_OP_R_PV_INVERTER_ENERGY_1, unsignedInt, 2, totalEnergy),
	HANDLED_REGISTER(REG_SYSTEM_OP_R_SYSTEM_TOTAL_PV_ENERGY_1, unsignedInt, 2, totalEnergy),
	HANDLED_REGISTER(REG_SYSTEM_OP_R_SYSTEM_FAULT_1, unsignedInt, 2, systemFault),

	HANDLED_LOOKUP(REG_SAFETY_TEST_RW_GRID_REGULATION, unsignedShort, 1, gridRegulationLookup),

	// Not inverter registers: RegisterHandler reads and works these out itself
	HANDLED_REGISTER(REG_CUSTOM_SYSTEM_DATE_TIME, character, 3, dateTime),
	HANDLED_REGISTER(REG_CUSTOM_LOAD, signedInt, 2, value),
	HANDLED_REGISTER(REG_CUSTOM_GRID_CURRENT_A_PHASE, signedShort, 1, value),
	HANDLED_REGISTER(REG_CUSTOM_TOTAL_SOLAR_POWER, signedInt, 2, value),
};

#undef HANDLED_REGISTER
#undef HANDLED_LOOKUP
#undef REGISTER_LOOKUP

inline const registerDescriptor* findRegister(uint16_t address)
{
	for (const registerDescriptor& reg : handledRegisters)
	{
		if (reg.address == address)
		{
			return &reg;
		}
	}
	return NULL;
}

// What the request needs before it is sent: data type, MQTT name and number of registers
inline void describeRegister(const registerDescriptor* reg, modbusRequestAndResponse* rs)
{
	rs->returnDataType = (modbusReturnDataType)reg->returnDataType;
	strcpy(rs->mqttName, reg->name);
	rs->registerCount = reg->registerCount;
	// Quoted in the JSON, as a description is text
	if (reg->format == registerFormat::lookup || reg->format == registerFormat::systemFault || reg->format == registerFormat::ipAddress)
	{
		rs->hasLookup = true;
	}
}

// Sets the typed value from the response bytes, according to the rules of the Modbus documentation
inline void decodeRegisterValue(modbusRequestAndResponse* rs)
{
	switch (rs->returnDataType)
	{
	case modbusReturnDataType::character:
	{
		memcpy(rs->characterValue, rs->data, rs->dataSize);
		strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_CHARACTER_DESC);
		break;
	}
	case modbusReturnDataType::unsignedInt:
	{
		rs->unsignedIntValue = (uint32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]);
		strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_UNSIGNED_INT_DESC);
		break;
	}
	case modbusReturnDataType::unsignedShort:
	{
		rs->unsignedShortValue = (uint16_t)(rs->data[0] << 8 | rs->data[1]);
		strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_UNSIGNED_SHORT_DESC);
		break;
	}
	case modbusReturnDataType::signedInt:
	{
		rs->signedIntValue = (int32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]);
		strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_SIGNED_INT_DESC);
		break;
	}
	case modbusReturnDataType::signedShort:
	{
		rs->signedShortValue = (int16_t)(rs->data[0] << 8 | rs->data[1]);
		strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_SIGNED_SHORT_DESC);
		break;
	}
	default:
		break;
	}
}

inline double registerScale(registerFormat format)
{
	switch (format)
	{
	case registerFormat::tenths:
		return 0.1;
	case registerFormat::hundredths:
		return 0.01;
	case registerFormat::gridVoltage:
		return GRID_VOLTAGE_MULTIPLIER;
	case registerFormat::cellVoltage:
		return CELL_VOLTAGE_MULTIPLIER;
	case registerFormat::inverterTemp:
		return INVERTER_TEMP_MULTIPLIER;
	case registerFormat::totalEnergy:
		return TOTAL_ENERGY_MUTLIPLIER;
	default:
		return 0;
	}
}

inline const char* registerLookupDesc(const registerLookup* lookup, uint32_t value)
{
	for (uint8_t i = 0; i < lookup->count; i++)
	{
		const registerLookupEntry* entry = &lookup->entries[i];
		if (lookup->bits ? (value >> entry->value & 1) != 0 : value == entry->value)
		{
			return entry->desc;
		}
	}
	return lookup->unknown;
}

// Writes dataValueFormatted from the value decodeRegisterValue set. serialNumberPrefix picks the system fault descriptions.
inline void formatRegisterValue(const registerDescriptor* reg, modbusRequestAndResponse* rs, const char* serialNumberPrefix)
{
	const registerLookup* lookup = reg->lookup;
	const char* desc;
	double value = 0;

	switch (reg->format)
	{
	case registerFormat::value:
	{
		if (rs->returnDataType == modbusReturnDataType::unsignedShort)
		{
			sprintf(rs->dataValueFormatted, "%u", rs->unsignedShortValue);
		}
		else if (rs->returnDataType == modbusReturnDataType::unsignedInt)
		{
			sprintf(rs->dataValueFormatted, "%u", (unsigned int)rs->unsignedIntValue);
		}
		else if (rs->returnDataType == modbusReturnDataType::signedShort)
		{
			sprintf(rs->dataValueFormatted, "%d", rs->signedShortValue);
		}
		else if (rs->returnDataType == modbusReturnDataType::signedInt)
		{
			sprintf(rs->dataValueFormatted, "%d", (int)rs->signedIntValue);
		}
		break;
	}
	case registerFormat::tenths:
	case registerFormat::hundredths:
	case registerFormat::gridVoltage:
	case registerFormat::cellVoltage:
	case registerFormat::inverterTemp:
	case registerFormat::totalEnergy:
	{
		if (rs->returnDataType == modbusReturnDataType::unsignedShort)
		{
			value = rs->unsignedShortValue;
		}
		else if (rs->returnDataType == modbusReturnDataType::unsignedInt)
		{
			value = rs->unsignedIntValue;
		}
		else if (rs->returnDataType == modbusReturnDataType::signedShort)
		{
			value = rs->signedShortValue;
		}
		else if (rs->returnDataType == modbusReturnDataType::signedInt)
		{
			value = rs->signedIntValue;
		}
		sprintf(rs->dataValueFormatted, "%0.02f", value * registerScale(reg->format));
		break;
	}
	case registerFormat::dispatchSoc:
	{
		// Integer maths: %u given a float prints 0.
		sprintf(rs->dataValueFormatted, "%u", (rs->unsignedShortValue * 4 + 5) / 10);
		break;
	}
	case registerFormat::ipAddress:
	{
		sprintf(rs->dataValueFormatted, "%u.%u.%u.%u", rs->data[0], rs->data[1], rs->data[2], rs->data[3]);
		break;
	}
	case registerFormat::text:
	case registerFormat::textMoved:
	{
		strcpy(rs->dataValueFormatted, rs->characterValue);
		if (reg->format == registerFormat::textMoved)
		{
			rs->characterValue[0] = 0;
		}
		break;
	}
	case registerFormat::systemFault:
	{
		if (serialNumberPrefix[0] == 'A' && serialNumberPrefix[1] == 'L')
		{
			lookup = &systemFaultAlLookup;
		}
		else if (serialNumberPrefix[0] == 'A' && serialNumberPrefix[1] == 'E')
		{
			lookup = &systemFaultAeLookup;
		}
		else
		{
			strcpy(rs->dataValueFormatted, "Unknown");
			break;
		}
		[[fallthrough]];
	}
	case registerFormat::lookup:
	{
		desc = registerLookupDesc(lookup, lookup->bits ? rs->unsignedIntValue : rs->unsignedShortValue);
		if (desc != NULL)
		{
			strcpy(rs->dataValueFormatted, desc);
		}
		break;
	}
	case registerFormat::dateTime:
		break;
	}
}

#endif
