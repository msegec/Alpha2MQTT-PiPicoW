/*
Name:		RS485Handler.h
Created:	24/Aug/2022
Author:		Daniel Young

This file is part of Alpha2MQTT (A2M) which is released under GNU GENERAL PUBLIC LICENSE.
See file LICENSE or go to https://choosealicense.com/licenses/gpl-3.0/ for full license details.

Notes

Handles Modbus requests and responses in a tidy class separate from main program logic.
*/
#ifndef _RS485Handler_h
#define _RS485Handler_h

#if defined(ARDUINO) && ARDUINO >= 100
	#include "Arduino.h"
#else
	#include "WProgram.h"
#endif

#include "Definitions.h"

// UART0 is dedicated to RS485. The USB serial port remains available for flashing and debugging.

#define RS485_TX HIGH						// Transmit control pin goes high
#define RS485_RX LOW						// Receive control pin goes low
#define DEFAULT_BAUD_RATE 9600				// Just a default, Alpha2MQTT will cycle baud rates until one is found
#define RS485_QUIET_MS 10					// A gap this long after a failed exchange means the line is clear
#define RS485_DRAIN_MAX_MS 150				// About one 63 byte frame at 4800 baud

#define SERIAL_COMMUNICATION_CONTROL_PIN 2	// GP2, transmission set pin
#define RX_PIN 1							// GP1, UART0 RX
#define TX_PIN 0							// GP0, UART0 TX

class RS485Handler
{

	private:
		SerialUART* _RS485Serial;

		char* _debugOutput;
		void flushRS485();
		bool _lineDirty = false;
		modbusRequestAndResponseStatusValues listenResponse(const uint8_t request[], modbusRequestAndResponse* resp);
		bool checkForData();
		void outputFrameToSerial(bool transmit, uint8_t frame[], byte actualFrameSize);

	protected:


	public:
		RS485Handler();
		~RS485Handler();
		modbusRequestAndResponseStatusValues sendModbus(uint8_t frame[], byte actualFrameSize, modbusRequestAndResponse* resp);
		bool checkCRC(uint8_t frame[], byte actualFrameSize);
		void calcCRC(uint8_t frame[], byte actualFrameSize);
		void setDebugOutput(char* _db);
		void setBaudRate(unsigned long baudRate);
};


#endif

