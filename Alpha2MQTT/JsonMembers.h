// Pure helpers for the JSON state payloads. No Arduino types, so tests/firmware builds them on the host.
#ifndef _JsonMembers_h
#define _JsonMembers_h

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

#endif
