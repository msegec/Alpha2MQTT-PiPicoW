/*
Name:		Alpha2MQTT.ino
Created:	24/Aug/2022
Author:		Daniel Young

This file is part of Alpha2MQTT (A2M) which is released under GNU GENERAL PUBLIC LICENSE.
See file LICENSE or go to https://choosealicense.com/licenses/gpl-3.0/ for full license details.

Notes

First, go and customise options at the top of Definitions.h!
*/

// Supporting files
#include "RegisterHandler.h"
#include "RS485Handler.h"
#include "Definitions.h"
#include "Dispatch.h"
#include "ModbusFrame.h"
#include "Timer.h"
#include "IdleHold.h"
#include <Arduino.h>
#if defined MP_ESP8266
#include <ESP8266WiFi.h>
#elif defined MP_ESP32 || defined MP_PICOW
#include <WiFi.h>
#endif
#if defined MP_PICOW
#include <hardware/adc.h>
#include <pico/cyw43_arch.h>
#include <ArduinoOTA.h>
#ifndef OTA_PASSWORD
#error Set OTA_PASSWORD in Secrets.h, see Secrets.example.h
#endif
static_assert(sizeof(OTA_PASSWORD) > 1, "OTA_PASSWORD must not be empty, or anyone on the network can flash the Pico");
#endif
#include <PubSubClient.h>
#include <SPI.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// Device parameters
char _version[6] = "v1.29";

// WiFi parameters
WiFiClient _wifi;

// MQTT parameters
PubSubClient _mqtt(_wifi);

// Buffer Size (and therefore payload size calc)
int _bufferSize;
int _maxPayloadSize;


// I want to declare this once at a modular level, keep the heap somewhere in check.
//char _mqttPayload[MAX_MQTT_PAYLOAD_SIZE] = "";
char* _mqttPayload;

// Incoming commands are handled between the reads of a schedule, while _mqttPayload holds
// that schedule's half built payload, so they build their responses here instead.
char* _commandPayload;

// The fast state can also be sent between the reads of a slow schedule, so it gets its own too.
char* _fastPayload;

// The last dispatch block written in full, so a power change can go out as one write
DispatchBlock _activeDispatch = {};

// Set when the inverter answers the one frame dispatch write with an exception but takes the five single writes.
// Later commands go straight to the five single writes instead of paying for a refused frame first. Only a reboot
// clears it.
bool _blockFrameRefused = false;

// Whether Home Assistant is gone, and the idle hold or its release last written. See holdIdleWhileHaGone.
IdleHold _idleHold = {};

// OLED variables
struct displayReading
{
	bool ok;
	int32_t value;
	char displayMessage[OLED_CHARACTER_WIDTH];
};
displayReading _shownDispatchStart = {};
displayReading _shownBatteryPower = {};
displayReading _shownSoc = {};
char _oledOperatingIndicator = '*';
char _oledLine2[OLED_CHARACTER_WIDTH] = "";
char _oledLine3[OLED_CHARACTER_WIDTH] = "";
char _oledLine4[OLED_CHARACTER_WIDTH] = "";


// RS485 and AlphaESS functionality are packed up into classes
// to keep separate from the main program logic.
RS485Handler* _modBus;
RegisterHandler* _registerHandler;

// Fixed char array for messages to the serial port
char _debugOutput[100];



// Schedules
/*
The registers a controller needs, read back to back every STATUS_INTERVAL_FAST so each sample is coherent.
Keep this list short, every register costs one RS485 transaction per cycle.
*/
static const uint16_t _mqttFastStatusRegisters[] =
{
	REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1,		// Total Grid Power (+/-)
	REG_PV_METER_R_TOTAL_ACTIVE_POWER_1,		// Total PV Power (+/-)
	REG_BATTERY_HOME_R_BATTERY_POWER,			// Battery Power
	REG_BATTERY_HOME_R_SOC,						// State Of Charge
	REG_DISPATCH_RW_DISPATCH_START				// Dispatch running or not
};

/*
Add any number of handled registers in this list and they will be
read and returned every 10 seconds.
*/
static const uint16_t _mqttTenSecondStatusRegisters[] =
{
	REG_BATTERY_HOME_R_SOC,						// State Of Charge
	REG_BATTERY_HOME_R_BATTERY_POWER,			// Battery Power
	REG_BATTERY_HOME_R_VOLTAGE,					// Battery Voltage
	REG_BATTERY_HOME_R_CURRENT,					// Battery Current
	REG_BATTERY_HOME_R_MAX_CELL_TEMPERATURE,	// Highest Battery Temp
	REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1,		// Total Grid Power (+/-)
	REG_CUSTOM_GRID_CURRENT_A_PHASE,			// Grid Current (Phase A)
	REG_PV_METER_R_TOTAL_ACTIVE_POWER_1,		// Total PV Power (+/-)

	REG_INVERTER_HOME_R_CURRENT_L1,				// Inverter Current (L1) (Phase A)
	REG_INVERTER_HOME_R_POWER_L1_1,				// Inverter Power (L1) (Phase A)
	REG_INVERTER_HOME_R_INVERTER_TEMP,			// Inverter Temp
	REG_CUSTOM_LOAD,							// Consumption

	REG_DISPATCH_RW_DISPATCH_START,
	REG_DISPATCH_RW_DISPATCH_MODE,
	REG_DISPATCH_RW_ACTIVE_POWER_1,
	REG_DISPATCH_RW_DISPATCH_SOC,
	REG_DISPATCH_RW_DISPATCH_TIME_1,

	// PV1 to PV6 power (REG_INVERTER_HOME_R_PV1_POWER_1 onwards) exist on hybrid inverters only.
	// The Smile B3 is AC coupled, so they are left out to save six bus reads per cycle.

	REG_CUSTOM_TOTAL_SOLAR_POWER

	/*
	* 
	* Alpha don't appear to expose Solar Current, Battery Cycles
	* They don't expose Load either via a register, it is a calculation which Alpha ESS provided the logic for.
	* Alpha don't also expose today's generation / exported / purchased / consumed, so if using Home Assistant leverage
	* https://www.home-assistant.io/integrations/integration/#energy
	* Will convert regular submitted power readings (in W or kW) into kWh for use in Utility Meters
	* https://www.home-assistant.io/integrations/utility_meter/
	* Which you can configure numerous of, and set to restart daily, weekly, monthly, etc.
	*/
};

/*
Add any number of handled registers in this list and they will be
read and returned every minute.
*/
static const uint16_t _mqttOneMinuteStatusRegisters[] =
{
	REG_GRID_METER_R_VOLTAGE_OF_A_PHASE,
	REG_PV_METER_R_VOLTAGE_OF_A_PHASE,
	REG_INVERTER_HOME_R_VOLTAGE_L1,

	// Energy totals, every minute so the Home Assistant Energy dashboard stays current
	REG_BATTERY_HOME_R_BATTERY_CHARGE_ENERGY_1,
	REG_BATTERY_HOME_R_BATTERY_DISCHARGE_ENERGY_1,
	REG_BATTERY_HOME_R_BATTERY_ENERGY_CHARGE_FROM_GRID_1,
	REG_SYSTEM_OP_R_SYSTEM_TOTAL_PV_ENERGY_1,
	REG_GRID_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1,
	REG_GRID_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1,
	REG_PV_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1,
	REG_PV_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1
};

/*
Add any number of handled registers in this list and they will be
read and returned every five minutes.
*/
static const uint16_t _mqttFiveMinuteStatusRegisters[] =
{
};

/*
Add any number of handled registers in this list and they will be
read and returned every hour.
*/
static const uint16_t _mqttOneHourStatusRegisters[] =
{
	REG_GRID_METER_R_FREQUENCY,
	REG_PV_METER_R_FREQUENCY,
	REG_INVERTER_HOME_R_FREQUENCY,
	REG_SYSTEM_OP_R_SYSTEM_FAULT_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_1
};

/*
Add any number of handled registers in this list and they will be
read and returned every day.
*/
static const uint16_t _mqttOneDayStatusRegisters[] =
{
};


/*
Every handled register
*/

