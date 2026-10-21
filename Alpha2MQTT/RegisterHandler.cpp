/*
Name:		RegisterHandler.h
Created:	24/Aug/2022
Author:		Daniel Young

This file is part of Alpha2MQTT (A2M) which is released under GNU GENERAL PUBLIC LICENSE.
See file LICENSE or go to https://choosealicense.com/licenses/gpl-3.0/ for full license details.

Notes

Handles AlphaESS specific functions as of v1.23 AlphaESS Modbus documentation
Namely:
	0x03 READ DATA REGISTER
	0x06 WRITE SINGLE REGISTER
	0x0A WRITE DATA REGISTER

I presumed when I started that WRITE SINGLE REGISTER was for single two byte registers, and was
put in to provide a simpler way to write the bulk of the smaller registers without needing to worry
about sending over data lengths, etc.  On my Smile B3 at least, WRITE SINGLE REGISTER does nothing,
it does not attempt any data changes and it does not yield any response (nor slave error) from the
system.  I found that WRITE DATA REGISTER will write any appropriate single (2 byte) or double (4 byte)
register, and we just leverage 'Number of bytes' to guide it accordingly.
*/
#include "RegisterHandler.h"
#include "Dispatch.h"
#include "PvMeter.h"
#include "Registers.h"

/*
Default Constructor

Initialisations and what not
*/
RegisterHandler::RegisterHandler()
{

}

/*
Constructor which allows setting the RS485 class level variable

Initialisations and what not
*/
RegisterHandler::RegisterHandler(RS485Handler* modBus)
{
	_modBus = modBus;
}

/*
Default Destructor

Disconnections, clean-up and what not
*/
RegisterHandler::~RegisterHandler()
{
	_modBus = NULL;
}

/*
setSerialNumberPrefix

To allow appropriate error codes to be spat out for AL or AE based serial numbers
*/
void RegisterHandler::setSerialNumberPrefix(uint8_t char1, uint8_t char2)
{
	_serialNumberPrefix[0] = char1;
	_serialNumberPrefix[1] = char2;
}

/*
setModbus

Set the class level variable for RS485
*/
void RegisterHandler::setModbus(RS485Handler* modBus)
{
	_modBus = modBus;
}



