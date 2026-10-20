// Host tests for ModbusFrame.h. Run with tests/firmware/run.
#include "ModbusFrame.h"
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

int main()
{
	// The firmware's receive buffer is 63 bytes
	expect(readReplyFits(2, 63), "a one register reply fits");
	expect(readReplyFits(58, 63), "58 data bytes fill the buffer exactly");
	expect(!readReplyFits(59, 63), "59 data bytes would run one past the end");
	expect(!readReplyFits(255, 63), "the largest byte count is refused");

	expect(!readRequestFits(0, 63), "a read of no registers is refused");
	expect(readRequestFits(1, 63), "one register");
	expect(readRequestFits(29, 63), "29 registers, 58 bytes, fit");
	expect(!readRequestFits(30, 63), "30 registers do not");
	expect(!readRequestFits(127, 63), "dataBytes 255 is refused");
	expect(!readRequestFits(0xFFFFFFFFul / 2, 63), "a negative dataBytes read as unsigned is refused");

	// Battery power 0x0126, one register, and grid power 0x0021, two registers. CRC bytes are not checked here.
	const uint8_t readBattery[] = { 0x55, 0x03, 0x01, 0x26, 0x00, 0x01, 0, 0 };
	const uint8_t batteryReply[] = { 0x55, 0x03, 2, 0xFF, 0x9C, 0, 0 };
	const uint8_t gridReply[] = { 0x55, 0x03, 4, 0x00, 0x00, 0x01, 0x2C, 0, 0 };
	expect(replyMatchesRequest(readBattery, batteryReply), "a read reply of two bytes for one register");
	expect(!replyMatchesRequest(readBattery, gridReply), "a late grid reply is not the battery reading");

	const uint8_t readSixty[] = { 0x55, 0x03, 0x00, 0x10, 0x00, 0x80, 0, 0 };
	const uint8_t zeroCount[] = { 0x55, 0x03, 0, 0, 0 };
	expect(!replyMatchesRequest(readSixty, zeroCount), "a count past 255 bytes never matches a one byte count");

	// Dispatch block, 0x0880, nine registers
	const uint8_t writeBlock[] = { 0x55, 0x10, 0x08, 0x80, 0x00, 0x09, 18 };
	const uint8_t blockAck[] = { 0x55, 0x10, 0x08, 0x80, 0x00, 0x09, 0, 0 };
	const uint8_t startAck[] = { 0x55, 0x10, 0x08, 0x80, 0x00, 0x01, 0, 0 };
	const uint8_t powerAck[] = { 0x55, 0x10, 0x08, 0x81, 0x00, 0x09, 0, 0 };
	expect(replyMatchesRequest(writeBlock, blockAck), "a write ack echoing address and count");
	expect(!replyMatchesRequest(writeBlock, startAck), "an ack for another register count");
	expect(!replyMatchesRequest(writeBlock, powerAck), "an ack for another address");
	expect(!replyMatchesRequest(writeBlock, batteryReply), "a read reply to a write");
	expect(!replyMatchesRequest(readBattery, blockAck), "a write ack to a read");

	const uint8_t writeSingle[] = { 0x55, 0x06, 0x08, 0x80, 0x00, 0x01, 0, 0 };
	const uint8_t singleAck[] = { 0x55, 0x06, 0x08, 0x80, 0x00, 0x01, 0, 0 };
	const uint8_t singleOther[] = { 0x55, 0x06, 0x08, 0x80, 0x00, 0x00, 0, 0 };
	expect(replyMatchesRequest(writeSingle, singleAck), "a single register ack echoes the value");
	expect(!replyMatchesRequest(writeSingle, singleOther), "and another value is someone else's");

	const uint8_t readException[] = { 0x55, 0x83, 0x02, 0, 0 };
	const uint8_t writeException[] = { 0x55, 0x90, 0x02, 0, 0 };
	const uint8_t oddCode[] = { 0x55, 0x2B, 0x01, 0, 0 };
	expect(replyMatchesRequest(readBattery, readException), "a read exception answers a read");
	expect(!replyMatchesRequest(readBattery, writeException), "a write exception does not");
	expect(replyMatchesRequest(writeBlock, writeException), "a write exception answers a write");
	expect(replyMatchesRequest(readBattery, oddCode), "an unknown code is still a slave error");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
