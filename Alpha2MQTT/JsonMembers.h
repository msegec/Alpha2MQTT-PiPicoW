// Pure helpers for the JSON state payloads and their buffers. No Arduino types, so tests/firmware builds them on the host.
#ifndef _JsonMembers_h
#define _JsonMembers_h

#include <stddef.h>
#include <string.h>

// A state payload is one JSON object with a member per register, and a register that fails to read is left out.
// Each member goes in as "    \"name\": value" with no line end. The separator goes before a member, once one is
// in, so the object stays valid whichever reads fail. With every read good the bytes match a comma after each
// member but the last.

// What goes in front of the next member. Notes that one is now in.
inline const char* jsonMemberSeparator(bool& anyMember)
{
	const char* separator = anyMember ? ",\r\n" : "";

	anyMember = true;
	return separator;
}

// Ends the last member's line, if any, and closes the object.
inline const char* jsonObjectEnd(bool anyMember)
{
	return anyMember ? "\r\n}" : "}";
}

// Appends addition to the string in payload, a buffer of size bytes. An addition that does not fit with the
// terminator is refused whole and changes nothing. length is the string's length after, or what it would have been.
inline bool payloadAppend(char* payload, size_t size, const char* addition, size_t& length)
{
	size_t used = strlen(payload);
	size_t adding = strlen(addition);

	length = used + adding;
	if (length >= size)
	{
		return false;
	}
	memmove(payload + used, addition, adding + 1);
	return true;
}

// The next PubSubClient buffer size to try at boot: step down, then the floor itself, then 0 for none left.
inline int nextBufferSize(int size, int smallest, int step)
{
	if (size <= smallest)
	{
		return 0;
	}
	return size - step > smallest ? size - step : smallest;
}

#endif
