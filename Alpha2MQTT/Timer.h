// Pure timer arithmetic for checkTimer. No Arduino types, so tests/firmware builds it on the host.
#ifndef _Timer_h
#define _Timer_h

#include <stdint.h>

// True once interval has passed since lastRun. The elapsed time is an unsigned difference, so it stays right
// across the millis() wrap every 49.7 days. A due run keeps the phase, so a run that was held up (a two second
// dispatch write, say) does not shift every run after it. One catch up run at most: the modulo drops any whole
// intervals missed.
inline bool timerDue(uint32_t* lastRun, uint32_t now, uint32_t interval)
{
	uint32_t elapsed = now - *lastRun;

	if (elapsed < interval)
	{
		return false;
	}
	*lastRun = interval == 0 ? now : now - elapsed % interval;
	return true;
}

#endif
