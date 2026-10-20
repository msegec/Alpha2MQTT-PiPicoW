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

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
