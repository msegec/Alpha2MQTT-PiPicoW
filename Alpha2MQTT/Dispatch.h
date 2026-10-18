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

// The dispatch block the Pico last wrote in full and saw acknowledged. Any failed or foreign write clears valid.
struct DispatchBlock
{
	bool valid;
	bool charge;
	uint16_t socRegister;
	uint32_t durationS;
	uint32_t writtenMs;
};

// A request that allows it rewrites only the power register while the running block has the same kind, SOC
// and duration and at least two thirds of its lease left. Everything else rewrites the whole block, which
// renews the lease, so the inverter still falls back to normal within one duration of the last full write.
inline bool dispatchPowerOnly(bool allowed, const DispatchBlock& last, bool charge, uint16_t socRegister, uint32_t durationS, uint32_t nowMs)
{
	return allowed && last.valid && last.charge == charge && last.socRegister == socRegister && last.durationS == durationS
		&& (uint32_t)(nowMs - last.writtenMs) / 1000u < durationS / 3u;
}

#endif