/*
readHandledRegister

This will perform validation, sense checking and cleansed / appropriately cast results for a whole
raft of registers (300+.)  If the request is for a register which isn't handled it will throw back an appropriate error
*/
modbusRequestAndResponseStatusValues RegisterHandler::readHandledRegister(uint16_t registerAddress, modbusRequestAndResponse* rs)
{
	// To account for custom registers just before sending
	uint16_t registerAddressToSend;
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;

	// Slave Address
	// Function Code
	// Starting Address High
	// Starting Address Low, 
	// Number Of Registers High Byte
	// Number Of Registers Low Byte
	// CRC Low Byte
	// CRC High Byte

	// For custom registers
	int16_t batteryPower = 0;
	int32_t pvPower = 0;
	int32_t gridPower = 0;
	uint16_t gridVoltage = 0;

	// Determine number of registers/data type/mqtt name based on register passed in
	const registerDescriptor* reg = findRegister(registerAddress);
	if (reg == NULL)
	{
		// Not a valid register we have written code to handle, do something here to prevent the send
		result = modbusRequestAndResponseStatusValues::notHandledRegister;
		strcpy(rs->statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_NOT_HANDLED_REGISTER_MQTT_DESC);
	}
	else
	{
		describeRegister(reg, rs);
	}


	// If a custom register address we've made up to do some of our own work, swap it around here.
	if (result == modbusRequestAndResponseStatusValues::preProcessing)
	{
		if (registerAddress == REG_CUSTOM_LOAD)
		{
			strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_SIGNED_INT_DESC);

			/*
			Load is not exposed by the inverter, so we need a custom routine to pull the three registers relevantand do the calculations on the chip.
			OK so theory is
			Cosumption is PV generating
			Plus the grid, providing that the grid IS pulling
			Plus the battery, providing that it is discharging AND grid not pulling(i.e. not forcibly discharging to grid)
			*/


			result = readTotalSolarPower(rs, &pvPower);
			if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
			{
				// Generate a frame without CRC (ending 0, 0), sendModbus will do the rest
				uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1 >> 8, REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1 & 0xff, 0, 2, 0, 0 };
				// And send to the device, it's all synchronos so by the time we get a response we will know if success or failure
				result = _modBus->sendModbus(frame, sizeof(frame), rs);
				gridPower = (int32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]);
				if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
				{
					// Generate a frame without CRC (ending 0, 0), sendModbus will do the rest
					uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, REG_BATTERY_HOME_R_BATTERY_POWER >> 8, REG_BATTERY_HOME_R_BATTERY_POWER & 0xff, 0, 1, 0, 0 };
					// And send to the device, it's all synchronos so by the time we get a response we will know if success or failure
					result = _modBus->sendModbus(frame, sizeof(frame), rs);
					if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
					{
						batteryPower = (int16_t)(rs->data[0] << 8 | rs->data[1]);
						rs->signedIntValue =
							pvPower
							-
							// Minus feeding, if feeding
							(gridPower < 0 ? (int32_t)abs(gridPower) : (int32_t)0)
							+
							// Plus purchase, if purchasing
							(gridPower > 0 ? gridPower : (int32_t)0)
							-
							// Minus battery, if charging
							(batteryPower < 0 ? (int32_t)abs(batteryPower) : (int32_t)0)
							+
							// Plus battery, if discharging
							((int32_t)batteryPower > 0 ? (int32_t)batteryPower : (int32_t)0);

						rs->dataSize = 4;
						rs->data[0] = rs->signedIntValue >> 24;
						rs->data[1] = rs->signedIntValue >> 16;
						rs->data[2] = rs->signedIntValue >> 8;
						rs->data[3] = rs->signedIntValue & 0xff;
					}
				}
			}

		}
		else if(registerAddress == REG_CUSTOM_GRID_CURRENT_A_PHASE)
		{
			strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_SIGNED_SHORT_DESC);

			/*
			Grid Current (Phase A) is not exposed by my inverter, so I am using a custom routine to pull the two registers relevant and do the calculations on the chip.
			OK so theory is
			I = P/V
			Ensure V > 0 to avoid division by zero
			*/

			uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, REG_GRID_METER_R_ACTIVE_POWER_OF_A_PHASE_1 >> 8, REG_GRID_METER_R_ACTIVE_POWER_OF_A_PHASE_1 & 0xff, 0, 2, 0, 0 };
			result = _modBus->sendModbus(frame, sizeof(frame), rs);
			if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
			{
				gridPower = (int32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]);

				uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, REG_GRID_METER_R_VOLTAGE_OF_A_PHASE >> 8, REG_GRID_METER_R_VOLTAGE_OF_A_PHASE & 0xff, 0, 1, 0, 0 };
				result = _modBus->sendModbus(frame, sizeof(frame), rs);
				if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
				{
					gridVoltage = ((uint16_t)(rs->data[0] << 8 | rs->data[1])) * GRID_VOLTAGE_MULTIPLIER;

					rs->signedShortValue = (gridVoltage == 0 ? 0 : gridPower / gridVoltage);

					rs->dataSize = 2;
					rs->data[0] = rs->signedShortValue >> 8;
					rs->data[1] = rs->signedShortValue & 0xff;
				}
			}

		}
		else if (registerAddress == REG_CUSTOM_TOTAL_SOLAR_POWER)
		{
			strcpy(rs->returnDataTypeDesc, MODBUS_RETURN_DATA_TYPE_SIGNED_INT_DESC);

			/*
			PV if AC coupled is via  PV CT and results are stored in
			REG_PV_METER_R_TOTAL_ACTIVE_POWER_1

			PV if hybrid is via the individual string readings from within the Alpha, namely
			REG_INVERTER_HOME_R_PV1_POWER_1
			REG_INVERTER_HOME_R_PV2_POWER_1
			REG_INVERTER_HOME_R_PV3_POWER_1
			REG_INVERTER_HOME_R_PV4_POWER_1
			REG_INVERTER_HOME_R_PV5_POWER_1
			REG_INVERTER_HOME_R_PV6_POWER_1

			So essentially to get a solar reading which is safe across all types, we will just add all these up and present as a custom reg
			*/


			result = readTotalSolarPower(rs, &pvPower);
			if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
			{
				rs->signedIntValue = pvPower;

				rs->dataSize = 4;
				rs->data[0] = rs->signedIntValue >> 24;
				rs->data[1] = rs->signedIntValue >> 16;
				rs->data[2] = rs->signedIntValue >> 8;
				rs->data[3] = rs->signedIntValue & 0xff;
			}

		}
		else
		{
			// Normal route, however with a quick check for registers which may need switching
			switch (registerAddress)
			{
			case REG_CUSTOM_SYSTEM_DATE_TIME:
			{
				registerAddressToSend = REG_SYSTEM_INFO_RW_SYSTEM_TIME_YEAR_MONTH;
				break;
			}
			default:
			{
				registerAddressToSend = registerAddress;
				break;
			}
			}

			if (registerAddressToSend == REG_PV_METER_R_TOTAL_ACTIVE_POWER_1)
			{
				result = readPvMeterPower(rs, &pvPower);
			}
			else if (result == modbusRequestAndResponseStatusValues::preProcessing)
			{
				// Generate a frame without CRC (ending 0, 0), sendModbus will do the rest
				uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, registerAddressToSend >> 8, registerAddressToSend & 0xff, 0, rs->registerCount, 0, 0 };

				// And send to the device, it's all synchronos so by the time we get a response we will know if success or failure
				result = _modBus->sendModbus(frame, sizeof(frame), rs);
			}
		}
	}

	if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
	{
		// So, it's a success, so we can process according to the rules of the Modbus documentation
		// What we are aiming for as an understanding of a correct value, correctly typed, identifiable by any calling function
		decodeRegisterValue(rs);
		if (reg->format == registerFormat::dateTime)
		{
			// Custom date/time returned as text based on the three registers.
			createFormattedDateTime(rs->dataValueFormatted, rs->data[0], rs->data[1], rs->data[2], rs->data[3], rs->data[4], rs->data[5]);

			// Clear the characterValue as we are customising this one
			rs->characterValue[0] = 0;
		}
		else
		{
			formatRegisterValue(reg, rs, _serialNumberPrefix);
		}
	}
	return result;
}






