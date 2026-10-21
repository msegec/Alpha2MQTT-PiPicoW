"""Step response monitor: how long the battery takes to follow a large step in the setpoint. It only
watches; it changes no command. No Home Assistant imports, so it can be tested alone.

Signs follow the regulator: positive is discharge. A step is measured in its own direction, so a charge
step is timed the same way as a discharge step, and a step back towards zero is not timed at all.

The limits are the regulator's Settings fields response_step_w, response_slow_s and response_timeout_s, read live from
the settings object passed in, so a number changes them at once.
"""

from __future__ import annotations

# Arrival is this share of the rise: the last fifth is the integral's trim, not the inverter's response.
REACH = 0.8
# A request cut below this share of the step's setpoint is a new decision, not the step held.
CANCEL = 0.8


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class ResponseMonitor:
    def __init__(self, settings) -> None:
        self._settings = settings
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
            step_w = self._settings.response_step_w
            if d * (setpoint_w - battery_w) > step_w and d * (setpoint_w - (prev or 0.0)) > step_w:
                self._step = (t, battery_w, setpoint_w, d)
        return records

    def sample(self, t: float, battery_w: float) -> list[dict]:
        """A battery reading. Returns the finished record, if this reading finishes the step."""
        if self._step is None:
            return []
        t0, start, target, d = self._step
        if d * (battery_w - start) >= REACH * d * (target - start):
            return [self._finish(t, battery_w, "slow" if t - t0 > self._settings.response_slow_s else "ok")]
        if t - t0 >= self._settings.response_timeout_s:
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
