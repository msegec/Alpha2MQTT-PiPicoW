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

// The longest dispatch lease a request may ask for. The package scripts offer up to a day; the controller sends
// 60 to 600 s. What the inverter does with 0 or a longer lease is unknown, so neither is written.
#define DISPATCH_DURATION_MAX_S 86400

// The active power register is 32000 plus the discharge watts or minus the charge watts, so a charge over
// 32000 W would wrap below zero. A SOC above 105, the last step past full, is no target. Checked before any
// is narrowed.
inline bool dispatchRequestValid(unsigned long watts, unsigned long socPercent, unsigned long durationS)
{
	return watts <= 32000ul && socPercent <= 105ul && durationS >= 1ul && durationS <= DISPATCH_DURATION_MAX_S;
}

// The dispatch SOC register counts 0.4 % per bit. Round up, so a charge reaches at least the target
// and a discharge stops at or above it. At the top the 0.4 % scale does not hold on a SMILE-B3: 250 stopped
// at 99.2 %, 251 at 99.6 %, and 252 is full. So above 100 a percent is a register step: 101 sends 251, 102
// sends 252 (full), 105 sends 255, which charges on until the BMS ends it, for a balance.
inline uint16_t dispatchSocFromPercent(uint16_t percent)
{
	if (percent > 100)
	{
		return (uint16_t)(150u + percent);
	}
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
// and duration and at least half of its lease left. Everything else rewrites the whole block, which
// renews the lease, so the inverter still falls back to normal within one duration of the last full write.
inline bool dispatchPowerOnly(bool allowed, const DispatchBlock& last, bool charge, uint16_t socRegister, uint32_t durationS, uint32_t nowMs)
{
	return allowed && last.valid && last.charge == charge && last.socRegister == socRegister && last.durationS == durationS
		&& (uint32_t)(nowMs - last.writtenMs) / 1000u < durationS / 2u;
}

// The dispatch block, registers 0x0880 to 0x0888, as one Write Data Register frame: start, active power,
// reactive power (none, 32000 is zero), mode, SOC and time. One bus transaction of about 50 ms at 9600 baud
// instead of five. The last two bytes are left for the CRC.
#define DISPATCH_BLOCK_FRAME_SIZE 27

inline void dispatchBlockFrame(uint8_t frame[DISPATCH_BLOCK_FRAME_SIZE], uint8_t slaveId, uint16_t start, uint32_t powerRegister, uint16_t mode, uint16_t socRegister, uint32_t durationS)
{
	const uint8_t head[] = { slaveId, 0x10, 0x08, 0x80, 0x00, 0x09, 18,
		(uint8_t)(start >> 8), (uint8_t)start,
		(uint8_t)(powerRegister >> 24), (uint8_t)(powerRegister >> 16), (uint8_t)(powerRegister >> 8), (uint8_t)powerRegister,
		0x00, 0x00, 0x7D, 0x00,
		(uint8_t)(mode >> 8), (uint8_t)mode,
		(uint8_t)(socRegister >> 8), (uint8_t)socRegister,
		(uint8_t)(durationS >> 24), (uint8_t)(durationS >> 16), (uint8_t)(durationS >> 8), (uint8_t)durationS,
		0x00, 0x00 };
	for (unsigned int i = 0; i < DISPATCH_BLOCK_FRAME_SIZE; i++)
	{
		frame[i] = head[i];
	}
}

#endif
