// Pure helpers for the dispatch request path. No Arduino types, so tests/firmware builds them on the host.
#ifndef _Dispatch_h
#define _Dispatch_h

#include <stdint.h>

// Request JSON is short. Anything longer is rejected, not truncated, so a cut payload never parses as a different command.
#define MQTT_REQUEST_PAYLOAD_MAX 127

inline bool requestPayloadFits(unsigned int length)
{
	return length <= MQTT_REQUEST_PAYLOAD_MAX;
}

// The dispatch SOC register counts 0.4 % per bit. Round up, so a charge reaches at least the target
// and a discharge stops at or above it.
inline uint16_t dispatchSocFromPercent(uint16_t percent)
{
	return (uint16_t)((percent * 5u + 1u) / 2u);
}

#endif
