"""Battery regulation core. Pure Python, no Home Assistant imports, so it can be tested and simulated alone.

Signs follow Alpha2MQTT: grid power is positive on import, battery power is positive on discharge.

The policy says what is allowed (direction, limits, SOC). The regulator only chooses the watts and
when to send them. Each grid sample comes with the battery power read alongside it, so
grid + battery is the house's own demand at that instant, whatever commands are still in flight.
The next setpoint starts from that: battery now + kp * grid error + integral + feed forward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

RELEASE = "release"  # no dispatch, the inverter runs itself
HOLD = "hold"  # dispatch at 0 W
FIXED = "fixed"  # dispatch at fixed_w, a manual override
REGULATE = "regulate"

# A dispatch is five register writes, about 1.8 s. Closer than this, a command queues behind the last
# one's writes, so even an acked command waits this long.
MIN_SEND_GAP_S = 2.0


@dataclass(frozen=True)
class Policy:
    mode: str = RELEASE
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    charge_soc: int = 100
    discharge_soc: int = 10
    fixed_w: float = 0.0  # FIXED only, positive is discharge
    target_offset_w: float = 0.0  # added to the grid target, positive draws more from the grid


@dataclass
class Settings:
    target_w: float = 20.0  # grid power to hold, positive is a little import
    kp: float = 1.0  # share of the grid error corrected at once
    ki: float = 0.01  # per second, trims the inverter's own error
    integral_band_w: float = 300.0  # bigger errors are load steps: proportional only
    integral_limit_w: float = 300.0
    kff: float = 0.0  # fast load change since the last grid sample
    deadband_w: float = 20.0
    smoothing_s: float = 0.0  # low pass on the request, 0 chases every load step
    slew_w_per_s: float = 2000.0
    write_threshold_w: float = 50.0
    min_interval_s: float = 3.0  # one command costs about 2 s of RS485 writes; an acked import cut goes sooner
    keepalive_s: float = 40.0
    duration_s: int = 120  # dispatch lease, the inverter returns to normal after it
    flip_hysteresis_w: float = 150.0
    flip_dwell_s: float = 5.0  # long enough to ignore noise, short enough to turn round on a load edge
    stale_after_s: float = 30.0
    # 0 keeps every command a full block write. Above 0, a power change may go as one write, and once the
    # firmware acks one as written that way the next command may follow this long after it.
    power_write_gap_s: float = 0.0


@dataclass(frozen=True)
class Command:
    kind: str  # "charge", "discharge" or "normal"
    watts: int = 0
    soc: int = 0
    duration: int = 0
    power_only: bool = False  # the firmware may write just the power if the running block matches

    @property
    def signed_w(self) -> int:
        return self.watts if self.kind == "discharge" else -self.watts


def estimate_grid(correction_w: float, battery_w: float, clamp_w: float | None = None,
                  scale: float | None = None) -> float:
    """Grid power the meter would see, positive on import.

    With a scale, the clamp is trusted: scale x clamp + correction. Without one, correction is the
    house's demand net of solar, and the battery reading from the same sample is taken off it.
    """
    if scale is None:
        return correction_w - battery_w
    return scale * clamp_w + correction_w


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class Regulator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.grid_w: float | None = None
        self.battery_w: float | None = None
        self.load_w: float | None = None
        self._load_ref: float | None = None
        self._t_measure: float | None = None
        self._t_fresh: float | None = None
        self._t_step: float | None = None
        self._mode = RELEASE
        self._filtered: float | None = None
        self._saturation = 0
        self._direction = 0
        self._t_direction = float("-inf")
        self._t_sent = float("-inf")
        self._t_ack: float | None = None
        self._ack_power = False
        self._failed = False
        self.integral_w = 0.0
        self._offset_w = 0.0
        self.error_w = 0.0
        self.ff_w = 0.0
        self.request_w = 0.0
        self.output_w = 0.0
        self.sent: Command | None = None
        self.status = "waiting"

    def update_grid(self, t: float, grid_w: float, battery_w: float, fresh: bool = True) -> None:
        """A new grid estimate. fresh is False when only a correction term changed, not the sample."""
        if not (math.isfinite(grid_w) and math.isfinite(battery_w)):
            return  # a NaN would clip to full output
        s = self.settings
        e = self._error(grid_w)
        if self._mode == REGULATE and self._t_measure is not None and abs(e) <= s.integral_band_w:
            # Conditional integration: stop when the output is pinned in the error's direction.
            if _sign(e) != self._saturation:
                dt = min(t - self._t_measure, s.stale_after_s)
                self.integral_w = _clip(self.integral_w + s.ki * e * dt, -s.integral_limit_w, s.integral_limit_w)
        self.grid_w, self.battery_w, self._t_measure = grid_w, battery_w, t
        if fresh:
            self._t_fresh = t
        self._load_ref = self.load_w

    def _error(self, grid_w: float) -> float:
        e = grid_w - self.settings.target_w - self._offset_w
        return _sign(e) * max(abs(e) - self.settings.deadband_w, 0.0)

    def update_load(self, t: float, load_w: float) -> None:
        self.load_w = load_w
        if self._load_ref is None:
            self._load_ref = load_w

    def lease_active(self, t: float) -> bool:
        return self.sent is not None and self.sent.kind != "normal" and t < self._t_sent + self.sent.duration

    def ack(self, t: float, power_write: bool = False) -> None:
        """Alpha2MQTT wrote the last command, as one power write or as the whole block."""
        self._t_ack, self._ack_power = t, power_write

    def fail(self) -> None:
        """The last command was not published or not written: send it again in full once the minimum interval
        is up. It still counts as sent, so a release in the meantime still hands the inverter back."""
        self._failed = True

    def _restart(self) -> None:
        self.integral_w = 0.0
        self._filtered = None
        self._saturation = 0

    def step(self, t: float, policy: Policy, inputs_ok: bool = True) -> Command | None:
        """Returns a command to publish now, or None."""
        s = self.settings
        dt = 0.0 if self._t_step is None else max(0.0, t - self._t_step)
        self._t_step = t
        if policy.mode == FIXED and not math.isfinite(policy.fixed_w):
            policy = Policy(RELEASE)
        if policy.mode == RELEASE:
            # Needs no reading, so it goes out even when the inputs are stale.
            self.status = "released"
            self._mode = RELEASE
            self._restart()
            self.output_w = 0.0
            if self.lease_active(t):
                return self._send(t, Command("normal"))
            return None
        live = inputs_ok and self._t_fresh is not None and t - self._t_fresh <= s.stale_after_s
        if policy.mode in (HOLD, FIXED):
            # Fixed watts need no reading, so they hold through stale inputs.
            self.status = policy.mode
            self._mode = policy.mode
            self._offset_w = 0.0
            self._restart()
            self.ff_w = 0.0
            self.error_w = 0.0 if self.grid_w is None else self.grid_w - s.target_w
            request = lo = hi = policy.fixed_w
        elif not live:
            # Hand the inverter back now. Holding the last watts until the lease ran out could mean
            # two minutes at a setpoint chosen for a load that has gone.
            self.status = "stale"
            self._mode = RELEASE
            self._restart()
            self.output_w = 0.0
            if self.lease_active(t):
                return self._send(t, Command("normal"))
            return None
        else:
            if self._mode != REGULATE:
                # Bumpless start from whatever the inverter is doing on its own.
                self.output_w = self.battery_w
                self._direction = _sign(self.battery_w)
            self._mode = policy.mode
            self._offset_w = policy.target_offset_w
            e = self._error(self.grid_w)
            self.error_w = self.grid_w - s.target_w - self._offset_w
            self.status = "regulating"
            have_load = self.load_w is not None and self._load_ref is not None
            self.ff_w = s.kff * (self.load_w - self._load_ref) if have_load else 0.0
            request = self.battery_w + s.kp * e + self.integral_w + self.ff_w
            if s.smoothing_s > 0 and self._filtered is not None:
                request = self._filtered + (request - self._filtered) * min(1.0, dt / s.smoothing_s)
            self._filtered = request
            lo, hi = -policy.max_charge_w, policy.max_discharge_w
        self.request_w = request
        u = _clip(request, lo, hi)
        self._saturation = _sign(request - u)

        # Charge and discharge only swap after a clear margin and a minimum time in one direction.
        d = _sign(u) if abs(u) >= 1 else 0
        if d and d != self._direction:
            if policy.mode == REGULATE and self._direction and (abs(u) < s.flip_hysteresis_w or t - self._t_direction < s.flip_dwell_s):
                u = 0.0
            else:
                self._direction, self._t_direction = d, t

        # Slew counts from the last command sent, over the time since it went out. Steps come several
        # a second, so a per-step limit split a load step across two commands a Min gap apart.
        if self.sent is None:
            self.output_w = u
        else:
            step_max = s.slew_w_per_s * (t - self._t_sent)
            self.output_w = _clip(u, self.sent.signed_w - step_max, self.sent.signed_w + step_max)
        return self._maybe_send(t, policy)

    def _command(self, policy: Policy) -> Command:
        s = self.settings
        w = int(round(abs(self.output_w) / 10.0)) * 10
        if policy.mode == HOLD or w == 0:
            return Command("charge", 0, 100, s.duration_s)
        if self.output_w > 0:
            return Command("discharge", w, policy.discharge_soc, s.duration_s)
        return Command("charge", w, policy.charge_soc, s.duration_s)

    def _maybe_send(self, t: float, policy: Policy) -> Command | None:
        s = self.settings
        cmd = self._command(policy)
        last = self.sent
        since = t - self._t_sent
        if since < s.min_interval_s and not self._may_send_early(since, cmd):
            return None  # also after a release or a failed publish, so a flapping policy cannot hammer the inverter
        if last is None or self._failed or not self.lease_active(t) or since >= s.keepalive_s:
            return self._send(t, cmd)
        if cmd.kind != last.kind and (cmd.watts or last.watts):
            return self._send(t, cmd)
        if cmd.soc != last.soc or abs(cmd.signed_w - last.signed_w) >= s.write_threshold_w:
            same_block = (cmd.kind, cmd.soc, cmd.duration) == (last.kind, last.soc, last.duration)
            return self._send(t, replace(cmd, power_only=True) if same_block and s.power_write_gap_s > 0 else cmd)
        return None

    def _may_send_early(self, since: float, cmd: Command) -> bool:
        """The last command is written and a sample has come in since. After a power write the bus is free at
        once, so any move may follow after the power write gap; after a block write only an import cut goes early."""
        last = self.sent
        if (last is None or last.kind == "normal" or self._t_ack is None or self._t_fresh is None
                or self._t_fresh <= self._t_ack):
            return False
        if self._ack_power and self.settings.power_write_gap_s > 0:
            return since >= self.settings.power_write_gap_s
        return since >= MIN_SEND_GAP_S and cmd.signed_w > last.signed_w

    def _send(self, t: float, cmd: Command) -> Command:
        self.sent, self._t_sent, self._t_ack, self._ack_power, self._failed = cmd, t, None, False, False
        return cmd
