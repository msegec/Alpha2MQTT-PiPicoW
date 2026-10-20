// Host tests for Timer.h. Run with tests/firmware/run.
#include "Timer.h"
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
	uint32_t last = 0;
	expect(!timerDue(&last, 999, 1000), "not due before the interval");
	expect(last == 0, "and lastRun is left alone");
	expect(timerDue(&last, 1000, 1000), "due at the interval");
	expect(last == 1000, "lastRun moves to the due time");

	// A run held up by 300 ms keeps the phase
	last = 1000;
	expect(timerDue(&last, 2300, 1000), "a late run is still due");
	expect(last == 2000, "the next one is due at 3000, not 3300");
	expect(!timerDue(&last, 2999, 1000), "and not before");
	expect(timerDue(&last, 3000, 1000), "but at 3000");

	// A run held up by several intervals runs once
	last = 1000;
	expect(timerDue(&last, 4500, 1000), "a run several intervals late is due");
	expect(last == 4000, "once: the missed ones are dropped");
	expect(!timerDue(&last, 4600, 1000), "no second catch up run");

	// Across the millis() wrap
	last = 0xFFFFFC18u;  // 1000 ms before the wrap
	expect(!timerDue(&last, 0xFFFFFFFFu, 10000), "not due just before the wrap");
	expect(!timerDue(&last, 8999, 10000), "not due 9999 ms on, past the wrap");
	expect(last == 0xFFFFFC18u, "lastRun is not reset by the wrap");
	expect(timerDue(&last, 9000, 10000), "due 10000 ms on, past the wrap");
	expect(last == 9000, "and the phase carries over");

	// The old test of now >= lastRun + interval fired at once when the sum wrapped
	last = 0xFFFFF000u;
	expect(!timerDue(&last, 0xFFFFF100u, 0x2000u), "no early run when lastRun + interval wraps");

	// A late run across the wrap keeps the phase
	last = 0xFFFFFF00u;
	expect(timerDue(&last, 0x00000150u, 0x100u), "late across the wrap");
	expect(last == 0x00000100u, "the phase carries over the wrap");

	last = 5;
	expect(timerDue(&last, 5, 0), "a zero interval is always due");
	expect(last == 5, "and does not divide by zero");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