static const uint16_t _mqttAllHandledRegisters[] =
{
	REG_GRID_METER_RW_GRID_METER_CT_ENABLE,
	REG_GRID_METER_RW_GRID_METER_CT_RATE,
	REG_GRID_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1,
	REG_GRID_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1,
	REG_GRID_METER_R_VOLTAGE_OF_A_PHASE,
	REG_GRID_METER_R_VOLTAGE_OF_B_PHASE,
	REG_GRID_METER_R_VOLTAGE_OF_C_PHASE,
	REG_GRID_METER_R_CURRENT_OF_A_PHASE,
	REG_GRID_METER_R_CURRENT_OF_B_PHASE,
	REG_GRID_METER_R_CURRENT_OF_C_PHASE,
	REG_GRID_METER_R_FREQUENCY,
	REG_GRID_METER_R_ACTIVE_POWER_OF_A_PHASE_1,
	REG_GRID_METER_R_ACTIVE_POWER_OF_B_PHASE_1,
	REG_GRID_METER_R_ACTIVE_POWER_OF_C_PHASE_1,
	REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1,
	REG_GRID_METER_R_REACTIVE_POWER_OF_A_PHASE_1,
	REG_GRID_METER_R_REACTIVE_POWER_OF_B_PHASE_1,
	REG_GRID_METER_R_REACTIVE_POWER_OF_C_PHASE_1,
	REG_GRID_METER_R_TOTAL_REACTIVE_POWER_1,
	REG_GRID_METER_R_APPARENT_POWER_OF_A_PHASE_1,
	REG_GRID_METER_R_APPARENT_POWER_OF_B_PHASE_1,
	REG_GRID_METER_R_APPARENT_POWER_OF_C_PHASE_1,
	REG_GRID_METER_R_TOTAL_APPARENT_POWER_1,
	REG_GRID_METER_R_POWER_FACTOR_OF_A_PHASE,
	REG_GRID_METER_R_POWER_FACTOR_OF_B_PHASE,
	REG_GRID_METER_R_POWER_FACTOR_OF_C_PHASE,
	REG_GRID_METER_R_TOTAL_POWER_FACTOR,
	REG_PV_METER_RW_PV_METER_CT_ENABLE,
	REG_PV_METER_RW_PV_METER_CT_RATE,
	REG_PV_METER_R_TOTAL_ENERGY_FEED_TO_GRID_1,
	REG_PV_METER_R_TOTAL_ENERGY_CONSUMED_FROM_GRID_1,
	REG_PV_METER_R_VOLTAGE_OF_A_PHASE,
	REG_PV_METER_R_VOLTAGE_OF_B_PHASE,
	REG_PV_METER_R_VOLTAGE_OF_C_PHASE,
	REG_PV_METER_R_CURRENT_OF_A_PHASE,
	REG_PV_METER_R_CURRENT_OF_B_PHASE,
	REG_PV_METER_R_CURRENT_OF_C_PHASE,
	REG_PV_METER_R_FREQUENCY,
	REG_PV_METER_R_ACTIVE_POWER_OF_A_PHASE_1,
	REG_PV_METER_R_ACTIVE_POWER_OF_B_PHASE_1,
	REG_PV_METER_R_ACTIVE_POWER_OF_C_PHASE_1,
	REG_PV_METER_R_TOTAL_ACTIVE_POWER_1,
	REG_PV_METER_R_REACTIVE_POWER_OF_A_PHASE_1,
	REG_PV_METER_R_REACTIVE_POWER_OF_B_PHASE_1,
	REG_PV_METER_R_REACTIVE_POWER_OF_C_PHASE_1,
	REG_PV_METER_R_TOTAL_REACTIVE_POWER_1,
	REG_PV_METER_R_APPARENT_POWER_OF_A_PHASE_1,
	REG_PV_METER_R_APPARENT_POWER_OF_B_PHASE_1,
	REG_PV_METER_R_APPARENT_POWER_OF_C_PHASE_1,
	REG_PV_METER_R_TOTAL_APPARENT_POWER_1,
	REG_PV_METER_R_POWER_FACTOR_OF_A_PHASE,
	REG_PV_METER_R_POWER_FACTOR_OF_B_PHASE,
	REG_PV_METER_R_POWER_FACTOR_OF_C_PHASE,
	REG_PV_METER_R_TOTAL_POWER_FACTOR,
	REG_BATTERY_HOME_R_VOLTAGE,
	REG_BATTERY_HOME_R_CURRENT,
	REG_BATTERY_HOME_R_SOC,
	REG_BATTERY_HOME_R_STATUS,
	REG_BATTERY_HOME_R_RELAY_STATUS,
	REG_BATTERY_HOME_R_PACK_ID_OF_MIN_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_CELL_ID_OF_MIN_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_MIN_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_PACK_ID_OF_MAX_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_CELL_ID_OF_MAX_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_MAX_CELL_VOLTAGE,
	REG_BATTERY_HOME_R_PACK_ID_OF_MIN_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_CELL_ID_OF_MIN_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_MIN_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_PACK_ID_OF_MAX_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_CELL_ID_OF_MAX_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_MAX_CELL_TEMPERATURE,
	REG_BATTERY_HOME_R_MAX_CHARGE_CURRENT,
	REG_BATTERY_HOME_R_MAX_DISCHARGE_CURRENT,
	REG_BATTERY_HOME_R_CHARGE_CUT_OFF_VOLTAGE,
	REG_BATTERY_HOME_R_DISCHARGE_CUT_OFF_VOLTAGE,
	REG_BATTERY_HOME_R_BMU_SOFTWARE_VERSION,
	REG_BATTERY_HOME_R_LMU_SOFTWARE_VERSION,
	REG_BATTERY_HOME_R_ISO_SOFTWARE_VERSION,
	REG_BATTERY_HOME_R_BATTERY_NUMBER,
	REG_BATTERY_HOME_R_BATTERY_CAPACITY,
	REG_BATTERY_HOME_R_BATTERY_TYPE,
	REG_BATTERY_HOME_R_BATTERY_SOH,
	REG_BATTERY_HOME_R_BATTERY_WARNING_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_1,
	REG_BATTERY_HOME_R_BATTERY_CHARGE_ENERGY_1,
	REG_BATTERY_HOME_R_BATTERY_DISCHARGE_ENERGY_1,
	REG_BATTERY_HOME_R_BATTERY_ENERGY_CHARGE_FROM_GRID_1,
	REG_BATTERY_HOME_R_BATTERY_POWER,
	REG_BATTERY_HOME_R_BATTERY_REMAINING_TIME,
	REG_BATTERY_HOME_R_BATTERY_IMPLEMENTATION_CHARGE_SOC,
	REG_BATTERY_HOME_R_BATTERY_IMPLEMENTATION_DISCHARGE_SOC,
	REG_BATTERY_HOME_R_BATTERY_REMAINING_CHARGE_SOC,
	REG_BATTERY_HOME_R_BATTERY_REMAINING_DISCHARGE_SOC,
	REG_BATTERY_HOME_R_BATTERY_MAX_CHARGE_POWER,
	REG_BATTERY_HOME_R_BATTERY_MAX_DISCHARGE_POWER,
	REG_BATTERY_HOME_RW_BATTERY_MOS_CONTROL,
	REG_BATTERY_HOME_R_BATTERY_SOC_CALIBRATION,
	REG_BATTERY_HOME_R_BATTERY_SINGLE_CUT_ERROR_CODE,
	REG_BATTERY_HOME_R_BATTERY_FAULT_1_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_2_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_3_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_4_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_5_1,
	REG_BATTERY_HOME_R_BATTERY_FAULT_6_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_1_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_2_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_3_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_4_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_5_1,
	REG_BATTERY_HOME_R_BATTERY_WARNING_6_1,
	REG_INVERTER_HOME_R_VOLTAGE_L1,
	REG_INVERTER_HOME_R_VOLTAGE_L2,
	REG_INVERTER_HOME_R_VOLTAGE_L3,
	REG_INVERTER_HOME_R_CURRENT_L1,
	REG_INVERTER_HOME_R_CURRENT_L2,
	REG_INVERTER_HOME_R_CURRENT_L3,
	REG_INVERTER_HOME_R_POWER_L1_1,
	REG_INVERTER_HOME_R_POWER_L2_1,
	REG_INVERTER_HOME_R_POWER_L3_1,
	REG_INVERTER_HOME_R_POWER_TOTAL_1,
	REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L1,
	REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L2,
	REG_INVERTER_HOME_R_BACKUP_VOLTAGE_L3,
	REG_INVERTER_HOME_R_BACKUP_CURRENT_L1,
	REG_INVERTER_HOME_R_BACKUP_CURRENT_L2,
	REG_INVERTER_HOME_R_BACKUP_CURRENT_L3,
	REG_INVERTER_HOME_R_BACKUP_POWER_L1_1,
	REG_INVERTER_HOME_R_BACKUP_POWER_L2_1,
	REG_INVERTER_HOME_R_BACKUP_POWER_L3_1,
	REG_INVERTER_HOME_R_BACKUP_POWER_TOTAL_1,
	REG_INVERTER_HOME_R_FREQUENCY,
	REG_INVERTER_HOME_R_PV1_VOLTAGE,
	REG_INVERTER_HOME_R_PV1_CURRENT,
	REG_INVERTER_HOME_R_PV1_POWER_1,
	REG_INVERTER_HOME_R_PV2_VOLTAGE,
	REG_INVERTER_HOME_R_PV2_CURRENT,
	REG_INVERTER_HOME_R_PV2_POWER_1,
	REG_INVERTER_HOME_R_PV3_VOLTAGE,
	REG_INVERTER_HOME_R_PV3_CURRENT,
	REG_INVERTER_HOME_R_PV3_POWER_1,
	REG_INVERTER_HOME_R_PV4_VOLTAGE,
	REG_INVERTER_HOME_R_PV4_CURRENT,
	REG_INVERTER_HOME_R_PV4_POWER_1,
	REG_INVERTER_HOME_R_PV5_VOLTAGE,
	REG_INVERTER_HOME_R_PV5_CURRENT,
	REG_INVERTER_HOME_R_PV5_POWER_1,
	REG_INVERTER_HOME_R_PV6_VOLTAGE,
	REG_INVERTER_HOME_R_PV6_CURRENT,
	REG_INVERTER_HOME_R_PV6_POWER_1,
	REG_INVERTER_HOME_R_INVERTER_TEMP,
	REG_INVERTER_HOME_R_INVERTER_WARNING_1_1,
	REG_INVERTER_HOME_R_INVERTER_WARNING_2_1,
	REG_INVERTER_HOME_R_INVERTER_FAULT_1_1,
	REG_INVERTER_HOME_R_INVERTER_FAULT_2_1,
	REG_INVERTER_HOME_R_INVERTER_TOTAL_PV_ENERGY_1,
	REG_INVERTER_HOME_R_WORKING_MODE,
	REG_INVERTER_INFO_R_MASTER_SOFTWARE_VERSION_1,
	REG_INVERTER_INFO_R_SLAVE_SOFTWARE_VERSION_1,
	REG_INVERTER_INFO_R_SERIAL_NUMBER_1,
	REG_SYSTEM_INFO_RW_SYSTEM_TIME_YEAR_MONTH,
	REG_SYSTEM_INFO_RW_SYSTEM_TIME_DAY_HOUR,
	REG_SYSTEM_INFO_RW_SYSTEM_TIME_MINUTE_SECOND,
	REG_SYSTEM_INFO_R_EMS_SN_BYTE_1_2,
	REG_SYSTEM_INFO_R_EMS_VERSION_HIGH,
	REG_SYSTEM_INFO_R_EMS_VERSION_MIDDLE,
	REG_SYSTEM_INFO_R_EMS_VERSION_LOW,
	REG_SYSTEM_INFO_R_PROTOCOL_VERSION,
	REG_SYSTEM_CONFIG_RW_MAX_FEED_INTO_GRID_PERCENT,
	REG_SYSTEM_CONFIG_RW_PV_CAPACITY_STORAGE_1,
	REG_SYSTEM_CONFIG_RW_PV_CAPACITY_OF_GRID_INVERTER_1,
	REG_SYSTEM_CONFIG_RW_SYSTEM_MODE,
	REG_SYSTEM_CONFIG_RW_METER_CT_SELECT,
	REG_SYSTEM_CONFIG_RW_BATTERY_READY,
	REG_SYSTEM_CONFIG_RW_IP_METHOD,
	REG_SYSTEM_CONFIG_RW_LOCAL_IP_1,
	REG_SYSTEM_CONFIG_RW_SUBNET_MASK_1,
	REG_SYSTEM_CONFIG_RW_GATEWAY_1,
	REG_SYSTEM_CONFIG_RW_MODBUS_ADDRESS,
	REG_SYSTEM_CONFIG_RW_MODBUS_BAUD_RATE,
	REG_TIMING_RW_TIME_PERIOD_CONTROL_FLAG,
	REG_TIMING_RW_UPS_RESERVE_SOC,
	REG_TIMING_RW_TIME_DISCHARGE_START_TIME_1,
	REG_TIMING_RW_TIME_DISCHARGE_STOP_TIME_1,
	REG_TIMING_RW_TIME_DISCHARGE_START_TIME_2,
	REG_TIMING_RW_TIME_DISCHARGE_STOP_TIME_2,
	REG_TIMING_RW_CHARGE_CUT_SOC,
	REG_TIMING_RW_TIME_CHARGE_START_TIME_1,
	REG_TIMING_RW_TIME_CHARGE_STOP_TIME_1,
	REG_TIMING_RW_TIME_CHARGE_START_TIME_2,
	REG_TIMING_RW_TIME_CHARGE_STOP_TIME_2,
	REG_DISPATCH_RW_DISPATCH_START,
	REG_DISPATCH_RW_ACTIVE_POWER_1,
	REG_DISPATCH_RW_REACTIVE_POWER_1,
	REG_DISPATCH_RW_DISPATCH_MODE,
	REG_DISPATCH_RW_DISPATCH_SOC,
	REG_DISPATCH_RW_DISPATCH_TIME_1,
	REG_AUXILIARY_R_EMS_DI0,
	REG_AUXILIARY_R_EMS_DI1,
	REG_SYSTEM_OP_R_PV_INVERTER_ENERGY_1,
	REG_SYSTEM_OP_R_SYSTEM_TOTAL_PV_ENERGY_1,
	REG_SYSTEM_OP_R_SYSTEM_FAULT_1,
	REG_SAFETY_TEST_RW_GRID_REGULATION,
	REG_CUSTOM_LOAD,
	REG_CUSTOM_SYSTEM_DATE_TIME,
	REG_CUSTOM_GRID_CURRENT_A_PHASE
};



