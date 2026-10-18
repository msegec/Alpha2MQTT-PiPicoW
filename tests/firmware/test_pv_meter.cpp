// Host tests for PvMeter.h. Run with tests/firmware/run.
#include "PvMeter.h"
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
	expect(pvMeterZeroUnconfirmed(-77, 0), "a 0 straight after generating is held back");
	expect(pvMeterZeroUnconfirmed(2310, 0), "a 0 straight after a positive reading is held back");
	expect(pvMeterZeroUnconfirmed(-10, 0), "10 W from 0 is far enough to need confirming");
	expect(pvMeterZeroUnconfirmed(10, 0), "and so is +10 W");
	expect(!pvMeterZeroUnconfirmed(0, 0), "a second 0 in a row is published");
	expect(!pvMeterZeroUnconfirmed(-3, 0), "a 0 among night standby readings is published");
	expect(!pvMeterZeroUnconfirmed(9, 0), "9 W is close enough to 0");
	expect(!pvMeterZeroUnconfirmed(-77, -62), "a nonzero reading is always published");
	expect(!pvMeterZeroUnconfirmed(0, -62), "including the first one after a 0");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
