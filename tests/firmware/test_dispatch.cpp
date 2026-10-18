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

	const DispatchBlock discharging = { true, false, 25, 120, 1000 };
	expect(dispatchPowerOnly(true, discharging, false, 25, 120, 1000), "same block right after the full write");
	expect(dispatchPowerOnly(true, discharging, false, 25, 120, 40999), "39.999 s into a 120 s lease");
	expect(!dispatchPowerOnly(true, discharging, false, 25, 120, 41000), "a third of the lease gone renews it");
	expect(!dispatchPowerOnly(false, discharging, false, 25, 120, 2000), "a request that does not allow it");
	expect(!dispatchPowerOnly(true, discharging, true, 25, 120, 2000), "charge after discharge");
	expect(!dispatchPowerOnly(true, discharging, false, 26, 120, 2000), "a new SOC");
	expect(!dispatchPowerOnly(true, discharging, false, 25, 300, 2000), "a new duration");
	const DispatchBlock cleared = { false, false, 25, 120, 1000 };
	expect(!dispatchPowerOnly(true, cleared, false, 25, 120, 2000), "no block, or the last write failed");
	const DispatchBlock charging = { true, true, 250, 120, 1000 };
	expect(dispatchPowerOnly(true, charging, true, 250, 120, 2000), "same charge block");
	expect(!dispatchPowerOnly(true, charging, false, 250, 120, 2000), "discharge after charge");
	const DispatchBlock beforeWrap = { true, false, 25, 120, 0xFFFFF000u };
	expect(dispatchPowerOnly(true, beforeWrap, false, 25, 120, 0x00001000u), "8 s across a millis() wrap");
	expect(!dispatchPowerOnly(true, beforeWrap, false, 25, 120, 0x00009C40u + 0x1000u), "44 s across a millis() wrap");
	const DispatchBlock shortLease = { true, false, 25, 2, 1000 };
	expect(!dispatchPowerOnly(true, shortLease, false, 25, 2, 1000), "a lease under 3 s always rewrites");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
