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

// The will seen at 1000, so the first hold is due once HA_GONE_AFTER_MS has passed
static const uint32_t willMs = 1000;
static const uint32_t firstHoldMs = willMs + HA_GONE_AFTER_MS + 1;

// Connected at 1000 with Home Assistant's will seen then, and the first hold written and acknowledged as soon as due
static IdleHold heldAfterOffline()
{
	IdleHold h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	idleHoldDue(h, true, firstHoldMs);
	idleHoldWritten(h, hold, true, firstHoldMs);
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

	expect(HA_GONE_AFTER_MS == 180000u, "gone after 3 minutes, long enough for a planned restart");

	// Never seen a status, connected: Home Assistant is assumed present, nothing is written
	IdleHold h = {};
	expect(idleHoldDue(h, true, 1000) == none, "no status, connected: no hold");
	expect(!haGone(h), "and not gone");
	expect(idleHoldDue(h, true, 500000) == none, "still none much later");

	// Never seen a status, the broker goes away: only the disconnect rule applies, counted from the last
	// pass that saw the connection up, however many reconnect attempts fail in between
	expect(idleHoldDue(h, false, 505000) == none, "5 s after the last connected pass");
	for (uint32_t now = 510000; now <= 680000; now += 5000)
	{
		expect(idleHoldDue(h, false, now) == none, "failed reconnect attempts every 5 s, under 180 s");
	}
	expect(idleHoldDue(h, false, 680001) == hold, "over 180 s since last connected: hold due");
	expect(haGone(h), "gone");
	idleHoldWritten(h, hold, true, 680001);
	expect(idleHoldDue(h, false, 680002) == none, "written, not due again at once");

	// A reconnect in the never seen case ends it, and the hold the Pico wrote is released at once
	expect(idleHoldDue(h, true, 690000) == release, "a reconnect ends it: release due in the same pass");
	expect(!haGone(h), "not gone after the reconnect");
	idleHoldWritten(h, release, true, 690000);
	expect(idleHoldDue(h, true, 690001) == none, "released once, nothing more");
	expect(idleHoldDue(h, true, 900000) == none, "nor later");

	// Disconnected under 180 s and back: no hold, so no release either
	h = {};
	idleHoldDue(h, true, 1000);
	expect(idleHoldDue(h, false, 10000) == none, "down 9 s");
	expect(idleHoldDue(h, false, 181000) == none, "180 s since last connected is not more than 180 s");
	expect(idleHoldDue(h, true, 181001) == none, "back in time: no hold and nothing to release");
	expect(idleHoldDue(h, false, 200000) == none, "a new disconnect counts from the last connected pass");
	expect(idleHoldDue(h, false, 361001) == none, "180 s from it");
	expect(idleHoldDue(h, false, 361002) == hold, "over 180 s from it");

	// A boot without the broker counts as down from boot
	h = {};
	expect(idleHoldDue(h, false, 5000) == none, "5 s after boot, no broker yet");
	expect(idleHoldDue(h, false, 180001) == hold, "over 180 s after boot, still none");

	// Offline then online inside the window: a planned restart, nothing written
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	expect(!haGone(h), "offline is not gone at once");
	expect(idleHoldDue(h, true, willMs) == none, "no hold in the same pass");
	expect(idleHoldDue(h, true, willMs + 120000) == none, "nor 2 minutes on");
	expect(idleHoldDue(h, true, willMs + HA_GONE_AFTER_MS) == none, "180 s after the will is not more than 180 s");
	expect(!haGone(h), "still not gone");
	idleHoldStatus(h, HaStatus::online, willMs + HA_GONE_AFTER_MS);
	expect(!haGone(h), "online cancels the pending offline");
	expect(idleHoldDue(h, true, willMs + HA_GONE_AFTER_MS + 1) == none, "no hold was written, so nothing to release");
	expect(idleHoldDue(h, true, willMs + 2 * HA_GONE_AFTER_MS) == none, "the old will does not fire later");

	// Offline and the window passed: gone, the hold is written, and online ends it
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	expect(idleHoldDue(h, true, firstHoldMs) == hold, "over 180 s after the will: hold due");
	expect(haGone(h), "gone");
	idleHoldWritten(h, hold, true, firstHoldMs);
	idleHoldStatus(h, HaStatus::unknown, firstHoldMs + 100);
	expect(haGone(h), "a payload that says nothing changes nothing");
	idleHoldStatus(h, HaStatus::online, firstHoldMs + 500);
	expect(!haGone(h), "online ends it");
	expect(idleHoldDue(h, true, firstHoldMs + 500) == release, "and the hold is released in the same pass, not 60 s later");
	idleHoldWritten(h, release, true, firstHoldMs + 500);
	expect(idleHoldDue(h, true, firstHoldMs + 61000) == none, "no rewrite and no second release once back");
	idleHoldStatus(h, HaStatus::offline, firstHoldMs + 62000);
	expect(idleHoldDue(h, true, firstHoldMs + 62000) == none, "a new will starts a new window");
	expect(idleHoldDue(h, true, firstHoldMs + 62000 + HA_GONE_AFTER_MS + 1) == hold, "gone again after it");

	// A second offline inside the window does not extend it
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs + 100000);
	expect(idleHoldDue(h, true, willMs + HA_GONE_AFTER_MS) == none, "not before 180 s from the first will");
	expect(idleHoldDue(h, true, firstHoldMs) == hold, "due 180 s from the first will, not the second");
	idleHoldWritten(h, hold, true, firstHoldMs);
	idleHoldStatus(h, HaStatus::offline, firstHoldMs + 1000);
	expect(haGone(h), "an offline while gone changes nothing");
	expect(idleHoldDue(h, true, firstHoldMs + 2000) == none, "and does not bring the rewrite forward");

	// A command inside the window cancels the pending offline, with no hold written
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	idleHoldDue(h, true, willMs + 60000);
	idleHoldCommand(h);
	expect(!haGone(h), "a set command proves Home Assistant is up");
	expect(idleHoldDue(h, true, firstHoldMs) == none, "no hold when the window would have closed");
	expect(idleHoldDue(h, true, firstHoldMs + 600000) == none, "nor later, and no release either");

	// Online with no hold written by the Pico sends nothing
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::online, 1000);
	expect(idleHoldDue(h, true, 2000) == none, "a birth alone writes nothing");

	// Offline survives a disconnect and a reconnect: only online or a command ends it
	h = heldAfterOffline();
	idleHoldDue(h, false, firstHoldMs + 1000);
	expect(haGone(h), "offline and disconnected");
	expect(idleHoldDue(h, true, firstHoldMs + 2000) == none, "a reconnect does not end the offline case, and the hold is not due yet");
	expect(haGone(h), "still gone");

	// The broker drops inside the offline window: the will's clock keeps running
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	idleHoldDue(h, false, willMs + 100000);
	expect(idleHoldDue(h, false, firstHoldMs) == hold, "gone 180 s after the will, not 180 s after the broker went");
	idleHoldWritten(h, hold, true, firstHoldMs);
	expect(idleHoldDue(h, true, firstHoldMs + 1000) == none, "the reconnect does not release it: the will stands");
	expect(haGone(h), "still gone");

	// Online seen, then over 180 s down: the disconnect rule still applies
	h = {};
	idleHoldDue(h, true, 1000);
	idleHoldStatus(h, HaStatus::online, 1000);
	idleHoldDue(h, false, 2000);
	expect(idleHoldDue(h, false, 181001) == hold, "online then over 180 s down: gone");

	// Rewrite every 60 s while gone
	h = heldAfterOffline();
	expect(idleHoldDue(h, true, firstHoldMs + 28000) == none, "not at 28 s");
	expect(idleHoldDue(h, true, firstHoldMs + 59999) == none, "not at 59.999 s");
	expect(idleHoldDue(h, true, firstHoldMs + 60000) == hold, "due at 60 s, half the 120 s lease");
	idleHoldWritten(h, hold, true, firstHoldMs + 60000);
	expect(idleHoldDue(h, true, firstHoldMs + 119999) == none, "the next is 60 s after that");
	expect(idleHoldDue(h, true, firstHoldMs + 120000) == hold, "and due then");

	// A failed hold retries after a short gap, not at once and not after a full 60 s
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	expect(idleHoldDue(h, true, firstHoldMs) == hold, "first hold");
	idleHoldWritten(h, hold, false, firstHoldMs);
	expect(idleHoldDue(h, true, firstHoldMs + 1) == none, "no tight loop after a failure");
	expect(idleHoldDue(h, true, firstHoldMs + IDLE_HOLD_RETRY_MS - 1) == none, "not before the retry gap");
	expect(idleHoldDue(h, true, firstHoldMs + IDLE_HOLD_RETRY_MS) == hold, "retried after the gap");
	expect(IDLE_HOLD_RETRY_MS < IDLE_HOLD_REWRITE_MS, "the retry gap is shorter than the rewrite");
	idleHoldWritten(h, hold, true, firstHoldMs + IDLE_HOLD_RETRY_MS);
	expect(idleHoldDue(h, true, firstHoldMs + 2 * IDLE_HOLD_RETRY_MS) == none, "a good write goes back to 60 s");
	expect(idleHoldDue(h, true, firstHoldMs + IDLE_HOLD_RETRY_MS + 60000) == hold, "60 s after the good write");

	// A hold that failed may still have landed in part, so it is released too
	h = {};
	idleHoldDue(h, true, willMs);
	idleHoldStatus(h, HaStatus::offline, willMs);
	idleHoldDue(h, true, firstHoldMs);
	idleHoldWritten(h, hold, false, firstHoldMs);
	idleHoldStatus(h, HaStatus::online, firstHoldMs + 1);
	expect(idleHoldDue(h, true, firstHoldMs + 1) == release, "a failed hold is released when Home Assistant is back");

	// A failed release retries after the gap until it lands
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online, firstHoldMs + 1000);
	expect(idleHoldDue(h, true, firstHoldMs + 1000) == release, "release due");
	idleHoldWritten(h, release, false, firstHoldMs + 1000);
	expect(idleHoldDue(h, true, firstHoldMs + 1001) == none, "no tight loop after a failed release");
	expect(idleHoldDue(h, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS - 1) == none, "not before the retry gap");
	expect(idleHoldDue(h, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS) == release, "retried after the gap");
	idleHoldWritten(h, release, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS);
	expect(idleHoldDue(h, true, firstHoldMs + 1000 + 3 * IDLE_HOLD_RETRY_MS) == none, "landed: no more");

	// A failed release stops once Home Assistant sends a command: that command replaced the hold
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online, firstHoldMs + 1000);
	idleHoldDue(h, true, firstHoldMs + 1000);
	idleHoldWritten(h, release, false, firstHoldMs + 1000);
	idleHoldCommand(h);
	expect(idleHoldDue(h, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS) == none, "no release over the command");

	// A new will before a failed release lands: the hold waits for its window like any other
	h = heldAfterOffline();
	idleHoldStatus(h, HaStatus::online, firstHoldMs + 1000);
	idleHoldDue(h, true, firstHoldMs + 1000);
	idleHoldWritten(h, release, false, firstHoldMs + 1000);
	idleHoldStatus(h, HaStatus::offline, firstHoldMs + 2000);
	expect(idleHoldDue(h, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS) == release, "the release still retries inside the window");
	idleHoldWritten(h, release, true, firstHoldMs + 1000 + IDLE_HOLD_RETRY_MS);
	expect(idleHoldDue(h, true, firstHoldMs + 2000 + HA_GONE_AFTER_MS + 1) == hold, "then the hold once the window closes");

	// A missed birth: offline seen and gone, the Pico off the broker while Home Assistant restarts and says
	// online, then the controller's command arrives. It ends the gone state and is never overwritten by the hold.
	h = heldAfterOffline();
	idleHoldDue(h, false, firstHoldMs + 1000);
	idleHoldDue(h, true, firstHoldMs + 20000);
	expect(haGone(h), "online was missed: still latched offline");
	idleHoldCommand(h);
	expect(!haGone(h), "a set command proves Home Assistant is up");
	expect(idleHoldDue(h, true, firstHoldMs + 20001) == none, "no hold over the command");
	expect(idleHoldDue(h, true, firstHoldMs + 60000) == none, "nor at the 60 s rewrite");
	expect(idleHoldDue(h, true, firstHoldMs + 600000) == none, "nor ever after, and no release either");

	// A command also ends a disconnect latch at once
	h = {};
	idleHoldDue(h, false, 180001);
	expect(haGone(h), "gone from boot");
	idleHoldCommand(h);
	expect(!haGone(h), "a command ends it");

	// The hold lease is twice the rewrite, so one late rewrite does not let it lapse
	expect(IDLE_HOLD_DURATION_S * 1000u == 2u * IDLE_HOLD_REWRITE_MS, "rewrite at half the lease");

	// Across the millis() wrap: the last connected pass just before it
	h = {};
	idleHoldDue(h, true, 0xFFFFF000u);
	expect(idleHoldDue(h, false, 0x00000100u) == none, "4.3 s down across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 180000u) == none, "180 s down across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 180001u) == hold, "over 180 s down across the wrap");
	idleHoldWritten(h, hold, true, 0xFFFFF000u + 180001u);
	expect(idleHoldDue(h, false, 0xFFFFF000u + 180002u) == none, "no rewrite at once across the wrap");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 240000u) == none, "nor before 60 s");
	expect(idleHoldDue(h, false, 0xFFFFF000u + 240001u) == hold, "rewrite 60 s on across the wrap");

	// The will just before the wrap: its window is counted the same way
	h = {};
	idleHoldDue(h, true, 0xFFFFF000u);
	idleHoldStatus(h, HaStatus::offline, 0xFFFFF000u);
	expect(idleHoldDue(h, true, 0x00000100u) == none, "4.3 s after the will, across the wrap");
	expect(idleHoldDue(h, true, 0xFFFFF000u + 180000u) == none, "180 s after the will across the wrap");
	expect(idleHoldDue(h, true, 0xFFFFF000u + 180001u) == hold, "over 180 s after the will across the wrap");

	// A rewrite due just after the wrap
	h = {};
	idleHoldDue(h, true, 0xFFFF8000u - 180001u);
	idleHoldStatus(h, HaStatus::offline, 0xFFFF8000u - 180001u);
	idleHoldDue(h, true, 0xFFFF8000u);
	idleHoldWritten(h, hold, true, 0xFFFF8000u);
	expect(idleHoldDue(h, true, 0xFFFFFFFFu) == none, "not due at the wrap");
	expect(idleHoldDue(h, true, 0xFFFF8000u + 60000u) == hold, "due 60 s on, past the wrap");

	// A disconnect longer than a millis() wrap stays gone
	h = {};
	idleHoldDue(h, true, 1000);
	expect(idleHoldDue(h, false, 181001) == hold, "gone after 180 s");
	for (uint32_t now = 181001; now < 0xFFFFFFFFu - 60000u; now += 60000u)
	{
		idleHoldDue(h, false, now);
	}
	idleHoldDue(h, false, 0x00000000u);
	idleHoldDue(h, false, 1000u + 30000u);
	expect(haGone(h), "still gone when the time since last connected wraps past zero");

	// An offline longer than a millis() wrap stays gone too
	h = heldAfterOffline();
	for (uint32_t now = firstHoldMs; now < 0xFFFFFFFFu - 60000u; now += 60000u)
	{
		idleHoldDue(h, true, now);
	}
	idleHoldDue(h, true, 0x00000000u);
	idleHoldDue(h, true, willMs + 30000u);
	expect(haGone(h), "still gone when the time since the will wraps past zero");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
