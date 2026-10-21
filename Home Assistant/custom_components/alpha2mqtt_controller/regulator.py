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

# Firmware before the one frame block write takes about 1.8 s per dispatch, five register writes. Closer
# than this, a command queues behind the last one's writes, so even an acked command waits this long.
MIN_SEND_GAP_S = 2.0
# Samples come every second, or every 10 s from older firmware. A longer gap would wind the integral on
# an error nobody measured.
MAX_INTEGRATE_S = 10.0
# The inverter cuts discharge about 1 % above its floor, so the hold starts this far above the policy SOC.
FLOOR_MARGIN = 1.5
# A battery this far short of the last setpoint has not arrived: the integral waits, or the error it sees is the
# inverter's lag, not a trim. The wait ends after ARRIVE_S regardless, for an inverter that clamps below
# the request and would otherwise freeze it for good. A battery past the setpoint is not lag (cuts land in
# about 3 s) but a steady offset, such as a DC reading over an AC setpoint, which only the integral can trim.
LAG_W, ARRIVE_S = 100.0, 15.0
# Under this battery power for this long against a request of REFUSED_ASK_W or more, the inverter is refusing
# (at its cutoff, whatever the SOC says): ask for 0 W, drop the integral, try again after the hold. Every start
# logged since 17 Oct left 10 W within 5.1 s, so 20 s is not a slow start. A battery reads about 35 W under a
# 100 to 400 W command, so 100 W asked still reads over 30 W.
REFUSED_W, REFUSED_AFTER_S, REFUSED_HOLD_S, REFUSED_ASK_W = 30.0, 20.0, 60.0, 100.0
# A refusal this close to the policy SOC is the cutoff: no retry each minute, which only asks again into a
# refusal (on 19 Oct, 2 h of it at 12.8 %). It holds until the SOC rises this much or the policy SOC changes.
REFUSED_NEAR_SOC, REARM_SOC = 3.0, 1.0
# Moves under SMALL_TRIM_W are noise and integral creep: they wait TRIM_GAP_S, so the bus carries load steps. On
# 21 Oct three commands in four moved under 50 W, most within a second of the last. Replayed on that day, this
# cut the commands by a third for 0.6 W more mean import; a 10 s wait cut half for 1.8 W.
SMALL_TRIM_W, TRIM_GAP_S = 50.0, 6.0
# Keep warm stands in for a trickle charge this close to idle; a bigger surplus the battery can store is stored.
WARM_BAND_W = 50.0


