// Host tests for IdleHold.h. Run with tests/firmware/run.
#include "IdleHold.h"
#include <cstdio>
#include <cstring>

static int failures = 0;

static void expect(bool ok, const char* what)
{
	if (!ok)
	{
		printf("FAIL: %s\n", what);
		failures++;
	}
}

static HaStatus parse(const char* payload)
{
	return haStatusFromPayload((const uint8_t*)payload, strlen(payload));
}

static const IdleHoldWrite none = IdleHoldWrite::none;
static const IdleHoldWrite hold = IdleHoldWrite::hold;
static const IdleHoldWrite release = IdleHoldWrite::release;

// Connected at 1000 with Home Assistant's will already seen, and the first hold written and acknowledged at 2000
static IdleHold heldAfterOffline()
{
	IdleHold h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::offline);
	idleHoldDue(h, true, 2000);
	idleHoldWritten(h, hold, true, 2000);
	return h;
}

int main()
{
	expect(parse("online") == HaStatus::online, "online is the birth");
	expect(parse("offline") == HaStatus::offline, "offline is the will");
	expect(parse("") == HaStatus::unknown, "an empty payload says nothing");
	expect(parse("off") == HaStatus::unknown, "a prefix of offline says nothing");
	expect(parse("onlinex") == HaStatus::unknown, "online with more after it says nothing");
	expect(parse("Online") == HaStatus::unknown, "the match is exact");
	expect(haStatusFromPayload((const uint8_t*)"onlinegarbage", 6) == HaStatus::online, "only length bytes are read");

	// Never seen a status, connected: Home Assistant is assumed present, nothing is written
	IdleHold h = {};
	expect(idleHoldDue(h, true, 1000) == none, "no status, connected: no hold");
	expect(!haGone(h), "and not gone");
	expect(idleHoldDue(h, true, 500000) == none, "still none much later");

	// Never seen a status, the broker goes away: only the disconnect rule applies, counted from the last
	// pass that saw the connection up, however many reconnect attempts fail in between
	expect(idleHoldDue(h, false, 505000) == none, "5 s after the last connected pass");
	for (uint32_t now = 510000; now <= 560000; now += 5000)
	{
		expect(idleHoldDue(h, false, now) == none, "failed reconnect attempts every 5 s, under 60 s");
	}
	expect(idleHoldDue(h, false, 560001) == hold, "over 60 s since last connected: hold due");
	expect(haGone(h), "gone");
	idleHoldWritten(h, hold, true, 560001);
	expect(idleHoldDue(h, false, 560002) == none, "written, not due again at once");

	// A reconnect in the never seen case ends it, and the hold the Pico wrote is released at once
	expect(idleHoldDue(h, true, 570000) == release, "a reconnect ends it: release due in the same pass");
	expect(!haGone(h), "not gone after the reconnect");
	idleHoldWritten(h, release, true, 570000);
	expect(idleHoldDue(h, true, 570001) == none, "released once, nothing more");
	expect(idleHoldDue(h, true, 700000) == none, "nor later");

	// Disconnected under 60 s and back: no hold, so no release either
	h = {};
	idleHoldDue(h, true, 1000);
	expect(idleHoldDue(h, false, 10000) == none, "down 9 s");
	expect(idleHoldDue(h, false, 61000) == none, "60 s since last connected is not more than 60 s");
	expect(idleHoldDue(h, true, 61001) == none, "back in time: no hold and nothing to release");
	expect(idleHoldDue(h, false, 80000) == none, "a new disconnect counts from the last connected pass");
	expect(idleHoldDue(h, false, 121001) == none, "60 s from it");
	expect(idleHoldDue(h, false, 121002) == hold, "over 60 s from it");

	// A boot without the broker counts as down from boot
	h = {};
	expect(idleHoldDue(h, false, 5000) == none, "5 s after boot, no broker yet");
	expect(idleHoldDue(h, false, 60001) == hold, "over 60 s after boot, still none");

	// Offline then online
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::offline);
	expect(haGone(h), "offline is gone at once");
	expect(idleHoldDue(h, true, 1000) == hold, "the first hold is due in the same pass");
	idleHoldWritten(h, hold, true, 1000);
	idleHoldStatus(h, HaStatus::unknown);
	expect(haGone(h), "a payload that says nothing changes nothing");
	idleHoldStatus(h, HaStatus::online);
	expect(!haGone(h), "online ends it");
	expect(idleHoldDue(h, true, 1500) == release, "and the hold is released in the next pass, not 60 s later");
	idleHoldWritten(h, release, true, 1500);
	expect(idleHoldDue(h, true, 62000) == none, "no rewrite and no second release once back");
	idleHoldStatus(h, HaStatus::offline);
	expect(idleHoldDue(h, true, 63000) == hold, "gone again: the hold is due at once");

	// Online with no hold written by the Pico sends nothing
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::online);
	expect(idleHoldDue(h, true, 2000) == none, "a birth alone writes nothing");

	// Offline survives a disconnect and a reconnect: only online or a command ends it
	h = heldAfterOffline();
	idleHoldDue(h, false, 3000);
	expect(haGone(h), "offline and disconnected");
	expect(idleHoldDue(h, true, 4000) == none, "a reconnect does not end the offline case, and the hold is not due yet");
	expect(haGone(h), "still gone");

	// Online seen, then over 60 s down: the disconnect rule still applies
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::online);
	idleHoldDue(h, false, 2000);
	expect(idleHoldDue(h, false, 61001) == hold, "online then over 60 s down: gone");

	// Rewrite every 60 s while gone
	h = heldAfterOffline();
	expect(idleHoldDue(h, true, 30000) == none, "not at 28 s");
	expect(idleHoldDue(h, true, 61999) == none, "not at 59.999 s");
	expect(idleHoldDue(h, true, 62000) == hold, "due at 60 s, half the 120 s lease");
	idleHoldWritten(h, hold, true, 62000);
	expect(idleHoldDue(h, true, 121999) == none, "the next is 60 s after that");
	expect(idleHoldDue(h, true, 122000) == hold, "and due then");

	// A failed hold retries after a short gap, not at once and not after a full 60 s
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::offline);
	expect(idleHoldDue(h, true, 1000) == hold, "first hold");
	idleHoldWritten(h, hold, false, 1000);
	expect(idleHoldDue(h, true, 1001) == none, "no tight loop after a failure");
	expect(idleHoldDue(h, true, 1000 + IDLE_HOLD_RETRY_MS - 1) == none, "not before the retry gap");
	expect(idleHoldDue(h, true, 1000 + IDLE_HOLD_RETRY_MS) == hold, "retried after the gap");
	expect(IDLE_HOLD_RETRY_MS < IDLE_HOLD_REWRITE_MS, "the retry gap is shorter than the rewrite");
	idleHoldWritten(h, hold, true, 1000 + IDLE_HOLD_RETRY_MS);
	expect(idleHoldDue(h, true, 1000 + 2 * IDLE_HOLD_RETRY_MS) == none, "a good write goes back to 60 s");
	expect(idleHoldDue(h, true, 1000 + IDLE_HOLD_RETRY_MS + 60000) == hold, "60 s after the good write");

	// A hold that failed may still have landed in part, so it is released too
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::offline);
	idleHoldDue(h, true, 1000);
	idleHoldWritten(h, hold, false, 1000);
	idleHoldStatus(h, HaStatus::online);
	expect(idleHoldDue(h, true, 1001) == release, "a failed hold is released when Home Assistant is back");

	// A failed release retries after the gap until it lands
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online);
	expect(idleHoldDue(h, true, 3000) == release, "release due");
	idleHoldWritten(h, release, false, 3000);
	expect(idleHoldDue(h, true, 3001) == none, "no tight loop after a failed release");
	expect(idleHoldDue(h, true, 3000 + IDLE_HOLD_RETRY_MS - 1) == none, "not before the retry gap");
	expect(idleHoldDue(h, true, 3000 + IDLE_HOLD_RETRY_MS) == release, "retried after the gap");
	idleHoldWritten(h, release, true, 3000 + IDLE_HOLD_RETRY_MS);
	expect(idleHoldDue(h, true, 3000 + 3 * IDLE_HOLD_RETRY_MS) == none, "landed: no more");

	// A failed release stops once Home Assistant sends a command: that command replaced the hold
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online);
	idleHoldDue(h, true, 3000);
	idleHoldWritten(h, release, false, 3000);
	idleHoldCommand(h);
	expect(idleHoldDue(h, true, 3000 + IDLE_HOLD_RETRY_MS) == none, "no release over the command");

	// Gone again before a failed release lands: back to the hold at once
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online);
	idleHoldDue(h, true, 3000);
	idleHoldWritten(h, release, false, 3000);
	idleHoldStatus(h, HaStatus::offline);
	expect(idleHoldDue(h, true, 3001) == hold, "the hold, not the release retry gap");

	// A missed birth: offline seen, the Pico off the broker while Home Assistant restarts and says online,
	// then the controller's command arrives. It ends the gone state and is never overwritten by the hold.
	h = heldAfterOffline();
	idleHoldDue(h, false, 3000);
	idleHoldDue(h, true, 20000);
	expect(haGone(h), "online was missed: still latched offline");
	idleHoldCommand(h);
	expect(!haGone(h), "a set command proves Home Assistant is up");
	expect(idleHoldDue(h, true, 20001) == none, "no hold over the command");
	expect(idleHoldDue(h, true, 62000) == none, "nor at the 60 s rewrite");
	expect(idleHoldDue(h, true, 600000) == none, "nor ever after, and no release either");

	// A command also ends a disconnect latch at once
	h = {};
	idleHoldDue(h, false, 60001);
	expect(haGone(h), "gone from boot");
	idleHoldCommand(h);
	expect(!haGone(h), "a command ends it");

	// The hold lease is twice the rewrite, so one late rewrite does not let it lapse
	expect(IDLE_HOLD_DURATION_S * 1000u == 2u * IDLE_HOLD_REWRITE_MS, "rewrite at half the lease");

	// Across the millis() wrap: the last connected pass just before it
	h = {};
	idleHoldDue(h, true, 0xFFFFF000u);
	expect(idleHoldDue(h, false, 0x00000100u) == none, "4.3 s down across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 60000u) == none, "60 s down across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 60001u) == hold, "over 60 s down across the wrap");
	idleHoldWritten(h, hold, true, 0xFFFFF000u + 60001u);
	expect(idleHoldDue(h, false, 0xFFFFF000u + 60002u) == none, "no rewrite at once across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 120000u) == none, "nor before 60 s");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 120001u) == hold, "rewrite 60 s on across the wrap");

	// A rewrite due just after the wrap
	h = {};
	idleHoldDue(h, true, 0xFFFF8000u);
	idleHoldStatus(h, HaStatus::offline);
	idleHoldDue(h, true, 0xFFFF8000u);
	idleHoldWritten(h, hold, true, 0xFFFF8000u);
	expect(idleHoldDue(h, true, 0xFFFFFFFFu) == none, "not due at the wrap");
	expect(idleHoldDue(h, true, 0xFFFF8000u + 60000u) == hold, "due 60 s on, past the wrap");

	// A disconnect longer than a millis() wrap stays gone
	h = {};
	idleHoldDue(h, true, 1000);
	expect(idleHoldDue(h, false, 61001) == hold, "gone after 60 s");
	for (uint32_t now = 61001; now < 0xFFFFFFFFu - 60000u; now += 60000u)
	{
		idleHoldDue(h, false, now);
	}
	idleHoldDue(h, false, 0x00000000u);
	idleHoldDue(h, false, 1000u + 30000u);
	expect(haGone(h), "still gone when the time since last connected wraps past zero");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