/*
createFormattedDateTime

Takes a series of uint8_t variables and converts into UK formatted date/time value (dd/MMM/yyyy HH:mm:ss)
*/
void RegisterHandler::createFormattedDateTime(char *target, uint8_t year, uint8_t month, uint8_t day, uint8_t hour, uint8_t minute, uint8_t second)
{
	char months[12][4] = { "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" };
	sprintf(target, "%02d/%0s/20%d %02d:%02d:%02d", day, months[month - 1], year, hour, minute, second);
}









/*
readPvMeterPower

Reads REG_PV_METER_R_TOTAL_ACTIVE_POWER_1 for the register itself and for the custom registers built on it.
A 0 that pvMeterZeroUnconfirmed holds back fails as unconfirmedZero, so a schedule leaves it out of that payload.
*/
modbusRequestAndResponseStatusValues RegisterHandler::readPvMeterPower(modbusRequestAndResponse* rs, int32_t* pvPower)
{
	bool unconfirmed;
	uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, REG_PV_METER_R_TOTAL_ACTIVE_POWER_1 >> 8, REG_PV_METER_R_TOTAL_ACTIVE_POWER_1 & 0xff, 0, 2, 0, 0 };
	modbusRequestAndResponseStatusValues result = _modBus->sendModbus(frame, sizeof(frame), rs);

	if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
	{
		*pvPower = (int32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]);
		unconfirmed = pvMeterZeroUnconfirmed(_lastPvMeterPower, *pvPower);
		_lastPvMeterPower = *pvPower;
		if (unconfirmed)
		{
			result = modbusRequestAndResponseStatusValues::unconfirmedZero;
			strcpy(rs->statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_UNCONFIRMED_ZERO_MQTT_DESC);
		}
	}
	return result;
}


