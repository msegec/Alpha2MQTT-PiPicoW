// Pure helpers for the PV meter power read. No Arduino types, so tests/firmware builds them on the host.
#ifndef _PvMeter_h
#define _PvMeter_h

#include <stdint.h>

// Now and then the inverter answers a PV meter power read with a good frame holding exactly 0, for that
// one read, while the PV is generating. A real reading reaches 0 through small values, so a 0 straight
// after a reading at least this far from 0 is held back until the next read confirms it.
#define PV_METER_ZERO_CONFIRM_FROM_W 10

inline bool pvMeterZeroUnconfirmed(int32_t previous, int32_t value)
{
	return value == 0 && (previous >= PV_METER_ZERO_CONFIRM_FROM_W || previous <= -PV_METER_ZERO_CONFIRM_FROM_W);
}

#endif