// These timers are used in the main loop.
#define RUNSTATE_INTERVAL 5000
#define STATUS_INTERVAL_FAST 1000
#define STATUS_INTERVAL_TEN_SECONDS 10000
#define STATUS_INTERVAL_ONE_MINUTE 60000
#define STATUS_INTERVAL_FIVE_MINUTE 300000
#define STATUS_INTERVAL_ONE_HOUR 3600000
#define STATUS_INTERVAL_ONE_DAY 86400000
#define UPDATE_STATUS_BAR_INTERVAL 500

// Reconnects. setupWifi reissues WiFi.begin if still not connected after WIFI_RETRY_INTERVAL, and mqttReconnect
// waits MQTT_RETRY_INTERVAL after a failed attempt. One attempt at the broker gives up on the TCP connect or a write
// after MQTT_CLIENT_TIMEOUT ms, and on the broker's answer after MQTT_REPLY_TIMEOUT seconds.
// On the Pico W, WiFi.begin blocks while the driver joins the access point, up to WIFI_JOIN_TIMEOUT ms, and nothing
// in the core feeds the watchdog meanwhile. The core's default is 15 s, longer than WATCHDOG_TIMEOUT.
#define WIFI_JOIN_TIMEOUT 3000
#define WIFI_RETRY_INTERVAL 10000
#define MQTT_RETRY_INTERVAL 5000
#define MQTT_CLIENT_TIMEOUT 2500
#define MQTT_REPLY_TIMEOUT 2

// Wemos OLED Shield set up. 64x48
// Pins D1 D2 if ESP8266
// Pins GPIO22 and GPIO21 (SCL/SDA) with optional reset on GPIO13 if ESP32
Adafruit_SSD1306 _display(-1); // No RESET Pin




