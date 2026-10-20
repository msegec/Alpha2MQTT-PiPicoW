// Pure rules for holding the battery idle while Home Assistant is gone. No Arduino types, so tests/firmware builds
// them on the host.
#ifndef _IdleHold_h
#define _IdleHold_h

#include <stdint.h>
#include "Timer.h"

// Home Assistant counts as gone once the broker has been away this long, since its commands cannot arrive
#define HA_GONE_AFTER_DISCONNECT_MS 60000u

// The hold is a 120 s dispatch block rewritten every 60 s, so one late rewrite does not let it lapse. A failed
// write tries again after the short gap.
#define IDLE_HOLD_DURATION_S 120u
#define IDLE_HOLD_REWRITE_MS 60000u
#define IDLE_HOLD_RETRY_MS 10000u

enum class HaStatus : uint8_t { unknown, online, offline };

// The status topic carries online (the birth) or offline (the will). Anything else says nothing. Reads only
// length bytes, so the payload needs no terminator.
inline HaStatus haStatusFromPayload(const uint8_t* payload, unsigned int length)
{
	static const char online[] = "online";
	static const char offline[] = "offline";
	const char* want = length == sizeof(online) - 1 ? online : length == sizeof(offline) - 1 ? offline : 0;

	if (want == 0)
	{
		return HaStatus::unknown;
	}
	for (unsigned int i = 0; i < length; i++)
	{
		if (payload[i] != (uint8_t)want[i])
		{
			return HaStatus::unknown;
		}
	}
	return want == online ? HaStatus::online : HaStatus::offline;
}

// What the Pico owes the inverter this pass: nothing, the idle hold, or the release of a hold it wrote
enum class IdleHoldWrite : uint8_t { none, hold, release };

struct IdleHold
{
	HaStatus status;			// The last birth or will seen, unknown until one arrives
	bool mqttGone;				// Off the broker over HA_GONE_AFTER_DISCONNECT_MS, held until a reconnect
	uint32_t lastConnectedMs;	// The last pass that saw the broker connection up, 0 from boot
	bool held;					// A hold has gone out, landed or not, and is not yet released or replaced
	bool tried;					// lastWrite went out, at lastTryMs, and lastOk says how it went
	IdleHoldWrite lastWrite;
	bool lastOk;
	uint32_t lastTryMs;
};

inline void idleHoldStatus(IdleHold& h, HaStatus status)
{
	if (status != HaStatus::unknown)
	{
		h.status = status;
	}
}

// A set/charge, set/discharge or set/normal request: Home Assistant is up, whatever the last status said, and
// its command replaces any hold, so nothing is written over it. This covers a birth sent while the Pico was off
// the broker, which would otherwise leave the will latched.
inline void idleHoldCommand(IdleHold& h)
{
	h.status = HaStatus::online;
	h.mqttGone = false;
	h.held = false;
	h.tried = false;
}

// Gone after an offline until the next online or command, or while the broker has been away over a minute.
// With no status seen, only the broker rule applies.
inline bool haGone(const IdleHold& h)
{
	return h.status == HaStatus::offline || h.mqttGone;
}

// Called once per loop pass with the connection state. While Home Assistant is gone the hold is due at once,
// then every IDLE_HOLD_REWRITE_MS. Once it is back, a hold the Pico wrote is released at once. A failed write of
// either kind is tried again after IDLE_HOLD_RETRY_MS. The broker rule counts from the last connected pass, so
// failed reconnect attempts do not restart it, and latches, so a disconnect longer than a millis() wrap stays gone.
inline IdleHoldWrite idleHoldDue(IdleHold& h, bool mqttConnected, uint32_t now)
{
	if (mqttConnected)
	{
		h.lastConnectedMs = now;
		h.mqttGone = false;
	}
	else if (now - h.lastConnectedMs > HA_GONE_AFTER_DISCONNECT_MS)
	{
		h.mqttGone = true;
	}

	IdleHoldWrite want = haGone(h) ? IdleHoldWrite::hold : h.held ? IdleHoldWrite::release : IdleHoldWrite::none;

	if (want == IdleHoldWrite::none)
	{
		return want;
	}
	return retryDue(h.tried && h.lastWrite == want, h.lastTryMs, now, h.lastOk ? IDLE_HOLD_REWRITE_MS : IDLE_HOLD_RETRY_MS) ? want : IdleHoldWrite::none;
}

inline void idleHoldWritten(IdleHold& h, IdleHoldWrite write, bool ok, uint32_t now)
{
	h.tried = true;
	h.lastWrite = write;
	h.lastOk = ok;
	h.lastTryMs = now;
	if (write == IdleHoldWrite::hold)
	{
		h.held = true;
	}
	else if (ok)
	{
		h.held = false;
	}
}

#endif