/*
readTotalSolarPower

The PV meter plus the six PV strings, so one figure is right for an AC coupled and a hybrid inverter alike.
Reads them in that order and stops at the first that fails.
*/
modbusRequestAndResponseStatusValues RegisterHandler::readTotalSolarPower(modbusRequestAndResponse* rs, int32_t* pvPower)
{
	static const uint16_t strings[] = { REG_INVERTER_HOME_R_PV1_POWER_1, REG_INVERTER_HOME_R_PV2_POWER_1, REG_INVERTER_HOME_R_PV3_POWER_1,
		REG_INVERTER_HOME_R_PV4_POWER_1, REG_INVERTER_HOME_R_PV5_POWER_1, REG_INVERTER_HOME_R_PV6_POWER_1 };
	modbusRequestAndResponseStatusValues result = readPvMeterPower(rs, pvPower);

	for (unsigned int i = 0; i < sizeof(strings) / sizeof(strings[0]) && result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess; i++)
	{
		uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, (uint8_t)(strings[i] >> 8), (uint8_t)(strings[i] & 0xff), 0, 2, 0, 0 };
		result = _modBus->sendModbus(frame, sizeof(frame), rs);
		if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
		{
			*pvPower = *pvPower + ((int32_t)(rs->data[0] << 24 | rs->data[1] << 16 | rs->data[2] << 8 | rs->data[3]));
		}
	}
	return result;
}



/*
readRawRegister

Sends a basic Read Data Register request to the Alpha system and returns back for the calling function to do onward procesing
*/
modbusRequestAndResponseStatusValues RegisterHandler::readRawRegister(uint16_t registerAddress, modbusRequestAndResponse* rs)
{
	// Generate a frame with CRC placeholders of 0, 0 at the end
	uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_READDATAREGISTER, registerAddress >> 8, registerAddress & 0xff, 0, rs->registerCount, 0, 0 };

	// And send to the device, it's all synchronos so by the time we get a response we will know if success or failure
	return _modBus->sendModbus(frame, sizeof(frame), rs);
}









/*
writeSingleRegister

Sends a basic Write Single Register request to the Alpha system and returns back for the calling function to do onward procesing
*/
modbusRequestAndResponseStatusValues RegisterHandler::writeRawSingleRegister(uint16_t registerAddress, uint16_t value, modbusRequestAndResponse* rs)
{
	// Generate a frame with CRC placeholders of 0, 0 at the end
	uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_WRITESINGLEREGISTER, registerAddress >> 8, registerAddress & 0xff, value >> 8, value & 0xff, 0, 0 };

	// And send to the device, it's all synchronos so by the time we get a response we will know if success or failure
	return _modBus->sendModbus(frame, sizeof(frame), rs);
}






/*
writeDataRegister

Sends a basic Write Data Register request to the Alpha system and returns back for the calling function to do onward procesing
This expects number of registers (1 for 2 byte registers, 2 for 4 byte registers) in the structure before called
*/
modbusRequestAndResponseStatusValues RegisterHandler::writeRawDataRegister(uint16_t registerAddress, uint32_t value, modbusRequestAndResponse* rs)
{
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;

	if (rs->registerCount == 1)
	{
		uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_WRITEDATAREGISTER, registerAddress >> 8, registerAddress & 0xff, 0, rs->registerCount, 2, value >> 8, value & 0xff, 0, 0 };
		result = _modBus->sendModbus(frame, sizeof(frame), rs);
	}
	else if (rs->registerCount == 2)
	{
		uint8_t	frame[] = { ALPHA_SLAVE_ID, MODBUS_FN_WRITEDATAREGISTER, registerAddress >> 8, registerAddress & 0xff, 0, rs->registerCount, 4, value >> 24, value >> 16, value >> 8, value & 0xff, 0, 0 };
		result = _modBus->sendModbus(frame, sizeof(frame), rs);
	}
	// And now it has been sent to the device, the response is essentially synchronos so by the time we get a response we will know if success or failure

	return result;
}

/*
writeDispatchBlock

The whole dispatch block in one Write Data Register, see dispatchBlockFrame. The inverter answers a
nine register write like any other, so the caller sees the same statuses as writeRawDataRegister.
*/
modbusRequestAndResponseStatusValues RegisterHandler::writeDispatchBlock(uint16_t start, uint32_t powerRegister, uint16_t mode, uint16_t socRegister, uint32_t durationS, modbusRequestAndResponse* rs)
{
	uint8_t frame[DISPATCH_BLOCK_FRAME_SIZE];

	rs->registerCount = 9;
	dispatchBlockFrame(frame, ALPHA_SLAVE_ID, start, powerRegister, mode, socRegister, durationS);
	return _modBus->sendModbus(frame, sizeof(frame), rs);
}
