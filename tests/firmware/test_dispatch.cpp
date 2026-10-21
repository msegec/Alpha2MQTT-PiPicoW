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

	expect(dispatchRequestValid(0, 0), "0 W to 0 %");
	expect(dispatchRequestValid(32000, 100), "32000 W to 100 % is the edge");
	expect(!dispatchRequestValid(32001, 50), "a charge over 32000 W would wrap the register below 0");
	expect(dispatchRequestValid(50, 105), "105 is the last step past full");
	expect(!dispatchRequestValid(50, 106), "a SOC past 105");
	expect(!dispatchRequestValid(65636, 50), "a watts that a 16 bit cast would shrink");
	expect(!dispatchRequestValid(50, 65636), "a SOC that a 16 bit cast would shrink to 100");
	expect(!dispatchRequestValid(0xFFFFFFFBul, 50), "a negative watts parsed as unsigned");

	expect(dispatchSocFromPercent(0) == 0, "0 % sends 0");
	expect(dispatchSocFromPercent(10) == 25, "10 % sends 25");
	expect(dispatchSocFromPercent(25) == 63, "25 % rounds up to 63, not 62");
	expect(dispatchSocFromPercent(1) == 3, "1 % rounds up to 3");
	expect(dispatchSocFromPercent(90) == 225, "90 % sends 225");
	expect(dispatchSocFromPercent(100) == 250, "100 % sends 250");
	expect(dispatchSocFromPercent(101) == 251, "101 is one register step past full");
	expect(dispatchSocFromPercent(105) == 255, "105 sends 255, so the stop two bits short is past full");
	for (uint16_t p = 0; p <= 100; p++)
	{
		expect(dispatchSocFromPercent(p) * 2 >= p * 5, "never below the requested percent");
		expect(dispatchSocFromPercent(p) * 2 < p * 5 + 2, "less than one step above it");
	}

	const DispatchBlock discharging = { true, false, 25, 120, 1000 };
	expect(dispatchPowerOnly(true, discharging, false, 25, 120, 1000), "same block right after the full write");
	expect(dispatchPowerOnly(true, discharging, false, 25, 120, 60999), "59.999 s into a 120 s lease");
	expect(!dispatchPowerOnly(true, discharging, false, 25, 120, 61000), "half the lease gone renews it");
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
	expect(!dispatchPowerOnly(true, beforeWrap, false, 25, 120, 0x0000EA60u + 0x1000u), "64 s across a millis() wrap");
	const DispatchBlock shortLease = { true, false, 25, 1, 1000 };
	expect(!dispatchPowerOnly(true, shortLease, false, 25, 1, 1000), "a lease under 2 s always rewrites");

	// Discharge 150 W, SOC register 25 (10 %), 120 s: slave 0x55, function 0x10, 0x0880, 9 registers, 18 bytes.
	uint8_t frame[DISPATCH_BLOCK_FRAME_SIZE];
	dispatchBlockFrame(frame, 0x55, 1, 32150, 2, 25, 120);
	const uint8_t want[DISPATCH_BLOCK_FRAME_SIZE] = { 0x55, 0x10, 0x08, 0x80, 0x00, 0x09, 18,
		0x00, 0x01,              // 0x0880 start
		0x00, 0x00, 0x7D, 0x96,  // 0x0881 active power 32150
		0x00, 0x00, 0x7D, 0x00,  // 0x0883 reactive power 32000
		0x00, 0x02,              // 0x0885 mode
		0x00, 0x19,              // 0x0886 SOC
		0x00, 0x00, 0x00, 0x78,  // 0x0887 time 120 s
		0x00, 0x00 };            // CRC, filled by the sender
	bool same = true;
	for (unsigned int i = 0; i < DISPATCH_BLOCK_FRAME_SIZE; i++)
	{
		same = same && frame[i] == want[i];
	}
	expect(same, "the block frame is laid out register by register");
	dispatchBlockFrame(frame, 0x55, 1, 32000 - 2200, 2, 250, 14400);
	expect(frame[11] == 0x74 && frame[12] == 0x68, "a 2200 W charge is 29800");
	expect(frame[19] == 0x00 && frame[20] == 0xFA, "100 % is SOC register 250");
	expect(frame[21] == 0 && frame[22] == 0 && frame[23] == 0x38 && frame[24] == 0x40, "14400 s spans two registers");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