@dataclass(frozen=True)
class Policy:
    mode: str = RELEASE
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    charge_soc: int = 100
    discharge_soc: int = 10
    fixed_w: float = 0.0  # FIXED only, positive is discharge
    target_offset_w: float = 0.0  # added to the grid target, positive draws more from the grid
    reason: str = ""  # RELEASE only: why, when it is not the policy's choice; shown in the status


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
    # Some inverters start a discharge slowly: an AlphaESS SMILE-B3 was seen to start from about 50 W and climb
    # 6 to 9 W/s once the battery had read 10 W or less, while a step from a running discharge took 3 s. Above 0,
    # this is the ready floor: while discharge is allowed, the command never goes under this discharge, so the
    # battery never idles and the next load step goes out at full rate. Only a surplus the battery can store takes
    # it under, into a charge. 0 is off.
    warm_w: float = 0.0


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
        self.soc: float | None = None
        self._load_ref: float | None = None
        self._t_measure: float | None = None
        self._t_fresh: float | None = None
        self._t_step: float | None = None
        self._regulating = False  # the last step regulated: the integral runs, the next step needs no bumpless start
        self._filtered: float | None = None
        self._saturation = 0
        self._direction = 0
        self._t_direction = float("-inf")
        self._t_sent = float("-inf")
        self._t_ack: float | None = None
        self._ack_power = False
        self._failed = False
        self._t_low: float | None = None  # battery under REFUSED_W against a request, since
        self._t_refused = float("-inf")
        self.refused_soc: float | None = None  # the SOC a refusal near the floor began at; None while armed
        self._refused_floor: int | None = None
        self.integral_w = 0.0
        self._offset_w = 0.0
        self.error_w = 0.0
        self.ff_w = 0.0
        self.request_w = 0.0
        self.output_w = 0.0
        self.sent: Command | None = None
        self.status = "waiting"
        self.limit = "none"  # what holds the output this step, for diagnosis: see step()

    def update_grid(self, t: float, grid_w: float, battery_w: float, fresh: bool = True,
                    soc: float | None = None) -> None:
        """A new grid estimate. fresh is False when only a correction term changed, not the sample. A sample
        without an SOC keeps the last one."""
        if not (math.isfinite(grid_w) and math.isfinite(battery_w)):
            return  # a NaN would clip to full output
        if soc is not None and math.isfinite(soc):
            self.soc = soc
        s = self.settings
        e = self._error(grid_w)
        arrived = (self.sent is None or self.sent.kind == "normal"
                   or _sign(self.sent.signed_w) * (self.sent.signed_w - battery_w) <= LAG_W
                   or t - self._t_sent >= ARRIVE_S)
        if self._regulating and self._t_measure is not None and abs(e) <= s.integral_band_w and arrived:
            # Conditional integration: stop when the output is pinned in the error's direction.
            if _sign(e) != self._saturation:
                dt = min(t - self._t_measure, MAX_INTEGRATE_S)
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

    def _hand_back(self, t: float, status: str, unknown: bool) -> Command | None:
        """Stops regulating. Sends a normal while a lease may run, and again after the minimum interval if the
        last one failed."""
        self.status = status
        self._restart()
        self.output_w = 0.0
        retry = (self.sent is not None and self.sent.kind == "normal" and self._failed
                 and t - self._t_sent >= self.settings.min_interval_s)
        # A restart forgets what the inverter was told, and a crash sends no release: until the first command,
        # a release assumes a lease may still be running and hands the inverter back. A stale start does not:
        # the first sample takes the inverter over within a second.
        if self.lease_active(t) or retry or (unknown and self.sent is None):
            return self._send(t, Command("normal"))
        return None

    def ack(self, t: float, power_write: bool = False) -> None:
        """Alpha2MQTT wrote the last command, as one power write or as the whole block."""
        self._t_ack, self._ack_power = t, power_write

    def fail(self) -> None:
        """The last command was not published or not written: send it again in full once the minimum interval
        is up. It still counts as sent, so a release in the meantime still hands the inverter back."""
        self._failed = True

    def _restart(self) -> None:
        self._regulating = False
        self.integral_w = 0.0
        self._t_low = None
        self.refused_soc = None
        self._filtered = None
        self._saturation = 0

    def step(self, t: float, policy: Policy) -> Command | None:
        """Returns a command to publish now, or None. Without a fresh sample it holds the last command until
        stale_after_s, then hands the inverter back."""
        s = self.settings
        dt = 0.0 if self._t_step is None else max(0.0, t - self._t_step)
        self._t_step = t
        self.limit = "none"
        if policy.mode == FIXED and not math.isfinite(policy.fixed_w):
            policy = Policy(RELEASE)
        if policy.mode == RELEASE:
            # Needs no reading, so it goes out even when the inputs are stale.
            status = f"released: {policy.reason}" if policy.reason else "released"
            return self._hand_back(t, status, unknown=True)
        live = self._t_fresh is not None and t - self._t_fresh <= s.stale_after_s
        if policy.mode in (HOLD, FIXED):
            # Fixed watts need no reading, so they hold through stale inputs.
            self.status = policy.mode
            self._restart()
            self.ff_w = 0.0
            self.error_w = 0.0 if self.grid_w is None else self.grid_w - s.target_w
            request = lo = hi = policy.fixed_w
        elif not live:
            # Hand the inverter back now. Holding the last watts until the lease ran out could mean
            # two minutes at a setpoint chosen for a load that has gone.
            return self._hand_back(t, "stale", unknown=False)
        else:
            if not self._regulating:
                # Bumpless start from whatever the inverter is doing on its own.
                self.output_w = self.battery_w
                self._direction = _sign(self.battery_w)
            self._regulating = policy.mode == REGULATE
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
            floor, ceiling = "discharge_cap", "charge_cap"
            if self.soc is not None and self.soc <= policy.discharge_soc + FLOOR_MARGIN:
                # At its limit the battery refuses to discharge, so ask for 0 W, not a request that only grows.
                hi, floor = 0.0, "floor"
            elif self._refused(t, policy):
                hi, floor = 0.0, "refused"
            if self.soc is not None and self.soc >= policy.charge_soc:
                # Full: the same for a charge, so the export it cannot store does not wind the integral.
                lo, ceiling = 0.0, "full"
            if request > hi:
                self.limit = floor
            elif request < lo:
                self.limit = ceiling
        self.request_w = request
        u = _clip(request, lo, hi)
        self._saturation = _sign(request - u)

        # Charge and discharge only swap after a clear margin and a minimum time in one direction.
        d = _sign(u) if abs(u) >= 1 else 0
        if d and d != self._direction:
            if policy.mode == REGULATE and self._direction and (abs(u) < s.flip_hysteresis_w or t - self._t_direction < s.flip_dwell_s):
                u = 0.0
                self.limit = "turn_round"
            else:
                self._direction, self._t_direction = d, t
        ready_w = s.warm_w if policy.mode == REGULATE and 0 < s.warm_w <= hi else 0.0
        if (ready_w and -WARM_BAND_W < u < ready_w
                and (self._direction >= 0 or t - self._t_direction >= s.flip_dwell_s)):
            # Idle, a discharge under the ready floor or a trickle charge: hold the floor, see Settings.warm_w. A
            # surplus the battery cannot store, or one the turn round holds under the margin, goes out with the
            # floor on top: a slow start costs more than the floor's watts. The output is pinned there, so the
            # export must not wind the integral into a charge. Out of a charge, the floor waits out the dwell.
            u = ready_w
            self.limit = "warm"
            self._saturation = _sign(request - u)
            if self._direction <= 0:
                self._direction, self._t_direction = 1, t

        # Slew counts from the last command sent, over the time since it went out. Steps come several
        # a second, so a per-step limit split a load step across two commands a Min gap apart.
        if self.sent is None:
            self.output_w = u
        else:
            step_max = s.slew_w_per_s * (t - self._t_sent)
            self.output_w = _clip(u, self.sent.signed_w - step_max, self.sent.signed_w + step_max)
        if ready_w and u >= ready_w and self.output_w > -WARM_BAND_W:
            # A turn out of a charge that the slew cuts short lands on the floor, not at idle or under it.
            self.output_w = max(self.output_w, ready_w)
        return self._maybe_send(t, policy)

    def _refused(self, t: float, policy: Policy) -> bool:
        """The battery gives nothing against a real request: the inverter is refusing."""
        asking = self.sent is not None and self.sent.kind == "discharge" and self.sent.watts >= REFUSED_ASK_W
        if asking and self.battery_w is not None and self.battery_w < REFUSED_W:
            if self._t_low is None:
                self._t_low = t
            elif t - self._t_low >= REFUSED_AFTER_S:
                self._t_refused, self._t_low = t, None
                self.integral_w = 0.0
                if self.soc is not None and self.soc <= policy.discharge_soc + REFUSED_NEAR_SOC:
                    self.refused_soc, self._refused_floor = self.soc, policy.discharge_soc
        else:
            self._t_low = None
        if self.refused_soc is not None:
            if self.soc < self.refused_soc + REARM_SOC and policy.discharge_soc == self._refused_floor:
                return True
            self.refused_soc, self._t_refused = None, float("-inf")
        return t - self._t_refused < REFUSED_HOLD_S

    def _command(self, policy: Policy) -> Command:
        s = self.settings
        w = int(round(abs(self.output_w) / 10.0)) * 10
        if policy.mode == REGULATE and self.output_w >= s.warm_w > 0:
            w = max(w, math.ceil(s.warm_w))  # rounding to 10 W must not take a discharge under the ready floor
        last = self.sent
        if policy.mode == HOLD:
            return Command("charge", 0, 100, s.duration_s)
        if w == 0:
            # 0 W in the running block's kind, so idling never costs a block write.
            if last is not None and last.kind != "normal":
                return Command(last.kind, 0, last.soc, s.duration_s)
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
        # A rise to the ready floor goes out however small, or the battery idles until the keepalive.
        floor = math.ceil(s.warm_w)
        to_floor = policy.mode == REGULATE and floor > 0 and last.signed_w < floor <= cmd.signed_w
        # A stop at 0 W goes out however small, as at the floor or a refusal: it is not a trim.
        if (not to_floor and cmd.watts and cmd.soc == last.soc and abs(cmd.signed_w - last.signed_w) < SMALL_TRIM_W
                and since < TRIM_GAP_S):
            return None
        if to_floor or cmd.soc != last.soc or abs(cmd.signed_w - last.signed_w) >= s.write_threshold_w:
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
