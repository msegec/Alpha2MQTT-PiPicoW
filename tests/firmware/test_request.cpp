// Host tests for Request.h. Run with tests/firmware/run.
#include "Request.h"
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

static bool parse(const char* payload, RequestFields& fields)
{
	return parseRequestFields(payload, strlen(payload), fields);
}

static bool whole(const char* text, uint32_t expected)
{
	uint32_t out = 0;
	return parseWhole(text, out) && out == expected;
}

static bool notWhole(const char* text)
{
	uint32_t out = 0;
	return !parseWhole(text, out);
}

static bool hex(const char* text, uint16_t expected)
{
	uint16_t out = 0;
	return parseHex16(text, out) && out == expected;
}

static bool notHex(const char* text)
{
	uint16_t out = 0;
	return !parseHex16(text, out);
}

static bool flag(const char* text, bool expected)
{
	bool out = !expected;
	return parseFlag(text, out) && out == expected;
}

int main()
{
	RequestFields fields;

	// What the controller sends: json.dumps with no spaces, integers, powerOnly exactly 1
	expect(parse("{\"watts\":150,\"socPercent\":102,\"duration\":120,\"seq\":7,\"powerOnly\":1}", fields), "the controller's payload parses");
	expect(strcmp(fields.watts, "150") == 0, "watts");
	expect(strcmp(fields.socPercent, "102") == 0, "socPercent");
	expect(strcmp(fields.duration, "120") == 0, "duration");
	expect(strcmp(fields.seq, "7") == 0, "seq");
	expect(strcmp(fields.powerOnly, "1") == 0, "powerOnly");
	expect(parse("{\"void\":0,\"seq\":8}", fields) && strcmp(fields.seq, "8") == 0 && fields.watts[0] == 0, "set/normal from the controller");

	// What the package scripts and README examples send: spaces, quotes, line breaks
	expect(parse("{\"watts\": 3000, \"socPercent\": 100, \"duration\": 14400}", fields), "a script payload parses");
	expect(strcmp(fields.watts, "3000") == 0 && strcmp(fields.duration, "14400") == 0, "spaces around values go");
	expect(parse("{\r\n    \"registerAddress\": \"0x0880\",\r\n    \"dataBytes\": 2\r\n}", fields), "a multi line raw read parses");
	expect(strcmp(fields.registerAddress, "0x0880") == 0, "quotes around a value go");
	expect(strcmp(fields.dataBytes, "2") == 0, "the last value ends at the brace");
	expect(parse("{\"start\": 0, \"end\": 10}", fields) && strcmp(fields.start, "0") == 0 && strcmp(fields.end, "10") == 0, "start and end");
	expect(parse("{\"value\": \"25\"}", fields) && strcmp(fields.value, "25") == 0, "a quoted number keeps its digits");

	// Values are kept as sent, so a typed parse can refuse them instead of reading another number
	expect(parse("{\"watts\": 150.5}", fields) && strcmp(fields.watts, "150.5") == 0, "a point is kept, not stripped to 1505");
	expect(parse("{\"powerOnly\": true}", fields) && strcmp(fields.powerOnly, "true") == 0, "true is kept, not cleaned to e");
	expect(parse("{\"duration\": -1}", fields) && strcmp(fields.duration, "-1") == 0, "a sign is kept");

	// Unknown keys are ignored, a repeated key keeps its last value, and every parse starts empty
	expect(parse("{\"colour\": 3, \"watts\": 1, \"watts\": 2}", fields) && strcmp(fields.watts, "2") == 0, "last of a repeated key");
	expect(parse("{}", fields) && fields.watts[0] == 0 && fields.seq[0] == 0, "an empty object clears every field");

	// Too long for a field is refused, never cut to a different value
	// The limit counts the raw text between the colon and the comma or brace, spaces and quotes too
	expect(parse("{\"watts\":1234567890123456789012345678901}", fields), "a 31 character value fits");
	expect(!parse("{\"watts\":12345678901234567890123456789012}", fields), "a 32 character value is refused");
	expect(!parse("{\"watts\": \"12345678901234567890123456789\"}", fields), "31 digits plus a space and quotes is refused");
	expect(!parse("{\"wattswattswattswattswattswattsw\": 1}", fields), "a 32 character name is refused");

	expect(whole("0", 0), "0");
	expect(whole("32000", 32000), "32000");
	expect(whole("4294967295", 4294967295u), "the largest uint32");
	expect(notWhole("4294967296"), "one past uint32 is refused, not wrapped");
	expect(notWhole("99999999999999999999"), "a long number is refused");
	expect(notWhole(""), "empty");
	expect(notWhole("150.5"), "a fraction");
	expect(notWhole("150.0"), "a point even with a zero fraction");
	expect(notWhole("-1"), "a negative");
	expect(notWhole("+1"), "a sign");
	expect(notWhole("1e3"), "an exponent");
	expect(notWhole("0x10"), "hex where decimal is wanted");
	expect(notWhole("true"), "a word");
	expect(notWhole("12 3"), "digits with a gap");

	expect(hex("0x0880", 0x0880), "0x0880");
	expect(hex("0880", 0x0880), "0880 without a prefix");
	expect(hex("0X00a1", 0x00A1), "upper case prefix, lower case digits");
	expect(hex("FFFF", 0xFFFF), "FFFF");
	expect(notHex("0x10000"), "past 16 bits");
	expect(notHex(""), "empty");
	expect(notHex("0x"), "a prefix alone");
	expect(notHex("-1"), "a sign");
	expect(notHex("08g0"), "a letter past f");

	expect(flag("1", true), "1 is on");
	expect(flag("true", true), "true is on");
	expect(flag("0", false), "0 is off");
	expect(flag("false", false), "false is off");
	bool out = false;
	expect(!parseFlag("yes", out), "yes is refused");
	expect(!parseFlag("2", out), "2 is refused");
	expect(!parseFlag("", out), "empty is refused");

	// sameText runs at compile time, for the OTA_PASSWORD check
	static_assert(sameText("change-me", "change-me"), "equal text is the same");
	static_assert(!sameText("change-me", "change-me-too"), "a longer text is not the same");
	static_assert(!sameText("change", "change-me"), "a shorter text is not the same");
	static_assert(!sameText("Change-me", "change-me"), "case counts");
	expect(sameText("", ""), "two empty texts are the same");

	if (failures == 0)
	{
		printf("OK\n");
	}
	return failures == 0 ? 0 : 1;
}