/*
setup

The setup function runs once when you press reset or power the board
*/
void setup()
{

	// All for testing different baud rates to 'wake up' the inverter
	unsigned long knownBaudRates[7] = { 9600, 115200, 19200, 57600, 38400, 14400, 4800 };
	bool gotResponse = false;
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;
	modbusRequestAndResponse response;
	char baudRateString[10] = "";
	int baudRateIterator = -1;

#if defined MP_PICOW
	// First, so a hang anywhere in setup reboots too. See feedWatchdog.
	rp2040.wdt_begin(WATCHDOG_TIMEOUT);
#endif

	// Set up serial for debugging using an appropriate baud rate
	// This is for communication with the development environment, NOT the Alpha system
	// See Definitions.h for this.
	Serial.begin(9600);


	// Configure LED for output
	pinMode(LED_BUILTIN, OUTPUT);
	
	// Wire.setClock(10000);

	// Display time
	_display.begin(SSD1306_SWITCHCAPVCC, 0x3C);  // initialize OLED with the I2C addr 0x3C (for the 64x48)
	_display.clearDisplay();
	_display.display();
	updateOLED(false, "", "", _version);



	// Bit of a delay to give things time to kick in
	delay(100);


#if defined MP_PICOW
	// Bound the join inside WiFi.begin, see WIFI_JOIN_TIMEOUT. Milliseconds, see WiFiClass::setTimeout.
	WiFi.setTimeout(WIFI_JOIN_TIMEOUT);
#endif

	// Start WiFi. It connects in the background, see setupWifi.
	setupWifi();

#if defined MP_PICOW
	// Start OTA before anything that can stall on the inverter, so a bad build can always be replaced.
	// The image is staged in LittleFS, so build with a filesystem, see README.md.
	ArduinoOTA.setHostname(DEVICE_NAME);
	ArduinoOTA.setPassword(OTA_PASSWORD);
	// An upload takes longer than WATCHDOG_TIMEOUT. It reports progress after every chunk, at most about 6 s apart.
	ArduinoOTA.onProgress([](unsigned int, unsigned int) { feedWatchdog(); });
	ArduinoOTA.begin();
#endif

	// Configure MQTT to the address and port specified above
	_mqtt.setServer(MQTT_SERVER, MQTT_PORT);
	_wifi.setTimeout(MQTT_CLIENT_TIMEOUT);
	_mqtt.setSocketTimeout(MQTT_REPLY_TIMEOUT);
#ifdef DEBUG
	sprintf(_debugOutput, "About to request buffer");
	Serial.println(_debugOutput);
#endif
	for (_bufferSize = (MAX_MQTT_PAYLOAD_SIZE + MQTT_HEADER_SIZE); _bufferSize >= MIN_MQTT_PAYLOAD_SIZE + MQTT_HEADER_SIZE; _bufferSize = _bufferSize - 1024)
	{
#ifdef DEBUG
		sprintf(_debugOutput, "Requesting a buffer of : %d bytes", _bufferSize);
		Serial.println(_debugOutput);
#endif

		if (_mqtt.setBufferSize(_bufferSize))
		{
			
			_maxPayloadSize = _bufferSize - MQTT_HEADER_SIZE;
#ifdef DEBUG
			sprintf(_debugOutput, "_bufferSize: %d,\r\n\r\n_maxPayload (Including null terminator): %d", _bufferSize, _maxPayloadSize);
			Serial.println(_debugOutput);
#endif
			
			// Example, 2048, if declared as 2048 is positions 0 to 2047, and position 2047 needs to be zero.  2047 usable chars in payload.
			_mqttPayload = new char[_maxPayloadSize];
			emptyPayload();
			_commandPayload = new char[_maxPayloadSize];
			_fastPayload = new char[_maxPayloadSize];

			break;
		}
		else
		{
#ifdef DEBUG
			sprintf(_debugOutput, "Coudln't allocate buffer of %d bytes", _bufferSize);
			Serial.println(_debugOutput);
#endif
		}
	}
	

	// And any messages we are subscribed to will be pushed to the mqttCallback function for processing
	_mqtt.setCallback(mqttCallback);




	// Set up the serial for communicating with the MAX
	_modBus = new RS485Handler;
	_modBus->setDebugOutput(_debugOutput);

	// Set up the helper class for reading with reading registers
	_registerHandler = new RegisterHandler(_modBus);

	// Iterate known baud rates until we find a success
	while (!gotResponse)
	{
		// Starts at -1, so increment to 0 for example
		baudRateIterator++;

		// Go back to zero if beyond the bounds
		if (baudRateIterator > (sizeof(knownBaudRates) / sizeof(knownBaudRates[0])) - 1)
		{
			baudRateIterator = 0;
		}

		// Update the display
		sprintf(baudRateString, "%u", knownBaudRates[baudRateIterator]);

		updateOLED(false, "Test Baud", baudRateString, "");
#ifdef DEBUG
		sprintf(_debugOutput, "About To Try: %u", knownBaudRates[baudRateIterator]);
		Serial.println(_debugOutput);
#endif
		// Set the rate
		_modBus->setBaudRate(knownBaudRates[baudRateIterator]);

		// Ask for a reading
		result = _registerHandler->readHandledRegister(REG_SAFETY_TEST_RW_GRID_REGULATION, &response);
		if (result != modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
		{
#ifdef DEBUG
			sprintf(_debugOutput, "Baud Rate Checker Problem: %s", response.statusMqttMessage);
			Serial.println(_debugOutput);
#endif
			updateOLED(false, "Test Baud", baudRateString, response.displayMessage);

			// Delay a while before trying the next
			waitServicingOta(1000);
		}
		else
		{
			// Excellent, baud rate is set in the class, we got a response.. get out of here
			gotResponse = true;
		}
	}

	// Get the serial number (especially prefix for error codes)
	getSerialNumber();

	// First try at MQTT. loop() keeps trying if WiFi or the broker is not there yet.
	mqttReconnect();

	updateOLED(false, "", "", _version);
}




/*
loop

The loop function runs overand over again until power down or reset
*/
void loop()
{
	feedWatchdog();

	// Refresh LED Screen, will cause the status asterisk to flicker
	updateOLED(true, "", "", "");

	// Make sure WiFi is good. Returns at once, connected or not.
	setupWifi();

#if defined MP_PICOW
	// Takes over and reboots if an upload has been accepted. Only ever called between RS485 requests.
	ArduinoOTA.handle();
#endif

	// make sure mqtt is still connected
	if ((!_mqtt.connected()) || !_mqtt.loop())
	{
		mqttReconnect();
	}

	// Keep the battery idle while Home Assistant is gone
	holdIdleWhileHaGone();

	// Read and transmit the controller registers
	sendFastState();

	// Check and display the runstate on the display
	updateRunstate();

	// Read and transmit all configured data to MQTT
	sendData();

	
	// Force Restart?
#ifdef FORCE_RESTART
	static unsigned long autoReboot = 0;

	if (checkTimer(&autoReboot, FORCE_RESTART_HOURS * 60 * 60 * 1000))
	{
#if defined MP_PICOW
		rp2040.reboot();
#else
		ESP.restart();
#endif
	}
#endif


}





/*
setupWifi

Connect to WiFi without waiting for it, so OTA and the RS485 work carry on while WiFi is down.
WiFi.begin joins the access point, blocking for up to WIFI_JOIN_TIMEOUT, and leaves DHCP running in the background.
loop() and waitServicingOta call this on every pass: it returns at once, and reissues WiFi.begin if the access point
is lost, or if still not joined after WIFI_RETRY_INTERVAL. A join whose DHCP is still pending is left to finish.
*/

// True while the station is joined to the access point, or a join is in progress. On the Pico W, WiFi.status()
// only says whether lwIP holds an IPv4 address (LwipIntfDev::connected). Losing the access point does not clear
// that address, only the DHCP lease running out does, so status() reads connected for hours after the link has
// gone, and the core never rejoins on its own. The driver does clear its join flag on link loss
// (cyw43_cb_tcpip_set_link_down), which is what cyw43_wifi_link_status reports. Reading it costs no bus time.
static bool wifiJoined()
{
#if defined MP_PICOW
	return cyw43_wifi_link_status(&cyw43_state, CYW43_ITF_STA) == CYW43_LINK_JOIN;
#else
	return WiFi.status() == WL_CONNECTED;
#endif
}

void setupWifi()
{
	static bool connecting = false;
	static unsigned long lastBegin = 0;
	wl_status_t status = (wl_status_t)WiFi.status();
	bool joined = wifiJoined();

	if (status == WL_CONNECTED && joined)
	{
		if (connecting)
		{
			connecting = false;

			// Set the hostname for this Arduino
			WiFi.hostname(DEVICE_NAME);

			// Output some debug information
#ifdef DEBUG
			Serial.print("WiFi connected, IP is");
			Serial.print(WiFi.localIP());
#endif

			// Connected, so ditch out with blank screen
			updateOLED(false, "", "", _version);
		}
		return;
	}

	// Joined with DHCP still running: WL_DISCONNECTED means the join succeeded and lwIP has no address yet. A new
	// begin would leave the access point and stop DHCP (_beginInternal calls end()), so wait. A join that timed
	// out reads WL_NO_SHIELD instead, since the core removed the interface, so it falls through to the retry.
	if (status == WL_DISCONNECTED && joined)
	{
		return;
	}

	// A lost access point arrives here with connecting false, so it begins again at once. A join still being tried
	// waits out the retry interval.
	if (!retryDue(connecting, lastBegin, millis(), WIFI_RETRY_INTERVAL))
	{
		return;
	}
	connecting = true;
	lastBegin = millis();

#ifdef DEBUG
	sprintf(_debugOutput, "Connecting to %s", WIFI_SSID);
	Serial.println(_debugOutput);
#endif
	updateOLED(false, "Connecting", "WiFi...", _version);

	// Set up in Station Mode - Will be connecting to an access point
	WiFi.mode(WIFI_STA);

	// The join inside WiFi.begin blocks for up to WIFI_JOIN_TIMEOUT with no feed in the core, so start it on a fresh
	// watchdog. Past the timeout the driver still waits out a join that is mid negotiation (the while (true) in
	// CYW43::begin); the watchdog is the backstop there.
	feedWatchdog();
	WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}




/*
checkTimer

Check to see if the elapsed interval has passed since the passed in millis() value. If it has, return true and update the lastRun.
See timerDue in Timer.h, which also handles the millis() wrap.
*/
bool checkTimer(unsigned long *lastRun, unsigned long interval)
{
	uint32_t last = *lastRun;
	bool due = timerDue(&last, millis(), interval);

	*lastRun = last;
	return due;
}

/*
waitServicingOta

A delay that still answers OTA uploads and keeps WiFi trying, for the long waits in setup.
espota.py gives up if the Pico takes more than 10 seconds to start an accepted upload.
*/
void waitServicingOta(unsigned long ms)
{
	unsigned long start = millis();

	while (millis() - start < ms)
	{
		feedWatchdog();
		setupWifi();
#if defined MP_PICOW
		ArduinoOTA.handle();
#endif
		delay(10);
	}
}

/*
serviceBetweenReads

Called between the register reads of a schedule, so a long schedule does not hold up OTA, incoming commands
or the fast state. The RS485 bus is idle here, so they can use it. Each builds its payload in its own buffer.
*/
void serviceBetweenReads()
{
	feedWatchdog();
#if defined MP_PICOW
	ArduinoOTA.handle();
#endif
	_mqtt.loop();
	sendFastState();
}

/*
updateOLED

Update the OLED. Use "NULL" for no change to a line or "" for an empty line.
Three parameters representing each of the three lines available for status indication - Top line functionality fixed
*/
void updateOLED(bool justStatus, const char* line2, const char* line3, const char* line4)
{
	static unsigned long updateStatusBar = 0;


	_display.clearDisplay();
	_display.setTextSize(1);
	_display.setTextColor(WHITE);
	_display.setCursor(0, 0);

	char line1Contents[OLED_CHARACTER_WIDTH];
	char line2Contents[OLED_CHARACTER_WIDTH];
	char line3Contents[OLED_CHARACTER_WIDTH];
	char line4Contents[OLED_CHARACTER_WIDTH];

	// Ensure only dealing with 10 chars passed in, and null terminate.
	if (strlen(line2) > OLED_CHARACTER_WIDTH - 1)
	{
		strncpy(line2Contents, line2, OLED_CHARACTER_WIDTH - 1);
		line2Contents[OLED_CHARACTER_WIDTH - 1] = 0;
	}
	else
	{
		strcpy(line2Contents, line2);
	}


	if (strlen(line3) > OLED_CHARACTER_WIDTH - 1)
	{
		strncpy(line3Contents, line3, OLED_CHARACTER_WIDTH - 1);
		line3Contents[OLED_CHARACTER_WIDTH - 1] = 0;
	}
	else
	{
		strcpy(line3Contents, line3);
	}


	if (strlen(line4) > OLED_CHARACTER_WIDTH - 1)
	{
		strncpy(line4Contents, line4, OLED_CHARACTER_WIDTH - 1);
		line4Contents[OLED_CHARACTER_WIDTH - 1] = 0;
	}
	else
	{
		strcpy(line4Contents, line4);
	}

	// Only update the operating indicator once per half second.
	if (checkTimer(&updateStatusBar, UPDATE_STATUS_BAR_INTERVAL))
	{
		// Simply swap between space and asterisk every time we come here to give some indication of activity
		_oledOperatingIndicator = (_oledOperatingIndicator == '*') ? ' ' : '*';
	}

	// There's ten characters we can play with, width wise.
	sprintf(line1Contents, "%s%c%c%c", "A2M    ", _oledOperatingIndicator, (WiFi.status() == WL_CONNECTED ? 'W' : ' '), (_mqtt.connected() ? 'M' : ' ') );
	_display.println(line1Contents);




	// Next line

	_display.setCursor(0, 12);
	if (!justStatus)
	{
		_display.println(line2Contents);
		strcpy(_oledLine2, line2Contents);
	}
	else
	{
		_display.println(_oledLine2);
	}



	_display.setCursor(0, 24);
	if (!justStatus)
	{
		_display.println(line3Contents);
		strcpy(_oledLine3, line3Contents);
	}
	else
	{
		_display.println(_oledLine3);
	}

	_display.setCursor(0, 36);
	if (!justStatus)
	{
		_display.println(line4Contents);
		strcpy(_oledLine4, line4Contents);
	}
	else
	{
		_display.println(_oledLine4);
	}
	// Refresh the display
	_display.display();
}








/*
getSerialNumber

Display on load to demonstrate connectivty and send the prefix into RegisterHandler as
some system fault descriptions depend on knowing whether an AL based or AE based inverter.
*/
modbusRequestAndResponseStatusValues getSerialNumber()
{
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;
	modbusRequestAndResponse response;

	char oledLine3[OLED_CHARACTER_WIDTH];
	char oledLine4[OLED_CHARACTER_WIDTH];

	// Get the serial number
	result = _registerHandler->readHandledRegister(REG_SYSTEM_INFO_R_EMS_SN_BYTE_1_2, &response);


	if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
	{
		// strncpy doesn't null terminate.
		strncpy(oledLine3, &response.dataValueFormatted[0], 10);
		oledLine3[10] = 0;
		strncpy(oledLine4, &response.dataValueFormatted[10], 5);
		oledLine4[5] = 0;
		updateOLED(false, "Hello", oledLine3, oledLine4);
#ifdef DEBUG
		sprintf(_debugOutput, "Alpha Serial Number: %s", response.dataValueFormatted);
		Serial.println(_debugOutput);
#endif

		_registerHandler->setSerialNumberPrefix(response.dataValueFormatted[0], response.dataValueFormatted[1]);
	}
	else
	{
		updateOLED(false, "Alpha sys", "not known", "");
	}

	waitServicingOta(4000);

	//Flash the LED
	digitalWrite(LED_BUILTIN, LOW);
	delay(4);
	digitalWrite(LED_BUILTIN, HIGH);

	return result;
}


/*
updateRunstate

Determines a few things about the sytem and updates the display
Things updated - Dispatch state discharge/charge, battery power, battery percent
*/
void updateRunstate()
{
	
	static unsigned long lastRun = 0;
	modbusRequestAndResponse response;
	modbusRequestAndResponseStatusValues request;

	char runningMode[OLED_CHARACTER_WIDTH] = "Other";
	char batteryPower[OLED_CHARACTER_WIDTH] = "";
	char batterySOC[OLED_CHARACTER_WIDTH] = "";



	if (checkTimer(&lastRun, RUNSTATE_INTERVAL))
	{
		//Flash the LED
		digitalWrite(LED_BUILTIN, LOW);
		delay(4);
		digitalWrite(LED_BUILTIN, HIGH);


		// Dispatch start, battery power and SOC come from the fast state, see keepForDisplay
		if (!_shownDispatchStart.ok || !_shownBatteryPower.ok || !_shownSoc.ok)
		{
			updateOLED(false, "", "", !_shownDispatchStart.ok ? _shownDispatchStart.displayMessage : !_shownBatteryPower.ok ? _shownBatteryPower.displayMessage : _shownSoc.displayMessage);
			return;
		}

		// Is Alpha2MQTT controlling the inverter?
		if (_shownDispatchStart.value == DISPATCH_START_START)
		{
			// Yep, so get the mode.  And is it state of charge control or is the user using a different dispatch mode?
			request = _registerHandler->readHandledRegister(REG_DISPATCH_RW_DISPATCH_MODE, &response);
			bool socControl = request == modbusRequestAndResponseStatusValues::readDataRegisterSuccess && response.unsignedShortValue == DISPATCH_MODE_STATE_OF_CHARGE_CONTROL;
			if (socControl)
			{
				// Yep, SOC, so determine if charging or charging by looking at power
				request = _registerHandler->readHandledRegister(REG_DISPATCH_RW_ACTIVE_POWER_1, &response);
			}
			if (request != modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
			{
#ifdef DEBUG
				Serial.println(response.statusMqttMessage);
#endif
				updateOLED(false, "", "", response.displayMessage);
				return;
			}
			if (socControl)
			{
				if (response.signedIntValue < 32000)
				{
					strcpy(runningMode, "Charge");
				}
				else if (response.signedIntValue > 32000)
				{
					strcpy(runningMode, "Discharge");
				}
				else
				{
					strcpy(runningMode, "Hold");
				}
			}
		}
		else
		{
			strcpy(runningMode, "Normal");
		}

		sprintf(batteryPower, "Bat:%dW", (int)_shownBatteryPower.value);
		sprintf(batterySOC, "%0.02f%%", _shownSoc.value * 0.1);
		updateOLED(false, runningMode, batteryPower, batterySOC);
	}
}




/*
mqttReconnect

Makes one attempt to connect to the MQTT broker and subscribe, then returns either way, so OTA and the RS485 work
carry on while the broker is away. loop() calls it again on its next pass. It returns at once while WiFi is down,
and waits MQTT_RETRY_INTERVAL after a failed attempt.
*/
void mqttReconnect()
{
	static bool tried = false;
	static unsigned long lastTry = 0;
	bool subscribed = false;
	char subscriptionDef[100];

	if (WiFi.status() != WL_CONNECTED || !retryDue(tried, lastTry, millis(), MQTT_RETRY_INTERVAL))
	{
		return;
	}

	_mqtt.disconnect();		// Just in case.
	delay(200);

#ifdef DEBUG
	Serial.print("Attempting MQTT connection...");
#endif

	updateOLED(false, "Connecting", "MQTT...", _version);
	delay(100);

	// One attempt takes up to MQTT_CLIENT_TIMEOUT plus MQTT_REPLY_TIMEOUT, so start it with a fresh watchdog
	feedWatchdog();

	// Attempt to connect
	// An empty MQTT_USERNAME means connect without credentials
	if (strlen(MQTT_USERNAME) == 0 ? _mqtt.connect(DEVICE_NAME) : _mqtt.connect(DEVICE_NAME, MQTT_USERNAME, MQTT_PASSWORD))
	{
		Serial.println("Connected MQTT");

		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_READ_HANDLED_REGISTER);
		subscribed = _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_READ_RAW_REGISTER);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_SET_CHARGE);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_SET_DISCHARGE);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_SET_NORMAL);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_WRITE_RAW_SINGLE_REGISTER);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_WRITE_RAW_DATA_REGISTER);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		sprintf(subscriptionDef, "%s", DEVICE_NAME MQTT_SUB_REQUEST_READ_HANDLED_REGISTER_ALL);
		subscribed = subscribed && _mqtt.subscribe(subscriptionDef);
		subscribed = subscribed && _mqtt.subscribe(MQTT_SUB_HA_STATUS);
	}

	if (subscribed)
	{
		// Connected, so ditch out with runstate on the screen
		tried = false;
		updateRunstate();
		return;
	}

#ifdef DEBUG
	sprintf(_debugOutput, "MQTT Failed: RC is %d\r\nTrying again in five seconds...", _mqtt.state());
	Serial.println(_debugOutput);
#endif

	// loop() only calls back for a dropped connection, so drop one that is missing its subscriptions
	_mqtt.disconnect();
	tried = true;
	lastTry = millis();
}





