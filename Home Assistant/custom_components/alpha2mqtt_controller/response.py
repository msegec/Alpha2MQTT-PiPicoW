"""Step response monitor: how long the battery takes to follow a large step in the setpoint. It only
watches; it changes no command. No Home Assistant imports, so it can be tested alone.

Signs follow the regulator: positive is discharge. A step is measured in its own direction, so a charge
step is timed the same way as a discharge step, and a step back towards zero is not timed at all.
"""

from __future__ import annotations

# Smaller steps sit inside the loop's normal trim and the reading noise; a slow ramp costs real import above this.
STEP_W = 500.0
# Arrival is this share of the rise: the last fifth is the integral's trim, not the inverter's response.
REACH = 0.8
# The target: the battery matches a load step within 4 s. Anything later is a miss, not a margin.
SLOW_S = 4.0
# Long enough that a healthy step never hits it, short enough to report a 9 W/s ramp in the same minute.
TIMEOUT_S = 30.0
# A request cut below this share of the step's setpoint is a new decision, not the step held.
CANCEL = 0.8


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class ResponseMonitor:
    def __init__(self) -> None:
        self._setpoint: float | None = None  # the last command's signed watts, None for no dispatch
        self._step: tuple[float, float, float, int] | None = None  # t, battery, setpoint, direction

    def command(self, t: float, setpoint_w: float | None, battery_w: float | None) -> list[dict]:
        """A command went out. setpoint_w is None for a release. Returns a cancelled record, if any."""
        prev, self._setpoint = self._setpoint, setpoint_w
        records = []
        if self._step is not None:
            _, _, target, d = self._step
            if setpoint_w is None or d * setpoint_w < CANCEL * d * target:
                records.append(self._finish(t, battery_w, "cancelled"))
        if self._step is None and setpoint_w is not None and battery_w is not None:
            d = _sign(setpoint_w)
            # A step over both the battery and the last setpoint: a keepalive the battery still lags is no new step.
            if d * (setpoint_w - battery_w) > STEP_W and d * (setpoint_w - (prev or 0.0)) > STEP_W:
                self._step = (t, battery_w, setpoint_w, d)
        return records

    def sample(self, t: float, battery_w: float) -> list[dict]:
        """A battery reading. Returns the finished record, if this reading finishes the step."""
        if self._step is None:
            return []
        t0, start, target, d = self._step
        if d * (battery_w - start) >= REACH * d * (target - start):
            return [self._finish(t, battery_w, "slow" if t - t0 > SLOW_S else "ok")]
        if t - t0 >= TIMEOUT_S:
            return [self._finish(t, battery_w, "timeout")]
        return []

    def _finish(self, t: float, battery_w: float | None, result: str) -> dict:
        t0, start, target, d = self._step
        self._step = None
        elapsed = t - t0
        moved = None if battery_w is None else d * (battery_w - start)
        return {
            "result": result,
            "slow": result in ("slow", "timeout"),
            "from_w": round(start), "to_w": round(target), "rise_w": round(d * (target - start)),
            "t80_s": round(elapsed, 1) if result in ("ok", "slow") else None,
            "elapsed_s": round(elapsed, 1),
            # Mean rate in the step's direction, in W/s: about 9 for the slow ramp, hundreds for a healthy step.
            "slope_w_s": None if moved is None or elapsed <= 0 else round(moved / elapsed, 1),
        }
