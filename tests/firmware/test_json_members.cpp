// Host tests for JsonMembers.h. Run with tests/firmware/run.
#include "JsonMembers.h"
#include <cstdio>
#include <cstring>
#include <string>

static int failures = 0;

static void expect(bool ok, const char* what)
{
	if (!ok)
	{
		printf("FAIL: %s\n", what);
		failures++;
	}
}

static std::string member(int i)
{
	return "    \"r" + std::to_string(i) + "\": " + std::to_string(i);
}

// Builds a schedule's payload the way sendDataFromAppropriateArray does. Read i fails where failed[i] is set.
static std::string schedule(int count, const bool* failed)
{
	std::string payload = "{\r\n";
	bool anyMember = false;

	for (int i = 0; i < count; i++)
	{
		if (!failed[i])
		{
			payload += jsonMemberSeparator(anyMember) + member(i);
		}
	}
	return payload + jsonObjectEnd(anyMember);
}

// The same for sendFastState, which ends with its stamp instead
static std::string fastState(int count, const bool* failed)
{
	std::string payload = "{\r\n";
	bool anyMember = false;

	for (int i = 0; i < count; i++)
	{
		if (!failed[i])
		{
			payload += jsonMemberSeparator(anyMember) + member(i);
		}
	}
	return payload + jsonMemberSeparator(anyMember) + "    \"seq\": 1\r\n}";
}

int main()
{
	const bool none[3] = { false, false, false };
	const bool last[3] = { false, false, true };
	const bool middle[3] = { false, true, false };
	const bool first[3] = { true, false, false };
	const bool lastTwo[3] = { false, true, true };
	const bool all[3] = { true, true, true };

	expect(schedule(3, none) == "{\r\n    \"r0\": 0,\r\n    \"r1\": 1,\r\n    \"r2\": 2\r\n}", "every read good, the bytes are unchanged");
	expect(schedule(3, last) == "{\r\n    \"r0\": 0,\r\n    \"r1\": 1\r\n}", "the last read fails");
	expect(schedule(3, middle) == "{\r\n    \"r0\": 0,\r\n    \"r2\": 2\r\n}", "a middle read fails");
	expect(schedule(3, first) == "{\r\n    \"r1\": 1,\r\n    \"r2\": 2\r\n}", "the first read fails");
	expect(schedule(3, lastTwo) == "{\r\n    \"r0\": 0\r\n}", "the last two reads fail");
	expect(schedule(3, all) == "{\r\n}", "every read fails");
	expect(schedule(1, none) == "{\r\n    \"r0\": 0\r\n}", "a list of one");

	expect(fastState(3, none) == "{\r\n    \"r0\": 0,\r\n    \"r1\": 1,\r\n    \"r2\": 2,\r\n    \"seq\": 1\r\n}", "fast state, every read good, the bytes are unchanged");
	expect(fastState(3, last) == "{\r\n    \"r0\": 0,\r\n    \"r1\": 1,\r\n    \"seq\": 1\r\n}", "fast state, the last read fails");
	expect(fastState(3, all) == "{\r\n    \"seq\": 1\r\n}", "fast state, every read fails");

	// payloadAppend: what addToPayload builds every payload with
	char buffer[8] = "{";
	size_t length = 0;

	expect(payloadAppend(buffer, sizeof(buffer), "ab", length) && strcmp(buffer, "{ab") == 0 && length == 3, "an addition that fits goes on the end");
	expect(payloadAppend(buffer, sizeof(buffer), "", length) && strcmp(buffer, "{ab") == 0 && length == 3, "an empty addition changes nothing");
	expect(payloadAppend(buffer, sizeof(buffer), "cdef", length) && strcmp(buffer, "{abcdef") == 0 && length == 7, "seven characters fill an eight byte buffer with its terminator");
	expect(!payloadAppend(buffer, sizeof(buffer), "g", length) && strcmp(buffer, "{abcdef") == 0 && length == 8, "one more is refused and the payload is unchanged");

	char small[8] = "{";
	expect(!payloadAppend(small, sizeof(small), "abcdefghij", length) && strcmp(small, "{") == 0 && length == 11, "a long addition is refused whole, never cut");

	char aliased[8] = "ab";
	expect(payloadAppend(aliased, sizeof(aliased), aliased + 1, length) && strcmp(aliased, "abb") == 0, "an addition from inside the payload is copied before it is overwritten");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