/*
keepForDisplay

Keeps the latest dispatch start, battery power and SOC read by any schedule. The fast state reads all three
every second, so updateRunstate shows them without reading them again.
*/
void keepForDisplay(uint16_t registerAddress, modbusRequestAndResponseStatusValues result, modbusRequestAndResponse& response)
{
	displayReading* kept;

	switch (registerAddress)
	{
	case REG_DISPATCH_RW_DISPATCH_START:
		kept = &_shownDispatchStart;
		break;
	case REG_BATTERY_HOME_R_BATTERY_POWER:
		kept = &_shownBatteryPower;
		break;
	case REG_BATTERY_HOME_R_SOC:
		kept = &_shownSoc;
		break;
	default:
		return;
	}

	kept->ok = result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess;
	kept->value = registerAddress == REG_BATTERY_HOME_R_BATTERY_POWER ? response.signedShortValue : response.unsignedShortValue;
	strcpy(kept->displayMessage, response.displayMessage);
}

/*
addStateInfo

Query the handled register in the usual way, and add the cleansed output to the buffer
*/
modbusRequestAndResponseStatusValues addStateInfo(uint16_t registerAddress, bool addComma, modbusRequestAndResponseStatusValues& resultAddedToPayload)
{
	char stateAddition[128] = ""; // 128 should cover individual additions to the payload
	char addQuote = false;
	modbusRequestAndResponse response;
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;

	// Read the register
	result = _registerHandler->readHandledRegister(registerAddress, &response);
	keepForDisplay(registerAddress, result, response);

	if (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess)
	{
		// The inverter has left dispatch: the lease ran out, or something else stopped it. The cached block no
		// longer runs, so the next charge or discharge writes it in full rather than only the power.
		if (registerAddress == REG_DISPATCH_RW_DISPATCH_START && response.unsignedShortValue != DISPATCH_START_START)
		{
			_activeDispatch.valid = false;
		}

		// Add a quote if the return data type is character or has been converted from lookup to description.
		addQuote = (response.returnDataType == modbusReturnDataType::character || response.hasLookup);

		sprintf(stateAddition, "    \"%s\": %s%s%s%s\r\n", response.mqttName, addQuote ? "\"" : "", response.dataValueFormatted, addQuote ? "\"" : "", addComma ? "," : "");

		/*
		ABC,
		1234 LEN
		0123
		*/

		// Let the onward process also know if the buffer failed.
		resultAddedToPayload = addToPayload(stateAddition);
	}
	else
	{
#ifdef DEBUG
		sprintf(_debugOutput, "Failed to addStateInfo for: %u, Result was: %d", registerAddress, result);
		Serial.println(_debugOutput);
#endif
	}
	return result;
}




modbusRequestAndResponseStatusValues addToPayload(char* addition)
{
	int targetRequestedSize = strlen(_mqttPayload) + strlen(addition);

	// If max payload size is 2048 it is stored as (0-2047), however character 2048  (position 2047) is null terminator so 2047 chars usable usable
	if (targetRequestedSize > _maxPayloadSize - 1)
	{
		// Safely print using snprintf
		snprintf(_mqttPayload, _maxPayloadSize, "{\r\n    \"mqttError\": \"Length of payload exceeds %d bytes.  Length would be %d bytes.\"\r\n}", _maxPayloadSize - 1, targetRequestedSize);

		return modbusRequestAndResponseStatusValues::payloadExceededCapacity;
	}
	else
	{
		// Add to the payload by sprintf back on itself with the addition
		sprintf(_mqttPayload, "%s%s", _mqttPayload, addition);

		return modbusRequestAndResponseStatusValues::addedToPayload;
	}
}


/*
sendData

Runs once every loop, checks to see if time periods have elapsed to allow the schedules to run.
Each time, the appropriate arrays are iterated, processed and added to the payload.
*/
void sendData()
{
	static unsigned long lastRunTenSeconds = 0;
	static unsigned long lastRunOneMinute = 0;
	static unsigned long lastRunFiveMinutes = 0;
	static unsigned long lastRunOneHour = 0;
	static unsigned long lastRunOneDay = 0;
	int numberOfRegisters;
	// Update all parameters and send to MQTT.
	if (checkTimer(&lastRunTenSeconds, STATUS_INTERVAL_TEN_SECONDS))
	{
		numberOfRegisters = sizeof(_mqttTenSecondStatusRegisters) / sizeof(_mqttTenSecondStatusRegisters[0]);
		sendDataFromAppropriateArray(_mqttTenSecondStatusRegisters, numberOfRegisters, DEVICE_NAME MQTT_MES_STATE_SECOND_TEN);
	}

	// Update all parameters and send to MQTT.
	if (checkTimer(&lastRunOneMinute, STATUS_INTERVAL_ONE_MINUTE))
	{
		numberOfRegisters = sizeof(_mqttOneMinuteStatusRegisters) / sizeof(_mqttOneMinuteStatusRegisters[0]);
		sendDataFromAppropriateArray(_mqttOneMinuteStatusRegisters, numberOfRegisters, DEVICE_NAME MQTT_MES_STATE_MINUTE_ONE);
#if defined MP_PICOW
		sendDeviceState();
#endif
	}

	// Update all parameters and send to MQTT.
	if (checkTimer(&lastRunFiveMinutes, STATUS_INTERVAL_FIVE_MINUTE))
	{
		numberOfRegisters = sizeof(_mqttFiveMinuteStatusRegisters) / sizeof(_mqttFiveMinuteStatusRegisters[0]);
		sendDataFromAppropriateArray(_mqttFiveMinuteStatusRegisters, numberOfRegisters, DEVICE_NAME MQTT_MES_STATE_MINUTE_FIVE);
	}

	// Update all parameters and send to MQTT.
	if (checkTimer(&lastRunOneHour, STATUS_INTERVAL_ONE_HOUR))
	{
		numberOfRegisters = sizeof(_mqttOneHourStatusRegisters) / sizeof(_mqttOneHourStatusRegisters[0]);
		sendDataFromAppropriateArray(_mqttOneHourStatusRegisters, numberOfRegisters, DEVICE_NAME MQTT_MES_STATE_HOUR_ONE);
	}

	// Update all parameters and send to MQTT.
	if (checkTimer(&lastRunOneDay, STATUS_INTERVAL_ONE_DAY))
	{
		numberOfRegisters = sizeof(_mqttOneDayStatusRegisters) / sizeof(_mqttOneDayStatusRegisters[0]);
		sendDataFromAppropriateArray(_mqttOneDayStatusRegisters, numberOfRegisters, DEVICE_NAME MQTT_MES_STATE_DAY_ONE);
	}
}

#if defined MP_PICOW
/*
sendDeviceState

Publishes the Pico W supply: VSYS in millivolts and whether USB power is present.
VSYS is read through a 1:3 divider on GPIO29, which the WiFi chip also uses as its SPI clock,
so the read holds the CYW43 lock and hands the pin back. The driver reclaims it on its next transfer.
*/
void sendDeviceState()
{
	uint32_t total = 0;
	uint32_t vsysMillivolts;
	bool usbPower;
	char stateAddition[64];

	cyw43_thread_enter();
	adc_init();
	adc_gpio_init(PICO_VSYS_PIN);
	adc_select_input(PICO_VSYS_PIN - ADC_BASE_PIN);
	for (int i = 0; i < 16; i++)
	{
		total += adc_read();
	}
	cyw43_thread_exit();

	// 16 samples of 12 bits against a 3.3 V reference, times 3 for the divider
	vsysMillivolts = (total * 3300UL * 3UL) / (16UL * 4096UL);
	usbPower = cyw43_arch_gpio_get(CYW43_WL_GPIO_VBUS_PIN);

	emptyPayload();
	sprintf(stateAddition, "{\r\n    \"vsys_mv\": %lu,\r\n    \"usb_power\": %s\r\n}", (unsigned long)vsysMillivolts, usbPower ? "true" : "false");
	addToPayload(stateAddition);
	sendMqtt(DEVICE_NAME MQTT_MES_STATE_DEVICE);
}
#endif

