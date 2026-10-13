// Host tests for Dispatch.h. Run with tests/firmware/run.
#include "Dispatch.h"
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
	expect(requestPayloadFits(0), "empty payload fits");
	expect(requestPayloadFits(127), "127 bytes fit with a terminator");
	expect(!requestPayloadFits(128), "128 bytes are rejected");
	expect(!requestPayloadFits(4096), "a full MQTT buffer is rejected");

	expect(dispatchSocFromPercent(0) == 0, "0 % sends 0");
	expect(dispatchSocFromPercent(10) == 25, "10 % sends 25");
	expect(dispatchSocFromPercent(25) == 63, "25 % rounds up to 63, not 62");
	expect(dispatchSocFromPercent(1) == 3, "1 % rounds up to 3");
	expect(dispatchSocFromPercent(90) == 225, "90 % sends 225");
	expect(dispatchSocFromPercent(100) == 250, "100 % sends 250");
	for (uint16_t p = 0; p <= 100; p++)
	{
		expect(dispatchSocFromPercent(p) * 2 >= p * 5, "never below the requested percent");
		expect(dispatchSocFromPercent(p) * 2 < p * 5 + 2, "less than one step above it");
	}

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
