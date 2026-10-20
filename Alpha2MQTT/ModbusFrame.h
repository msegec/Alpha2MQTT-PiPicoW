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

// Modbus RTU carries no transaction ID, so a reply is matched to its request by what it echoes. A late reply to
// an earlier request has the right slave ID and a good CRC, but not this request's function, byte count or address.
// The request is at least six bytes. The reply is read past its function code only for a success, which is too.
inline bool replyMatchesRequest(const uint8_t* request, const uint8_t* reply)
{
	const uint8_t asked = request[1];
	const uint8_t got = reply[1];

	// Read Data Register 0x03, Write Single Register 0x06 and Write Data Register 0x10 succeed with their own code
	if (got == 0x03 || got == 0x06 || got == 0x10)
	{
		if (got != asked)
		{
			return false;
		}
		if (got == 0x03)
		{
			return reply[2] == 2ul * ((unsigned long)request[4] << 8 | request[5]);
		}
		// A write echoes the address and the register count, or for a single register the value
		return reply[2] == request[2] && reply[3] == request[3] && reply[4] == request[4] && reply[5] == request[5];
	}

	// An exception sets the top bit of the code it answers. Any other code is taken as an exception, as before.
	return (got & 0x80) == 0 || got == (asked | 0x80);
}

#endif