/*
sendFastState

Reads the fast registers back to back and publishes them straight away, once every STATUS_INTERVAL_FAST.
seq counts up by one per message from boot, ms is millis() when the reads started and read_ms is how long they took,
so a controller can spot gaps, restarts and the real cycle time. haIdle is 1 while Home Assistant counts as gone and
the idle hold is kept. Registers that fail to read are left out.
*/
void sendFastState()
{
	static unsigned long lastRun = 0;
	static unsigned long seq = 0;
	unsigned long started;
	char stateAddition[128];
	char* interruptedPayload;
	modbusRequestAndResponseStatusValues resultAddedToPayload;
	int numberOfRegisters = sizeof(_mqttFastStatusRegisters) / sizeof(_mqttFastStatusRegisters[0]);

	if (!checkTimer(&lastRun, STATUS_INTERVAL_FAST))
	{
		return;
	}

	// A slow schedule may be part way through the main payload
	interruptedPayload = _mqttPayload;
	_mqttPayload = _fastPayload;
	emptyPayload();

	started = millis();
	resultAddedToPayload = addToPayload("{\r\n");
	for (int l = 0; l < numberOfRegisters && resultAddedToPayload == modbusRequestAndResponseStatusValues::addedToPayload; l++)
	{
		// Always a comma, the stamp below closes the object
		addStateInfo(_mqttFastStatusRegisters[l], true, resultAddedToPayload);
	}

	if (resultAddedToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
	{
		snprintf(stateAddition, sizeof(stateAddition), "    \"seq\": %lu,\r\n    \"ms\": %lu,\r\n    \"read_ms\": %lu,\r\n    \"haIdle\": %d\r\n}", ++seq, started, millis() - started, haGone(_idleHold) ? 1 : 0);
		addToPayload(stateAddition);
	}

	sendMqtt(DEVICE_NAME MQTT_MES_STATE_FAST);
	_mqttPayload = interruptedPayload;
}

void sendDataFromAppropriateArray(const uint16_t* registerArray, int numberOfRegisters, char* topic)
{
	int	l = 0;

	// For storing results
	modbusRequestAndResponseStatusValues resultAddedToPayload;

	// An empty schedule has nothing to read, so publish nothing
	if (numberOfRegisters == 0)
	{
		return;
	}

	emptyPayload();

	resultAddedToPayload = addToPayload("{\r\n");
	if (resultAddedToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
	{
		for (l = 0; l < numberOfRegisters; l++)
		{
			addStateInfo(registerArray[l], l < (numberOfRegisters - 1), resultAddedToPayload);
			
			if (resultAddedToPayload == modbusRequestAndResponseStatusValues::payloadExceededCapacity)
			{
				// If the response to addStateInfo is payload exceeded get out, We will permit failing registers to carry on and try the next one.
				break;
			}

			if (l < numberOfRegisters - 1)
			{
				serviceBetweenReads();
			}
		}
		if (resultAddedToPayload != modbusRequestAndResponseStatusValues::payloadExceededCapacity)
		{
			// Footer, if we haven't already bombed out with payload issues
			resultAddedToPayload = addToPayload("}");
		}

		// And send
		sendMqtt(topic);
	}
	else
	{
#ifdef DEBUG
		Serial.println("Invalid buffer");
#endif
	}
}



/*
closeSetResponse

Ends a set/charge, set/discharge or set/normal response with the request's seq, 0 if it sent none,
and the millis() when the writes finished, so the sender can match the response and time the round trip.
*/
modbusRequestAndResponseStatusValues closeSetResponse(char* seq)
{
	char stateAddition[64];

	sprintf(stateAddition, ",\r\n    \"seq\": %lu,\r\n    \"ms\": %lu\r\n}", strtoul(seq, NULL, 10), millis());
	return addToPayload(stateAddition);
}



/*
writeFullDispatchBlock

Writes the whole dispatch block in SOC control mode, which renews its lease: one nine register frame, about 50 ms
on the bus, or five single writes 400 ms apart for an inverter that refuses the frame. _activeDispatch is cleared
first and set to this block, stamped writeStartedMs, only once the inverter has acknowledged all of it.
Returns the last write's status. *frames says which path ran. After a failed single write, *failedRegister names it.
*/
modbusRequestAndResponseStatusValues writeFullDispatchBlock(bool charge, uint32_t powerRegister, uint16_t socRegister, uint32_t durationS, uint32_t writeStartedMs, modbusRequestAndResponse* rs, const char** failedRegister, int* frames)
{
	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;

	_activeDispatch.valid = false;
	*failedRegister = NULL;
	*frames = 1;

	if (!_blockFrameRefused)
	{
		result = _registerHandler->writeDispatchBlock(DISPATCH_START_START, powerRegister, DISPATCH_MODE_STATE_OF_CHARGE_CONTROL, socRegister, durationS, rs);
		if (result == modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
		{
			_activeDispatch = { true, charge, socRegister, durationS, writeStartedMs };
			return result;
		}
	}

	// An exception to the nine register write, then five single writes that land, says this inverter
	// will not take the frame. An exception the single writes also get is about the values instead.
	bool blockRefused = !_blockFrameRefused && result == modbusRequestAndResponseStatusValues::slaveError;

	// An inverter that will not take a nine register write gets the five single writes, 400 ms apart.
	// Keep the order START, ACTIVE_POWER, TIME, SOC, MODE: no other is proven on hardware.
	*frames = 5;
	rs->registerCount = 1;
	result = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_DISPATCH_START, DISPATCH_START_START, rs);
	if (result != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
	{
		*failedRegister = "REG_DISPATCH_RW_DISPATCH_START";
		return result;
	}
	delay(RS485_TRIES * 50);
	rs->registerCount = 2;
	result = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_ACTIVE_POWER_1, powerRegister, rs);
	if (result != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
	{
		*failedRegister = "REG_DISPATCH_RW_ACTIVE_POWER_1";
		return result;
	}
	delay(RS485_TRIES * 50);
	rs->registerCount = 2;
	result = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_DISPATCH_TIME_1, durationS, rs);
	if (result != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
	{
		*failedRegister = "REG_DISPATCH_RW_DISPATCH_TIME_1";
		return result;
	}
	delay(RS485_TRIES * 50);
	rs->registerCount = 1;
	result = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_DISPATCH_SOC, socRegister, rs);
	if (result != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
	{
		*failedRegister = "REG_DISPATCH_RW_DISPATCH_SOC";
		return result;
	}
	delay(RS485_TRIES * 50);
	rs->registerCount = 1;
	result = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_DISPATCH_MODE, DISPATCH_MODE_STATE_OF_CHARGE_CONTROL, rs);
	if (result != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
	{
		*failedRegister = "REG_DISPATCH_RW_DISPATCH_MODE";
		return result;
	}

	_activeDispatch = { true, charge, socRegister, durationS, writeStartedMs };
	_blockFrameRefused = _blockFrameRefused || blockRefused;
	return result;
}



/*
writeDispatchStop

Ends any dispatch, so the inverter goes back to its own mode: start 0 to 0x0880. Clears _activeDispatch, so the next
charge or discharge writes the whole block.
*/
modbusRequestAndResponseStatusValues writeDispatchStop(modbusRequestAndResponse* rs)
{
	_activeDispatch.valid = false;
	rs->registerCount = 1;
	return _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_DISPATCH_START, DISPATCH_START_STOP, rs);
}



/*
holdIdleWhileHaGone

While Home Assistant is gone (HA_GONE_AFTER_MS after its will said offline, or that long off the broker) the battery
is held idle instead of left to the inverter's own mode: a charge of 0 W to 100 % for IDLE_HOLD_DURATION_S, written as
a full block every IDLE_HOLD_REWRITE_MS. Charge 0 W stores no solar surplus, it exports. Once Home Assistant is back,
by its birth or a reconnect, a hold this wrote is ended in the same pass with a set/normal, so control returns at once.
A set request from Home Assistant replaces the hold instead and nothing is written over it. A failed write of either kind
is tried again after IDLE_HOLD_RETRY_MS. _activeDispatch is kept as after a set/charge or set/normal, so the next
command follows the same power-only rules. One block write or one stop write per call at most, never a wait.
*/
void holdIdleWhileHaGone()
{
	modbusRequestAndResponse response;
	const char* failedRegister = NULL;
	int frames = 1;
	uint32_t now = millis();
	IdleHoldWrite write = idleHoldDue(_idleHold, _mqtt.connected(), now);
	modbusRequestAndResponseStatusValues result;

	if (write == IdleHoldWrite::none)
	{
		return;
	}
	if (write == IdleHoldWrite::hold)
	{
		result = writeFullDispatchBlock(true, 32000, dispatchSocFromPercent(100), IDLE_HOLD_DURATION_S, now, &response, &failedRegister, &frames);
	}
	else
	{
		result = writeDispatchStop(&response);
	}

	bool ok = result == modbusRequestAndResponseStatusValues::writeDataRegisterSuccess;
	idleHoldWritten(_idleHold, write, ok, now);

#ifdef DEBUG
	sprintf(_debugOutput, "Idle %s write %s, %d frames", write == IdleHoldWrite::hold ? "hold" : "release", ok ? "acknowledged" : "failed", frames);
	Serial.println(_debugOutput);
#endif
}



/*
mqttCallback()

// This function is executed when an MQTT message arrives on a topic that we are subscribed to.
*/
void mqttCallback(char* topic, byte* message, unsigned int length)
{
	// Reject oversized payloads before they reach the fixed buffer below
	if (!requestPayloadFits(length))
	{
		return;
	}

	// Home Assistant's birth or will. Only noted here: loop() writes the idle hold, between RS485 requests.
	if (strcmp(topic, MQTT_SUB_HA_STATUS) == 0)
	{
		idleHoldStatus(_idleHold, haStatusFromPayload(message, length), millis());
		return;
	}

	modbusRequestAndResponseStatusValues result = modbusRequestAndResponseStatusValues::preProcessing;
	modbusRequestAndResponse response;
	modbusRequestAndResponseStatusValues resultDispatch = modbusRequestAndResponseStatusValues::preProcessing;
	modbusRequestAndResponse responseDispatch;
	modbusRequestAndResponseStatusValues resultAddToPayload = modbusRequestAndResponseStatusValues::addedToPayload;

	char stateAddition[256] = ""; // 256 should cover individual additions to be added to the payload.
	char topicResponse[100] = ""; // 100 should cover a topic name


	// Variables for new JSON parser
	int iSegNameCounter;
	int iSegValueCounter;
	int iPairNameCounter;
	int iPairValueCounter;
	int iCleanCounter;
	int iCounter;

	// All are emptied on creation as new arrays will just tend to have garbage in which would be recognised as actual content.
	char pairNameRaw[32] = "";
	char pairNameClean[32] = "";
	char pairValueRaw[32] = "";
	char pairValueClean[32] = "";
	char registerAddress[32] = "";
	char dataBytes[32] = "";
	char value[32] = "";
	char watts[32] = "";
	char duration[32] = "";
	char socPercent[32] = "";
	char startPos[32] = "";
	char endPos[32] = "";
	char seq[32] = "";		// Optional on set commands, echoed back so the sender can match the response
	char powerOnly[32] = "";	// Optional on set/charge and set/discharge, 1 lets a power change skip the rest of the block

	// Convert incoming values into native data types for sending
	uint16_t registerAddressConverted;
	uint16_t singleRegisterValueConverted;
	uint32_t dataRegisterValueConverted;
	uint32_t chargeDischargeWattsConverted;
	uint32_t durationSecondsConverted;
	uint16_t batterySocPercentConverted;
	uint16_t startPosConverted;
	uint16_t endPosConverted;

	uint8_t registerCount;

	// A reply's data bytes as base ten, up to 58 of them as "255," each
	char rawDataForPayload[(MAX_FRAME_SIZE_ZERO_INDEXED - MODBUS_READ_REPLY_OVERHEAD) * 4] = "";
	char mqttIncomingPayload[MQTT_REQUEST_PAYLOAD_MAX + 1] = "";

	mqttSubscriptions subScription = mqttSubscriptions::unknown;

	// Build the response in our own buffer, a schedule may be part way through the main one
	char* interruptedPayload = _mqttPayload;
	_mqttPayload = _commandPayload;

	// Start by clearing out the payload
	emptyPayload();


#ifdef DEBUG
	sprintf(_debugOutput, "Topic: %s", topic);
	Serial.println(_debugOutput);
#endif


	// Get the payload
	for (int i = 0; i < length; i++)
	{
		mqttIncomingPayload[i] = message[i];
	}
#ifdef DEBUG
	Serial.println("Payload:");
	Serial.println(mqttIncomingPayload);
#endif


	// Get an easy to use subScription type for later
	if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_READ_HANDLED_REGISTER) == 0)
	{
		subScription = mqttSubscriptions::readHandledRegister;
		strcpy(topicResponse, DEVICE_NAME MQTT_MES_RESPONSE_READ_HANDLED_REGISTER);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_READ_RAW_REGISTER) == 0)
	{
		subScription = mqttSubscriptions::readRawRegister;
		strcpy(topicResponse, DEVICE_NAME MQTT_MES_RESPONSE_READ_RAW_REGISTER);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_WRITE_RAW_SINGLE_REGISTER) == 0)
	{
		subScription = mqttSubscriptions::writeRawSingleRegister;
		strcpy(topicResponse, DEVICE_NAME MQTT_MES_RESPONSE_WRITE_RAW_SINGLE_REGISTER);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_WRITE_RAW_DATA_REGISTER) == 0)
	{
		subScription = mqttSubscriptions::writeRawDataRegister;
		strcpy(topicResponse, DEVICE_NAME MQTT_MES_RESPONSE_WRITE_RAW_DATA_REGISTER);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_READ_HANDLED_REGISTER_ALL) == 0)
	{
		subScription = mqttSubscriptions::readHandledRegisterAll;
		strcpy(topicResponse, DEVICE_NAME MQTT_SUB_RESPONSE_READ_HANDLED_REGISTER_ALL);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_SET_CHARGE) == 0)
	{
		subScription = mqttSubscriptions::setCharge;
		strcpy(topicResponse, DEVICE_NAME MQTT_SUB_RESPONSE_SET_CHARGE);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_SET_DISCHARGE) == 0)
	{
		subScription = mqttSubscriptions::setDischarge;
		strcpy(topicResponse, DEVICE_NAME MQTT_SUB_RESPONSE_SET_DISCHARGE);
	}
	else if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_SET_NORMAL) == 0)
	{
		subScription = mqttSubscriptions::setNormal;
		strcpy(topicResponse, DEVICE_NAME MQTT_SUB_RESPONSE_SET_NORMAL);
	}
	else
	{
		result = modbusRequestAndResponseStatusValues::notValidIncomingTopic;
	}

	if (result == modbusRequestAndResponseStatusValues::preProcessing)
	{
		if (length == 0)
		{
			// We won't be doing anything if no payload
			result = modbusRequestAndResponseStatusValues::noMQTTPayload;
			strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_NO_MQTT_PAYLOAD_MQTT_DESC);
		}
	}

	if (result == modbusRequestAndResponseStatusValues::preProcessing)
	{
		// Rudimentary JSON parser here, saves on using a library
		// Go through character by character
		for (iCounter = 0; iCounter < length; iCounter++)
		{
			// Find a colon
			if (mqttIncomingPayload[iCounter] == ':')
			{
				// Everything to left is name until reached the start, a comma or a left brace.
				for (iSegNameCounter = iCounter - 1; iSegNameCounter >= 0; iSegNameCounter--)
				{
					if (mqttIncomingPayload[iSegNameCounter] == ',' || mqttIncomingPayload[iSegNameCounter] == '{')
					{
						iSegNameCounter++;
						break;
					}
				}
				if (iSegNameCounter < 0)
				{
					// If went beyond the start, correct
					iSegNameCounter = 0;
				}

				// Everything to right is value until reached the end, a comma or a right brace.
				for (iSegValueCounter = iCounter + 1; iSegValueCounter < length; iSegValueCounter++)
				{
					if (mqttIncomingPayload[iSegValueCounter] == ',' || mqttIncomingPayload[iSegValueCounter] == '}')
					{
						iSegValueCounter--;
						break;
					}
				}
				// Correct if went beyond the end
				if (iSegValueCounter >= length)
				{
					// If went beyond end, correct
					iSegValueCounter = length - 1;
				}

				// A name or value too long for its buffer is rejected, not cut, so it never parses as something else
				if (iCounter - iSegNameCounter > (int)sizeof(pairNameRaw) - 1 || iSegValueCounter - iCounter > (int)sizeof(pairValueRaw) - 1)
				{
					result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
					strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
					break;
				}

				// Segment name is now from the following character until before the colon
				iPairNameCounter = 0;
				for (int x = iSegNameCounter; x < iCounter; x++)
				{
					pairNameRaw[iPairNameCounter] = mqttIncomingPayload[x];
					iPairNameCounter++;
				}
				pairNameRaw[iPairNameCounter] = '\0';

				// Segment value is now from the after the colon until the found character
				iPairValueCounter = 0;
				for (int x = iCounter + 1; x <= iSegValueCounter; x++)
				{
					pairValueRaw[iPairValueCounter] = mqttIncomingPayload[x];
					iPairValueCounter++;
				}
				pairValueRaw[iPairValueCounter] = '\0';


				// Transfer to a cleansed copy, without unwanted chars

				iPairNameCounter = 0;
				iCleanCounter = 0;
				while (pairNameRaw[iCleanCounter] != 0)
				{
					// Allow alpha numeric, upper case and lower case
					if ((pairNameRaw[iCleanCounter] >= 'a' && pairNameRaw[iCleanCounter] <= 'z') || (pairNameRaw[iCleanCounter] >= 'A' && pairNameRaw[iCleanCounter] <= 'Z') || (pairNameRaw[iCleanCounter] >= '0' && pairNameRaw[iCleanCounter] <= '9'))
					{
						// Transfer Over
						pairNameClean[iPairNameCounter] = pairNameRaw[iCleanCounter];
						iPairNameCounter++;
					}
					iCleanCounter++;
				}
				pairNameClean[iPairNameCounter] = '\0';



				iPairValueCounter = 0;
				iCleanCounter = 0;
				while (pairValueRaw[iCleanCounter] != 0)
				{
					// Allow a minus, x (for hex), and 0-9, and a-f A-F for hex
					if ((pairValueRaw[iCleanCounter] == '-' || pairValueRaw[iCleanCounter] == 'x' || (pairValueRaw[iCleanCounter] >= '0' && pairValueRaw[iCleanCounter] <= '9') || (pairValueRaw[iCleanCounter] >= 'A' && pairValueRaw[iCleanCounter] <= 'F')) || (pairValueRaw[iCleanCounter] >= 'a' && pairValueRaw[iCleanCounter] <= 'f'))
					{
						// Transfer Over
						pairValueClean[iPairValueCounter] = pairValueRaw[iCleanCounter];
						iPairValueCounter++;
					}
					iCleanCounter++;
				}
				pairValueClean[iPairValueCounter] = '\0';


#ifdef DEBUG
				sprintf(_debugOutput, "Got a cleaned JSON parameter of '%s', value '%s'", pairNameClean, pairValueClean);
				Serial.println(_debugOutput);
#endif

				if (strcmp(pairNameClean, "registerAddress") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as registerAddress");
					Serial.println(_debugOutput);
#endif
					strcpy(registerAddress, pairValueClean);
				}
				else if (strcmp(pairNameClean, "dataBytes") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as dataBytes");
					Serial.println(_debugOutput);
#endif
					strcpy(dataBytes, pairValueClean);
				}
				else if (strcmp(pairNameClean, "value") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as value");
					Serial.println(_debugOutput);
#endif
					strcpy(value, pairValueClean);
				}
				else if (strcmp(pairNameClean, "watts") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as watts");
					Serial.println(_debugOutput);
#endif
					strcpy(watts, pairValueClean);
				}
				else if (strcmp(pairNameClean, "duration") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as duration");
					Serial.println(_debugOutput);
#endif
					strcpy(duration, pairValueClean);
				}
				else if (strcmp(pairNameClean, "socPercent") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as socPercent");
					Serial.println(_debugOutput);
#endif
					strcpy(socPercent, pairValueClean);
				}
				else if (strcmp(pairNameClean, "start") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as start");
					Serial.println(_debugOutput);
#endif
					strcpy(startPos, pairValueClean);
				}
				else if (strcmp(pairNameClean, "end") == 0)
				{
#ifdef DEBUG
					sprintf(_debugOutput, "This was handled as end");
					Serial.println(_debugOutput);
#endif
					strcpy(endPos, pairValueClean);
				}
				else if (strcmp(pairNameClean, "seq") == 0)
				{
					strcpy(seq, pairValueClean);
				}
				else if (strcmp(pairNameClean, "powerOnly") == 0)
				{
					strcpy(powerOnly, pairValueClean);
				}
			}
		}
	}


	// A raw write may change the dispatch block, and normal ends it: the next charge or discharge writes it all
	if (subScription == mqttSubscriptions::writeRawSingleRegister || subScription == mqttSubscriptions::writeRawDataRegister || subScription == mqttSubscriptions::setNormal)
	{
		_activeDispatch.valid = false;
	}

	// A set request means Home Assistant is up, even if its birth was missed. Its command replaces the idle hold.
	if (subScription == mqttSubscriptions::setCharge || subScription == mqttSubscriptions::setDischarge || subScription == mqttSubscriptions::setNormal)
	{
		idleHoldCommand(_idleHold);
	}

	// Carry on?
	if (result == modbusRequestAndResponseStatusValues::preProcessing)
	{
		if (subScription == mqttSubscriptions::readHandledRegister)
		{
			// Check if registerAddress found
			if (!*registerAddress)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to readHandledRegister without a registerAddress!");
				Serial.println(_debugOutput);
#endif
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				// Convert string to a number using base 16.
				registerAddressConverted = strtoul(registerAddress, NULL, 16);

				result = _registerHandler->readHandledRegister(registerAddressConverted, &response);
			}
		}
		else if (subScription == mqttSubscriptions::readRawRegister)
		{
			if (!*registerAddress || !*dataBytes)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to readRawRegister without a registerAddress or dataBytes!");
				Serial.println(_debugOutput);
#endif
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else if (!readRequestFits(strtoul(dataBytes, NULL, 10) / 2, MAX_FRAME_SIZE_ZERO_INDEXED))
			{
				// The reply would not fit the receive buffer
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				registerAddressConverted = strtoul(registerAddress, NULL, 16);

				// Convert string to number using base ten
				registerCount = strtoul(dataBytes, NULL, 10);
				registerCount = registerCount / 2;
				response.registerCount = registerCount;

				result = _registerHandler->readRawRegister(registerAddressConverted, &response);
			}
		}
		else if (subScription == mqttSubscriptions::writeRawSingleRegister)
		{
			if (!*registerAddress || !*value)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to writeRawSingleRegister without a registerAddress or value!");
				Serial.println(_debugOutput);
#endif
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				registerAddressConverted = strtoul(registerAddress, NULL, 16);
				singleRegisterValueConverted = strtoul(value, NULL, 10);

				result = _registerHandler->writeRawSingleRegister(registerAddressConverted, singleRegisterValueConverted, &response);
			}
		}
		else if (subScription == mqttSubscriptions::writeRawDataRegister)
		{
			if (!*registerAddress || !*dataBytes || !*value)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to writeRawDataRegister without a registerAddress, dataBytes or value!");
				Serial.println(_debugOutput);
#endif
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				registerAddressConverted = strtoul(registerAddress, NULL, 16);
				dataRegisterValueConverted = strtoul(value, NULL, 10);
				registerCount = strtoul(dataBytes, NULL, 10);
				registerCount = registerCount / 2;
				response.registerCount = registerCount;

				result = _registerHandler->writeRawDataRegister(registerAddressConverted, dataRegisterValueConverted, &response);
			}
		}
		else if (subScription == mqttSubscriptions::readHandledRegisterAll)
		{
			if(!*startPos || !*endPos)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to readHandledRegisterAll without a start or end!");
				Serial.println(_debugOutput);
#endif
				result = modbusRequestAndResponseStatusValues::invalidMQTTPayload;
				strcpy(response.statusMqttMessage, MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				startPosConverted = strtoul(startPos, NULL, 10);
				endPosConverted = strtoul(endPos, NULL, 10);

				// Despite a start and end provided, ensure not above the array size
				int numberOfRegisters = sizeof(_mqttAllHandledRegisters) / sizeof(_mqttAllHandledRegisters[0]);
				uint16_t minPosition = startPosConverted;
				uint16_t maxPosition = endPosConverted > numberOfRegisters - 1 ? numberOfRegisters - 1 : endPosConverted;

				resultAddToPayload = addToPayload("{\r\n");
				if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
				{
					for (int l = minPosition; l <= maxPosition; l++)
					{
						result = addStateInfo(_mqttAllHandledRegisters[l], l < maxPosition, resultAddToPayload);
						if (resultAddToPayload == modbusRequestAndResponseStatusValues::payloadExceededCapacity)
						{
							// If the response to addStateInfo is payload exceeded get out, We will permit failing registers to carry on and try the next one.
							break;
						}
					}
					if (resultAddToPayload != modbusRequestAndResponseStatusValues::payloadExceededCapacity)
					{
						// Footer, if we haven't already bombed out with payload issues
						resultAddToPayload = addToPayload("}");
					}
				}
			}
		}
		else if ((subScription == mqttSubscriptions::setCharge) || (subScription == mqttSubscriptions::setDischarge))
		{
			// Handles dispatch mode for charge and discharge
			// Code is fundamentally the same, just a different charge power.
			int multiplier = 1;
			bool powerOnlyWrite = false;

			//if (strcmp(topic, DEVICE_NAME MQTT_SUB_REQUEST_SET_CHARGE) == 0)
			if (subScription == mqttSubscriptions::setCharge)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Multiplier for setCharge will be -1");
				Serial.println(_debugOutput);
#endif
				multiplier = -1;
			}
			else
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Multiplier for setDischarge will be 1");
				Serial.println(_debugOutput);
