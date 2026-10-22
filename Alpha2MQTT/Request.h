// Pure parsing of an MQTT request payload. No Arduino types, so tests/firmware builds it on the host.
#ifndef _Request_h
#define _Request_h

#include <stdint.h>
#include <string.h>

// A name or value longer than this, less one for the terminator, refuses the whole request
#define REQUEST_FIELD_SIZE 32

// The value text of each key a request topic reads, empty when the payload did not send it
struct RequestFields
{
	char registerAddress[REQUEST_FIELD_SIZE];
	char dataBytes[REQUEST_FIELD_SIZE];
	char value[REQUEST_FIELD_SIZE];
	char watts[REQUEST_FIELD_SIZE];
	char duration[REQUEST_FIELD_SIZE];
	char socPercent[REQUEST_FIELD_SIZE];
	char start[REQUEST_FIELD_SIZE];
	char end[REQUEST_FIELD_SIZE];
	char seq[REQUEST_FIELD_SIZE];
	char powerOnly[REQUEST_FIELD_SIZE];
};

inline bool requestSpace(char c)
{
	return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

// Copies text[from, to) less surrounding white space and one pair of double quotes
inline void requestTrim(const char* text, int from, int to, char* out)
{
	while (from < to && requestSpace(text[from]))
	{
		from++;
	}
	while (to > from && requestSpace(text[to - 1]))
	{
		to--;
	}
	if (to - from >= 2 && text[from] == '"' && text[to - 1] == '"')
	{
		from++;
		to--;
	}
	memcpy(out, text + from, to - from);
	out[to - from] = '\0';
}

inline char* requestField(RequestFields& fields, const char* name)
{
	struct { const char* name; char* field; } known[] = {
		{ "registerAddress", fields.registerAddress }, { "dataBytes", fields.dataBytes }, { "value", fields.value },
		{ "watts", fields.watts }, { "duration", fields.duration }, { "socPercent", fields.socPercent },
		{ "start", fields.start }, { "end", fields.end }, { "seq", fields.seq }, { "powerOnly", fields.powerOnly } };
	for (unsigned int i = 0; i < sizeof(known) / sizeof(known[0]); i++)
	{
		if (strcmp(name, known[i].name) == 0)
		{
			return known[i].field;
		}
	}
	return NULL;
}

// A flat JSON object. Each colon splits a member: the name runs back to a comma or brace, the value on to one.
// The name keeps letters and digits only. The value keeps its text, so a typed parse can refuse 150.5 or true
// instead of reading another number from what is left. Unknown names are ignored and a repeated name keeps the
// last value. False when a raw name or value is too long for its field, so it never parses as something else.
inline bool parseRequestFields(const char* payload, unsigned int length, RequestFields& fields)
{
	memset(&fields, 0, sizeof(fields));
	for (int colon = 0; colon < (int)length; colon++)
	{
		if (payload[colon] != ':')
		{
			continue;
		}
		int nameStart = colon;
		while (nameStart > 0 && payload[nameStart - 1] != ',' && payload[nameStart - 1] != '{')
		{
			nameStart--;
		}
		int valueEnd = colon + 1;
		while (valueEnd < (int)length && payload[valueEnd] != ',' && payload[valueEnd] != '}')
		{
			valueEnd++;
		}
		if (colon - nameStart > REQUEST_FIELD_SIZE - 1 || valueEnd - colon - 1 > REQUEST_FIELD_SIZE - 1)
		{
			return false;
		}

		char name[REQUEST_FIELD_SIZE];
		int n = 0;
		for (int i = nameStart; i < colon; i++)
		{
			char c = payload[i];
			if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))
			{
				name[n++] = c;
			}
		}
		name[n] = '\0';

		char* field = requestField(fields, name);
		if (field != NULL)
		{
			requestTrim(payload, colon + 1, valueEnd, field);
		}
	}
	return true;
}

// A whole decimal number: digits only, no sign, point or exponent, and no more than a uint32_t holds.
inline bool parseWhole(const char* text, uint32_t& out)
{
	uint64_t value = 0;
	if (*text == '\0')
	{
		return false;
	}
	for (; *text != '\0'; text++)
	{
		if (*text < '0' || *text > '9')
		{
			return false;
		}
		value = value * 10 + (uint64_t)(*text - '0');
		if (value > 0xFFFFFFFFull)
		{
			return false;
		}
	}
	out = (uint32_t)value;
	return true;
}

// A register address in hex, with or without 0x, that fits 16 bits
inline bool parseHex16(const char* text, uint16_t& out)
{
	uint32_t value = 0;
	int digits = 0;
	if (text[0] == '0' && (text[1] == 'x' || text[1] == 'X'))
	{
		text += 2;
	}
	for (; *text != '\0'; text++, digits++)
	{
		char c = *text;
		uint32_t digit;
		if (c >= '0' && c <= '9')
		{
			digit = c - '0';
		}
		else if (c >= 'a' && c <= 'f')
		{
			digit = c - 'a' + 10;
		}
		else if (c >= 'A' && c <= 'F')
		{
			digit = c - 'A' + 10;
		}
		else
		{
			return false;
		}
		value = value * 16 + digit;
		if (value > 0xFFFF)
		{
			return false;
		}
	}
	if (digits == 0)
	{
		return false;
	}
	out = (uint16_t)value;
	return true;
}

// 1 or true is on, 0 or false is off, anything else is refused
inline bool parseFlag(const char* text, bool& out)
{
	if (strcmp(text, "1") == 0 || strcmp(text, "true") == 0)
	{
		out = true;
		return true;
	}
	if (strcmp(text, "0") == 0 || strcmp(text, "false") == 0)
	{
		out = false;
		return true;
	}
	return false;
}

// Text equality at compile time, so a static_assert can refuse a placeholder in Secrets.h
constexpr bool sameText(const char* a, const char* b)
{
	return *a == *b && (*a == '\0' || sameText(a + 1, b + 1));
}

#endif
