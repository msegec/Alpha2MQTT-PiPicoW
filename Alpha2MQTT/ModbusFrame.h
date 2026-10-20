// Pure checks on Modbus RTU frames. No Arduino types, so tests/firmware builds them on the host.
#ifndef _ModbusFrame_h
#define _ModbusFrame_h

#include <stdint.h>

// A read reply is slave ID, function code, byte count, the data and two CRC bytes.
#define MODBUS_READ_REPLY_OVERHEAD 5

// The byte count arrives before the CRC, so it is checked against the receive buffer before any data is stored.
inline bool readReplyFits(unsigned int byteCount, unsigned int bufferSize)
{
	return byteCount + MODBUS_READ_REPLY_OVERHEAD <= bufferSize;
}

// A read of this many registers gets a reply that fits the receive buffer. A read of none is not a request.
inline bool readRequestFits(unsigned long registerCount, unsigned int bufferSize)
{
	return registerCount >= 1 && registerCount <= (bufferSize - MODBUS_READ_REPLY_OVERHEAD) / 2;
}

#endif