#endif
				multiplier = 1;
			}

			if (!*watts || !*socPercent || !*duration)
			{
#ifdef DEBUG
				sprintf(_debugOutput, "Trying to setCharge or setDischarge without a watts, socPercent or duration!");
				Serial.println(_debugOutput);
#endif
				// Nothing is written. The refusal carries the seq like any set response, so the sender fails the command at once.
				sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"missing\"", MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else if (!dispatchRequestValid(strtoul(watts, NULL, 10), strtoul(socPercent, NULL, 10)))
			{
				// Out of range, so nothing is written. A negative number reads as a huge one and is refused too.
				sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"range\"", MODBUS_REQUEST_AND_RESPONSE_INVALID_MQTT_PAYLOAD_MQTT_DESC);
			}
			else
			{
				chargeDischargeWattsConverted = strtoul(watts, NULL, 10);
				batterySocPercentConverted = strtoul(socPercent, NULL, 10);
				durationSecondsConverted = strtoul(duration, NULL, 10);

#ifdef DEBUG
				sprintf(_debugOutput, "Base 10 type-cast values for watts, socPercent and duration are %d, %d and %d respectively", chargeDischargeWattsConverted, batterySocPercentConverted, durationSecondsConverted);
				Serial.println(_debugOutput);
#endif
				// Adjust
				// Charge < 32000, discharge > 32000
				chargeDischargeWattsConverted = 32000 + (chargeDischargeWattsConverted * multiplier);
				batterySocPercentConverted = dispatchSocFromPercent(batterySocPercentConverted);

#ifdef DEBUG
				sprintf(_debugOutput, "And adjusted values for watts and socPercent are %d and %d respectively", chargeDischargeWattsConverted, batterySocPercentConverted);
				Serial.println(_debugOutput);
#endif

				bool charge = subScription == mqttSubscriptions::setCharge;
				int blockFrames = 1;
				uint32_t writeStartedMs = millis();
				DispatchBlock running = _activeDispatch;
				powerOnlyWrite = dispatchPowerOnly(strcmp(powerOnly, "1") == 0, running, charge, batterySocPercentConverted, durationSecondsConverted, writeStartedMs);

				// Cleared until this write is acknowledged, so after any failure the next command writes the whole block
				_activeDispatch.valid = false;

				if (powerOnlyWrite)
				{
					// The running block already has this kind, SOC and duration. One write changes the power, the lease runs on.
					responseDispatch.registerCount = 2;
					resultDispatch = _registerHandler->writeRawDataRegister(REG_DISPATCH_RW_ACTIVE_POWER_1, chargeDischargeWattsConverted, &responseDispatch);
					if (resultDispatch != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
					{
						sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"REG_DISPATCH_RW_ACTIVE_POWER_1\"", responseDispatch.statusMqttMessage);
					}
					else
					{
						_activeDispatch = running;
					}
				}
				else
				{
					const char* failedRegister = NULL;

					resultDispatch = writeFullDispatchBlock(charge, chargeDischargeWattsConverted, batterySocPercentConverted, durationSecondsConverted, writeStartedMs, &responseDispatch, &failedRegister, &blockFrames);
					if (failedRegister != NULL)
					{
						sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"%s\"", responseDispatch.statusMqttMessage, failedRegister);
					}
				}

				if (_activeDispatch.valid)
				{
					if (charge)
					{
						sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"\"", MODBUS_REQUEST_AND_RESPONSE_SET_CHARGE_SUCCESS_MQTT_DESC);
						result = modbusRequestAndResponseStatusValues::setChargeSuccess;
					}
					else
					{
						sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"\"", MODBUS_REQUEST_AND_RESPONSE_SET_DISCHARGE_SUCCESS_MQTT_DESC);
						result = modbusRequestAndResponseStatusValues::setDischargeSuccess;
					}
				}
				if (powerOnlyWrite)
				{
					strcat(stateAddition, ",\r\n    \"write\": \"power\"");
				}
				else
				{
					sprintf(stateAddition + strlen(stateAddition), ",\r\n    \"write\": \"block\",\r\n    \"frames\": %d", blockFrames);
				}
			}
			resultAddToPayload = addToPayload(stateAddition);
			resultAddToPayload = closeSetResponse(seq);
		}

		else if (subScription == mqttSubscriptions::setNormal)
		{
			// Turns off dispatch mode for normal
			resultDispatch = writeDispatchStop(&responseDispatch);
			if (resultDispatch != modbusRequestAndResponseStatusValues::writeDataRegisterSuccess)
			{
				sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"REG_DISPATCH_RW_DISPATCH_START\"", responseDispatch.statusMqttMessage);
			}
			else
			{
				sprintf(stateAddition, "{\r\n    \"responseStatus\": \"%s\",\r\n    \"failureDetail\": \"\"", MODBUS_REQUEST_AND_RESPONSE_SET_NORMAL_SUCCESS_MQTT_DESC);
				result = modbusRequestAndResponseStatusValues::setNormalSuccess;
			}
			resultAddToPayload = addToPayload(stateAddition);
			resultAddToPayload = closeSetResponse(seq);
		}

	}



	// Set Charge/Discharge/Normal/ReadAll do their own payload constructions.
	// For anything else, construct a standard response based on what we know from the request and the result.
	if (result == modbusRequestAndResponseStatusValues::invalidMQTTPayload || result == modbusRequestAndResponseStatusValues::noMQTTPayload || subScription == mqttSubscriptions::readHandledRegister || subScription == mqttSubscriptions::readRawRegister || subScription == mqttSubscriptions::writeRawDataRegister || subScription == mqttSubscriptions::writeRawSingleRegister)
	{
		// Construct a response based on what we know from above
		resultAddToPayload = addToPayload("{\r\n");
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
		{
			sprintf(stateAddition, "    \"responseStatus\": \"%s\",\r\n", response.statusMqttMessage);
			resultAddToPayload = addToPayload(stateAddition);
		}

		// Providing we had a payload and it was valid, we can at least send the registerAdress back to give the user some context
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result != modbusRequestAndResponseStatusValues::noMQTTPayload && result != modbusRequestAndResponseStatusValues::invalidMQTTPayload))
		{
			sprintf(stateAddition, "    \"registerAddress\": \"%s\",\r\n", registerAddress);
			resultAddToPayload = addToPayload(stateAddition);
		}

		// If some kind of result came back from the Alpha (even a slave error) we can give the function code
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result == modbusRequestAndResponseStatusValues::writeDataRegisterSuccess || result == modbusRequestAndResponseStatusValues::slaveError || result == modbusRequestAndResponseStatusValues::writeSingleRegisterSuccess || result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess))
		{
			sprintf(stateAddition, "    \"functionCode\": %d,\r\n", response.functionCode);
			resultAddToPayload = addToPayload(stateAddition);
		}

		// Content returned by a Read Handled request
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess && subScription == mqttSubscriptions::readHandledRegister))
		{
			sprintf(stateAddition, "    \"registerName\": \"%s\",\r\n", response.mqttName);
			resultAddToPayload = addToPayload(stateAddition);
			if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
			{
				sprintf(stateAddition, "    \"dataType\": \"%s\",\r\n", response.returnDataTypeDesc);
				resultAddToPayload = addToPayload(stateAddition);
			}
			if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
			{
				switch (response.returnDataType)
				{
				case modbusReturnDataType::character:
				{
					sprintf(stateAddition, "    \"dataValue\": \"%s\",\r\n", response.characterValue);
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				case modbusReturnDataType::signedInt:
				{
					sprintf(stateAddition, "    \"dataValue\": %d,\r\n", response.signedIntValue);
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				case modbusReturnDataType::unsignedInt:
				{
					sprintf(stateAddition, "    \"dataValue\": %u,\r\n", response.unsignedIntValue);
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				case modbusReturnDataType::signedShort:
				{
					sprintf(stateAddition, "    \"dataValue\": %d,\r\n", response.signedShortValue);
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				case modbusReturnDataType::unsignedShort:
				{
					sprintf(stateAddition, "    \"dataValue\": %u,\r\n", response.unsignedShortValue);
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				default:
				{
					sprintf(stateAddition, "    \"dataValue\": \"\",\r\n");
					resultAddToPayload = addToPayload(stateAddition);
					break;
				}
				}
			}
			if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload)
			{
				bool addQuote = (response.returnDataType == modbusReturnDataType::character || response.hasLookup);
				sprintf(stateAddition, "    \"formattedDataValue\": %s%s%s,\r\n", addQuote ? "\"" : "", response.dataValueFormatted, addQuote ? "\"" : "");
				resultAddToPayload = addToPayload(stateAddition);
			}

		}


		// Slave error can provide the code back
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result == modbusRequestAndResponseStatusValues::slaveError))
		{
			sprintf(stateAddition, "    \"slaveErrorCode\": %d,\r\n", response.data[0]);
			resultAddToPayload = addToPayload(stateAddition);
		}

		// Again, if some kind of result came back we can expose the raw data bytes for the user to process if they want
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result == modbusRequestAndResponseStatusValues::writeDataRegisterSuccess || result == modbusRequestAndResponseStatusValues::slaveError || result == modbusRequestAndResponseStatusValues::writeSingleRegisterSuccess || result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess))
		{
			sprintf(stateAddition, "    \"rawDataSize\": %u,\r\n", response.dataSize);
			resultAddToPayload = addToPayload(stateAddition);
		}
		if (resultAddToPayload == modbusRequestAndResponseStatusValues::addedToPayload && (result == modbusRequestAndResponseStatusValues::writeDataRegisterSuccess || result == modbusRequestAndResponseStatusValues::slaveError || result == modbusRequestAndResponseStatusValues::writeSingleRegisterSuccess || result == modbusRequestAndResponseStatusValues::readDataRegisterSuccess))
		{
			rawDataList(rawDataForPayload, sizeof(rawDataForPayload), response.data, response.dataSize);
			snprintf(stateAddition, sizeof(stateAddition), "    \"rawData\": [%s],\r\n", rawDataForPayload);
			resultAddToPayload = addToPayload(stateAddition);
		}
		// Horrible, however it sorts out needing to worry about commas from any of the above statements and is little overhead.
		if (resultAddToPayload)
		{
			sprintf(stateAddition, "    \"end\": \"true\"\r\n}");
			resultAddToPayload = addToPayload(stateAddition);
		}

	}

	if (subScription != mqttSubscriptions::unknown && result != modbusRequestAndResponseStatusValues::notValidIncomingTopic)
	{
		sendMqtt(topicResponse);
	}

	emptyPayload();
	_mqttPayload = interruptedPayload;
	return;
}


/*
sendMqtt

Sends whatever is in the modular level payload to the specified topic.
*/
void sendMqtt(char *topic)
{
	// A stalled broker holds a publish for up to MQTT_CLIENT_TIMEOUT
	feedWatchdog();

	// Attempt a send
	if (!_mqtt.publish(topic, _mqttPayload))
	{
#ifdef DEBUG
		sprintf(_debugOutput, "MQTT publish failed to %s", topic);
		Serial.println(_debugOutput);
		Serial.println(_mqttPayload);
#endif
	}

	// Empty payload for next use.
	emptyPayload();
	return;
}

/*
emptyPayload

Clears every char so end of string can be easily found
*/
void emptyPayload()
{
	for (int i = 0; i < _maxPayloadSize; i++)
	{
		_mqttPayload[i] = '\0';
	}
}