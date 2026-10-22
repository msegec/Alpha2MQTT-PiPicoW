"""Battery regulation core. Pure Python, no Home Assistant imports, so it can be tested and simulated alone.

Signs follow Alpha2MQTT: grid power is positive on import, battery power is positive on discharge.

The policy says what is allowed (direction, limits, SOC). The regulator only chooses the watts and
when to send them. Each grid sample comes with the battery power read alongside it, so
grid + battery is the house's own demand at that instant, whatever commands are still in flight.
The next setpoint starts from that: battery now + kp * grid error + integral + feed forward. P already sends the
whole measured change in demand; feed forward only leads a solar ramp the inverter's 2 to 4 s response lags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

RELEASE = "release"  # no dispatch, the inverter runs itself
HOLD = "hold"  # dispatch at 0 W
FIXED = "fixed"  # dispatch at fixed_w, a manual override
REGULATE = "regulate"

# The inverter's steady offset, battery reading minus setpoint, against the signed setpoint (positive is discharge).
# Measured on an AlphaESS SMILE-B3 over 20 to 22 Oct: charge about -8 W, discharge 100 to 1500 W about -25 W, +198 W
# at 2750 W and +220 W at 3000 W. Chosen, not measured: 0 W at 0 W so a stop stays a stop, the ramps either side of
# it, the line from 1500 to 2750 W, and flat past the ends. The integral alone held it, so after a 2 kW load it
# wound to about -155 W and gave back about +175 W of import when the load stopped.
INVERTER_OFFSET = ((-100.0, -8.0), (0.0, 0.0), (100.0, -25.0), (1500.0, -25.0), (2750.0, 198.0), (3000.0, 220.0))
# Learned offset table (long term trim): watts added to offset_comp x INVERTER_OFFSET at these signed setpoints, linear
# between them, flat past the ends. They index the persisted table, so changing them discards what was learned. The
# 0 W node stays 0 so a stop stays a stop.
TRIM_NODES = (-4000.0, -3500.0, -3000.0, -2500.0, -2000.0, -1500.0, -1000.0, -750.0, -500.0, -250.0, -100.0, 0.0,
              100.0, 250.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 2500.0, 2750.0, 3000.0, 3500.0, 4000.0)
# Neighbouring nodes differ by at most this times their spacing. With the static table's steepest fall (0.25 W per W,
# times offset_comp up to 1.5) the table falls under 1 W per W, so each output has one command. A maths bound, not a
# tuning choice.
TRIM_SLOPE = 0.5
# trim_points rounds each node to 0.1 W, so a stored pair at the slope bound may be this much past it (load_trim
# adds 1e-9 W for float error).
TRIM_ROUND_W = 0.1
# Learning needs the SOC this many percent over the floor hold (discharge_soc + floor_margin_soc), so the samples
# just before the hold takes the discharge to 0 W teach nothing. Set with the table in 9381e49; no measurement
# is behind the 1.
LEARN_SOC_MARGIN = 1.0


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
    # The integral gain on import errors is ki times this. Import costs about four times what export earns, so above 1
    # the integral leans on import first and settles a little on the export side. 1 is symmetric.
    ki_import_x: float = 1.0
    integral_band_w: float = 300.0  # bigger errors are load steps: proportional only
    integral_limit_w: float = 300.0
    # Solar feed forward: -kff x the solar change since the last sample, for that sample only. Replayed on 20 to
    # 22 Oct, 0.5 to 1.0 cut a cloudy day's cost 9 to 16 % and changed little on the others.
    kff: float = 0.0
    # On, the feed forward only leads a ramp: it never turns the battery round on its own. At kff 1 without it,
    # replayed on 20 to 22 Oct, it cost 60 % more turn rounds. Off is the old rule.
    ff_guard: bool = False
    # Above 0, solar under this, before or after, gets no feed forward: a West reading that flips across a template's
    # 95 W floor at dawn and dusk is a 100 W step that is not a ramp. 0 is off.
    solar_ff_min_w: float = 0.0
    deadband_w: float = 20.0
    smoothing_s: float = 0.0  # low pass on the request, 0 chases every load step
    slew_w_per_s: float = 2000.0
    write_threshold_w: float = 50.0
    # Minimum gap between commands; an acked import cut goes sooner. Sized when every block took five writes, about
    # 1.8 s. A block is now one frame of about 50 ms, but a failed or latched frame still falls back to the five.
    min_interval_s: float = 3.0
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
    # Share of INVERTER_OFFSET taken off each regulated command, so the battery lands on the output: 0 off, 1 full.
    offset_comp: float = 0.0
    # Demand spikes of 1 to 1.4 kW lasting a sample or two had the battery answer a load that was gone. Above 0, a
    # sample whose net demand moved more than this from the last one is held a sample: the output stays, and the
    # next sample acts as usual, so a jump that lasts goes out a sample late. 0 is off.
    spike_confirm_w: float = 0.0
    # Above 0, a command that moves this much starts the integral from 0: what it held trimmed the old level, and the
    # offset table owns the new one. 0 is off.
    integral_reset_w: float = 0.0
    # Small trims that cut import (raise the signed command) wait this long instead of small_trim_gap_s. Trims towards
    # export keep small_trim_gap_s: export is the cheap side. Equal to it is the old rule.
    import_trim_gap_s: float = 6.0
    # Long term trim (learned offset table, see TRIM_NODES). trim_learn on: while settled, trim_rate of the integral
    # per second moves into the two nodes around the running setpoint, and the integral gives up the same watts, so
    # the command does not move. Off: nothing is learned, what was learned is still used. Learning needs the command
    # unchanged trim_settle_s, the raw grid error within trim_band_w, a setpoint of at least trim_min_w, SOC under
    # trim_max_soc and LEARN_SOC_MARGIN over the floor hold, no cap, hold, slew or policy offset. Each node stays
    # within trim_limit_w, and the integral minus the trim at the running setpoint stays within integral_limit_w. A
    # rate of 0 learns nothing.
    trim_learn: bool = False
    trim_rate: float = 0.0
    trim_limit_w: float = 100.0
    trim_settle_s: float = 20.0
    trim_band_w: float = 50.0
    trim_min_w: float = 50.0
    trim_max_soc: float = 97.0
    # Dispatch SOC register sent for a charge to 100 %. The register is 0.4 % a step, but 250 stopped the
    # battery at 99.2 % and 252 is full, so 100 % goes as this, as socPercent register - 150.
    full_register: int = 252
    # On, no discharge while no SOC has been read since the start, as after a restart whose first sample lacked it,
    # since the floor cannot be checked. Off keeps the old rule: the inverter's own dispatch SOC is then the only floor.
    discharge_needs_soc: bool = False
    # Above 0, a battery still closing on its setpoint by this much since the arrival clock last restarted has not
    # arrived: the clock restarts, so a slow start's ramp does not wind the integral after arrive_s. 0 is off: the
    # clock runs from the send.
    arrive_restart_w: float = 0.0
    # The inverter cuts discharge about 1 % above its floor, so the hold starts this far above the policy SOC.
    floor_margin_soc: float = 1.5
    # Under refused_w of battery power for refused_after_s against a request of refused_ask_w or more, the inverter is
    # refusing (at its cutoff, whatever the SOC says): ask for 0 W, drop the integral, try again after refused_hold_s.
    # Every start logged since 17 Oct left 10 W within 5.1 s, so 20 s is not a slow start. A battery reads about 35 W
    # under a 100 to 400 W command, so 100 W asked still reads over 30 W.
    refused_w: float = 30.0
    refused_after_s: float = 20.0
    refused_hold_s: float = 60.0
    refused_ask_w: float = 100.0
    # A refusal this close to the policy SOC is the cutoff: no retry each minute, which only asks again into a
    # refusal (on 19 Oct, 2 h of it at 12.8 %). It holds until the SOC rises rearm_soc or the policy SOC changes.
    refused_near_soc: float = 3.0
    rearm_soc: float = 1.0
    # Even an acked block write waits this long before an early send. Sized for five single writes, about 1.8 s a
    # block: closer, a command queues behind the last one's writes. The one frame write takes about 50 ms, but the
    # firmware falls back to the five on any frame failure, and for good once a refused frame latches.
    min_send_gap_s: float = 2.0
    # Samples come every second, or every 10 s from older firmware. A longer gap would wind the integral on
    # an error nobody measured.
    max_integrate_s: float = 10.0
    # A battery lag_w short of the last setpoint has not arrived: the integral waits, or the error it sees is the
    # inverter's lag, not a trim. The wait ends after arrive_s regardless, for an inverter that clamps below
    # the request and would otherwise freeze it for good. A battery past the setpoint is not lag (cuts land in
    # about 3 s) but a steady offset, such as a DC reading over an AC setpoint, which only the integral can trim.
    lag_w: float = 100.0
    arrive_s: float = 15.0
    # Moves under small_trim_w are noise and integral creep: they wait small_trim_gap_s, so the bus carries load
    # steps. On 21 Oct three commands in four moved under 50 W, most within a second of the last. Replayed on that
    # day, this cut the commands by a third for 0.6 W more mean import; a 10 s wait cut half for 1.8 W.
    small_trim_w: float = 50.0
    small_trim_gap_s: float = 6.0
    # Keep warm stands in for a trickle charge this close to idle; a bigger surplus the battery can store is stored.
    warm_band_w: float = 50.0
    # Solar changes this small between samples are noise, as at night, and get no feed forward. Nor does a change over
    # a gap longer than solar_max_gap_s: it is not a ramp the inverter lags, and the error already holds all of it.
    solar_deadband_w: float = 20.0
    solar_max_gap_s: float = 2.0
    command_step_w: int = 10  # commands go out rounded to this many watts
    # Read by the controller only, kept here so every limit has one owner and one cfg record.
    # A cap entity configured but never read yet, or not configured, is this.
    default_max_w: float = 3000.0
    # A plausible reading is finite and under this. A kW value read as W, or a NaN, would otherwise drive the battery.
    max_plausible_w: float = 20000.0
    # Net demand rewrites every few seconds, 26 s at most over a measured day. Older than this, a plug or template has
    # frozen and its last value is not the house any more. So is a correction whose optional age_s attribute, the age
    # of its oldest input, is over this: a template re-rendered every second stays fresh by last_reported even when
    # an input under it froze.
    correction_max_age_s: float = 90.0
    # A block write is acked within about 2 s. No response this long after a publish means the request or its
    # response was lost, as in a Pico reconnecting: the command counts as failed and goes again in full.
    ack_timeout_s: float = 10.0
    # A correction rendered from the fast message, such as a net demand template, carries its seq and may update just
    # after the controller sees the message. A sample waits this long for it, then steps on the value it has.
    match_wait_s: float = 0.5
    # The step response monitor (response.py), which only watches. Smaller steps sit inside the loop's normal trim and
    # the reading noise; a slow ramp costs real import above response_step_w. The target: the battery reaches 80 % of a
    # load step within response_slow_s; anything later is a miss, not a margin. response_timeout_s is long enough that
    # a healthy step never hits it, short enough to report a 9 W/s ramp in the same minute.
    response_step_w: float = 500.0
    response_slow_s: float = 4.0
    response_timeout_s: float = 30.0


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


def inverter_offset(w: float) -> float:
    """The battery reading minus the setpoint expected at setpoint w, from INVERTER_OFFSET."""
    return _interp(*zip(*INVERTER_OFFSET), w)


def _interp(xs, ys, w: float) -> float:
    if w <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if w <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (w - xs[i - 1]) / (xs[i] - xs[i - 1])
    return ys[-1]


def _solve(xs, fs, w: float) -> float:
    """x with f(x) = w on the rising piecewise linear f through (xs, fs), slope 1 past the ends."""
    if w <= fs[0]:
        return xs[0] + w - fs[0]
    for i in range(1, len(xs)):
        if w <= fs[i]:
            return xs[i - 1] + (xs[i] - xs[i - 1]) * (w - fs[i - 1]) / (fs[i] - fs[i - 1])
    return xs[-1] + w - fs[-1]


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class Regulator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.grid_w: float | None = None
        self.battery_w: float | None = None
        self.solar_w: float | None = None
        self.soc: float | None = None
        self._solar_step = 0.0  # solar change from the last sample to this one, 0 under solar_deadband_w
        self._demand: float | None = None  # grid + battery of the last sample
        self._spike = False  # this sample jumped more than spike_confirm_w: the output waits for the next
        self._t_measure: float | None = None
        self._t_fresh: float | None = None
        self._t_step: float | None = None
        self._regulating = False  # the last step regulated: the integral runs, the next step needs no bumpless start
        self._filtered: float | None = None
        self._saturation = 0
        self._direction = 0
        self._t_direction = float("-inf")
        self._t_sent = float("-inf")
        self._t_arrive, self._arrive_w = float("-inf"), 0.0  # the arrival clock and the battery it started at
        self._sent_w = 0.0  # the battery watts the last command should deliver: its watts plus offset_comp x the table
        self._t_ack: float | None = None
        self._ack_power = False
        self._failed = False
        self._t_low: float | None = None  # battery under refused_w against a request, since
        self._t_refused = float("-inf")
        self.refused_soc: float | None = None  # the SOC a refusal near the floor began at; None while armed
        self._refused_floor: int | None = None
        self.integral_w = 0.0
        self._target_offset_w = 0.0
        self.error_w = 0.0
        self.ff_w = 0.0
        self.request_w = 0.0
        self.output_w = 0.0
        self.offset_w = 0.0  # the offset correction: the command asks for output_w + offset_w, see _setpoint
        self.sent: Command | None = None
        self.trim = [0.0] * len(TRIM_NODES)  # learned watts at TRIM_NODES: trim_points, load_trim, reset_trim
        self.trim_moves = 0  # times learning moved the table, so the controller can log and persist on change
        self._t_change = float("-inf")  # when the command's watts or kind last changed
        self._learn_ok = False  # the last step regulated free of every cap, hold, slew and policy offset
        self._grid: tuple | None = None  # (offset_comp, nodes, battery watts at them) for the solve; None when moved
        self.status = "waiting"
        self.limit = "none"  # what holds the output this step, for diagnosis: see step()
        # Also for diagnosis only, never read back: the proportional term, the caps after floor, refusal and full,
        # the output before the slew (None while handed back), and whether the slew cut it.
        self.p_w = 0.0
        self.lo_w: float | None = None
        self.hi_w: float | None = None
        self.u_w: float | None = None
        self.slewed = False

    @property
    def direction(self) -> int:
        """-1 charging, 0 none yet, +1 discharging, as the turn round check sees it."""
        return self._direction

    @property
    def direction_since(self) -> float | None:
        """When the direction last turned, None if it never has since the start."""
        return self._t_direction if math.isfinite(self._t_direction) else None

    @property
    def refusal_since(self) -> float | None:
        """When the battery began giving under refused_w against a discharge ask, None while it gives."""
        return self._t_low

    @property
    def fresh_at(self) -> float | None:
        """When the last fresh sample came in."""
        return self._t_fresh

    def table(self, w: float) -> float:
        """Expected battery reading minus setpoint at setpoint w: offset_comp x INVERTER_OFFSET plus the trim."""
        return self.settings.offset_comp * inverter_offset(w) + _interp(TRIM_NODES, self.trim, w)

    def _setpoint(self, w: float) -> float:
        """The setpoint whose battery reading, on the table, is w."""
        if self._grid is None or self._grid[0] != self.settings.offset_comp:
            xs = sorted(set(TRIM_NODES) | {x for x, _ in INVERTER_OFFSET})
            self._grid = (self.settings.offset_comp, xs, [x + self.table(x) for x in xs])
        return _solve(self._grid[1], self._grid[2], w)

    def trim_points(self) -> list[list[float]]:
        """The learned table as [setpoint W, trim W] pairs, for the controller to persist and show."""
        return [[x, round(y, 1)] for x, y in zip(TRIM_NODES, self.trim)]

    def load_trim(self, points) -> bool:
        """Restore a persisted table. A malformed table, other nodes, a non-zero 0 W node or neighbours further
        apart than TRIM_SLOPE allows loads nothing and returns False. A value past trim_limit_w loads clipped to it,
        so a lowered limit keeps the table."""
        try:
            pts = [(float(x), float(y)) for x, y in points]
        except (TypeError, ValueError):
            return False
        lim = self.settings.trim_limit_w
        if ([x for x, _ in pts] != list(TRIM_NODES) or any(not math.isfinite(y) for _, y in pts)
                or pts[TRIM_NODES.index(0.0)][1] != 0):
            return False
        ys = [_clip(y, -lim, lim) for _, y in pts]  # clipping never steepens the table
        if any(abs(ys[j + 1] - ys[j]) - TRIM_SLOPE * (TRIM_NODES[j + 1] - TRIM_NODES[j]) > TRIM_ROUND_W + 1e-9
               for j in range(len(ys) - 1)):
            return False  # _teach never builds it, and _solve needs the rising table the slope keeps
        self.trim, self._grid = ys, None
        return True

    def reset_trim(self) -> None:
        self.trim, self._grid = [0.0] * len(TRIM_NODES), None

    def _teach(self, c: float, d: float) -> float:
        """Move the trim at setpoint c by d watts over its two nodes, within the bounds. Returns the move made."""
        xs, ys = TRIM_NODES, self.trim
        before = _interp(xs, ys, c)
        i = next((j for j in range(1, len(xs)) if c <= xs[j]), len(xs) - 1)
        a = _clip((c - xs[i - 1]) / (xs[i] - xs[i - 1]), 0.0, 1.0)
        share = {i - 1: 1 - a, i: a}
        n2 = sum(w * w for w in share.values())
        lim = self.settings.trim_limit_w
        for j, w in share.items():
            if xs[j] == 0 or not w:
                continue
            y = _clip(ys[j] + d * w / n2, -lim, lim)
            for k in (j - 1, j + 1):
                if 0 <= k < len(xs):
                    gap = TRIM_SLOPE * abs(xs[k] - xs[j])
                    y = _clip(y, ys[k] - gap, ys[k] + gap)
            ys[j] = y
        self._grid = None
        moved = _interp(xs, ys, c) - before
        self.trim_moves += moved != 0
        return moved

    def update_grid(self, t: float, grid_w: float, battery_w: float, fresh: bool = True,
                    soc: float | None = None, solar_w: float | None = None) -> None:
        """A new grid estimate. fresh is False when only a correction term changed, not the sample. A sample
        without an SOC keeps the last one; solar_w None is unknown."""
        if not (math.isfinite(grid_w) and math.isfinite(battery_w)):
            return  # a NaN would clip to full output
        if soc is not None and math.isfinite(soc):
            self.soc = soc
        if solar_w is not None and not math.isfinite(solar_w):
            solar_w = None
        s = self.settings
        if fresh:
            # Solar step and spike compare samples. A correction update is the same sample: it keeps both, so it
            # cannot start, confirm or end a spike, nor feed forward a solar change twice.
            recent = self._t_fresh is not None and t - self._t_fresh <= s.solar_max_gap_s
            change = solar_w - self.solar_w if recent and solar_w is not None and self.solar_w is not None else 0.0
            min_w = s.solar_ff_min_w
            if min_w > 0 and (solar_w is None or self.solar_w is None or min(solar_w, self.solar_w) < min_w):
                change = 0.0
            self._solar_step = change if abs(change) > s.solar_deadband_w else 0.0
            self.solar_w = solar_w
            demand = grid_w + battery_w
            self._spike = (s.spike_confirm_w > 0 and not self._spike and self._demand is not None
                           and abs(demand - self._demand) > s.spike_confirm_w)
            self._demand = demand
        e = self._error(grid_w)
        if s.arrive_restart_w > 0 and _sign(self._sent_w) * (battery_w - self._arrive_w) >= s.arrive_restart_w:
            self._t_arrive, self._arrive_w = t, battery_w  # still closing: lag, not an offset
        arrived = (self.sent is None or self.sent.kind == "normal"
                   or _sign(self._sent_w) * (self._sent_w - battery_w) <= s.lag_w
                   or t - self._t_arrive >= s.arrive_s)
        if (self._regulating and self._t_measure is not None and abs(e) <= s.integral_band_w and arrived
                and not self._spike):
            # Conditional integration: stop when the output is pinned in the error's direction.
            if _sign(e) != self._saturation:
                dt = min(t - self._t_measure, s.max_integrate_s)
                g = s.ki * (s.ki_import_x if e > 0 else 1.0)
                self.integral_w = _clip(self.integral_w + g * e * dt, -s.integral_limit_w, s.integral_limit_w)
        c = self.sent.signed_w if self.sent is not None and self.sent.kind != "normal" else 0
        raw = grid_w - s.target_w - self._target_offset_w
        if (s.trim_learn and s.trim_rate > 0 and self._learn_ok and arrived and not self._spike
                and self._t_measure is not None and abs(c) >= s.trim_min_w and t - self._t_change >= s.trim_settle_s
                and abs(raw) <= s.trim_band_w and self._target_offset_w == 0 and self.soc is not None
                and self.soc < s.trim_max_soc):
            # Settled, the integral holds minus what the table misses at c: move a share of it into the table.
            dt = min(t - self._t_measure, s.max_integrate_s)
            self.integral_w += self._teach(c, -s.trim_rate * dt * self.integral_w)
        if c:
            # Trim and integral together have no more authority than the integral alone. The command moves by the
            # integral minus the trim at c (learning keeps that difference), so that is what the limit bounds. A trim
            # past the limit on its own is the table's, bounded by trim_limit_w: the integral may only take it back
            # towards the limit, never be forced off 0.
            tr, lim = _interp(TRIM_NODES, self.trim, c), s.integral_limit_w
            self.integral_w = _clip(self.integral_w, min(0.0, tr - lim), max(0.0, tr + lim))
        self.grid_w, self.battery_w, self._t_measure = grid_w, battery_w, t
        if fresh:
            self._t_fresh = t

    def _error(self, grid_w: float) -> float:
        e = grid_w - self.settings.target_w - self._target_offset_w
        return _sign(e) * max(abs(e) - self.settings.deadband_w, 0.0)

    def lease_active(self, t: float) -> bool:
        return self.sent is not None and self.sent.kind != "normal" and t < self._t_sent + self.sent.duration

    def _hand_back(self, t: float, status: str, unknown: bool) -> Command | None:
        """Stops regulating. Sends a normal while a lease may run, and again after the minimum interval if the
        last one failed."""
        self.status = status
        self._restart()
        self.output_w = self.offset_w = 0.0
        self.p_w, self.lo_w, self.hi_w, self.u_w, self.slewed = 0.0, None, None, None, False
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
        self._learn_ok = False
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
            self.p_w = 0.0
            request = lo = hi = policy.fixed_w if policy.mode == FIXED else 0.0  # HOLD sends 0 W, see _command
            held = False
        elif not live:
            # Hand the inverter back now. Holding the last watts until the lease ran out could mean
            # two minutes at a setpoint chosen for a load that has gone.
            return self._hand_back(t, "stale", unknown=False)
        else:
            if not self._regulating:
                # Bumpless start from whatever the inverter is doing on its own. A solar change seen before it, as
                # while off, is already in the battery it starts from. Every _restart comes through here.
                self.output_w = self.battery_w
                self._direction = _sign(self.battery_w)
                self._solar_step = 0.0
            self._regulating = policy.mode == REGULATE
            self._target_offset_w = policy.target_offset_w
            e = self._error(self.grid_w)
            self.error_w = self.grid_w - s.target_w - self._target_offset_w
            self.status = "regulating"
            if self._spike:
                self.limit = "spike"  # see Settings.spike_confirm_w
                return None
            self.ff_w = -s.kff * self._solar_step  # more solar is less demand
            self.p_w = s.kp * e
            request = self.battery_w + self.p_w + self.integral_w
            d = self._direction
            if s.ff_guard and d and _sign(request + self.ff_w) == -d and _sign(request) != -d:
                self.ff_w = 0.0  # see Settings.ff_guard
            request += self.ff_w
            if s.smoothing_s > 0 and self._filtered is not None:
                request = self._filtered + (request - self._filtered) * min(1.0, dt / s.smoothing_s)
            self._filtered = request
            # A cap is a limit, never a push: a negative one is 0 W, not a forced charge or discharge.
            lo, hi = -max(0.0, policy.max_charge_w), max(0.0, policy.max_discharge_w)
            hi_name, lo_name = "discharge_cap", "charge_cap"
            if self.soc is None and s.discharge_needs_soc:
                hi, hi_name = 0.0, "soc_unknown"  # see Settings.discharge_needs_soc
            elif self.soc is not None and self.soc <= policy.discharge_soc + s.floor_margin_soc:
                # At its limit the battery refuses to discharge, so ask for 0 W, not a request that only grows.
                hi, hi_name = 0.0, "floor"
            elif self._refused(t, policy):
                hi, hi_name = 0.0, "refused"
            if self.soc is not None and self.soc >= policy.charge_soc:
                # Full: the same for a charge, so the export it cannot store does not wind the integral.
                lo, lo_name = 0.0, "full"
            if request > hi:
                self.limit = hi_name
            elif request < lo:
                self.limit = lo_name
            held = hi_name != "discharge_cap" or lo_name != "charge_cap"  # in force, whether or not it clips this step
        self.request_w = request
        self.lo_w, self.hi_w = lo, hi
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
        if (ready_w and -s.warm_band_w < u < ready_w
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
        self.u_w = u
        if self.sent is None:
            self.output_w = u
        else:
            step_max = s.slew_w_per_s * (t - self._t_sent)
            self.output_w = _clip(u, self._sent_w - step_max, self._sent_w + step_max)
        if policy.mode == REGULATE:
            # A cut to a cap (floor, refusal, full, the policy's limit) goes out whole: a slew band from a command
            # over the cap would otherwise hold the battery past it. HOLD and FIXED keep the slew to their watts.
            self.output_w = _clip(self.output_w, lo, hi)
        self.slewed = self.output_w != u
        if ready_w and u >= ready_w and self.output_w > -s.warm_band_w:
            # A turn out of a charge that the slew cuts short lands on the floor, not at idle or under it.
            self.output_w = max(self.output_w, ready_w)
        # Everything above is battery watts. Only the command asks for less where the inverter gives more.
        on = policy.mode == REGULATE and (s.offset_comp or any(self.trim))
        self.offset_w = self._setpoint(self.output_w) - self.output_w if on else 0.0
        low_soc = policy.discharge_soc + s.floor_margin_soc + LEARN_SOC_MARGIN
        self._learn_ok = (policy.mode == REGULATE and not held and self.limit == "none" and self._saturation == 0
                          and not self.slewed and self.soc is not None and low_soc < self.soc < policy.charge_soc)
        return self._maybe_send(t, policy)

    def _refused(self, t: float, policy: Policy) -> bool:
        """The battery gives nothing against a real request: the inverter is refusing."""
        s = self.settings
        asking = self.sent is not None and self.sent.kind == "discharge" and self.sent.watts >= s.refused_ask_w
        if asking and self.battery_w is not None and self.battery_w < s.refused_w:
            if self._t_low is None:
                self._t_low = t
            elif t - self._t_low >= s.refused_after_s:
                self._t_refused, self._t_low = t, None
                self.integral_w = 0.0
                if self.soc is not None and self.soc <= policy.discharge_soc + s.refused_near_soc:
                    self.refused_soc, self._refused_floor = self.soc, policy.discharge_soc
        else:
            self._t_low = None
        if self.refused_soc is not None:
            if self.soc < self.refused_soc + s.rearm_soc and policy.discharge_soc == self._refused_floor:
                return True
            self.refused_soc, self._t_refused = None, float("-inf")
        return t - self._t_refused < s.refused_hold_s

    def _command(self, policy: Policy) -> Command:
        s = self.settings
        ask = self.output_w + self.offset_w
        step = s.command_step_w
        w = int(round(abs(ask) / step)) * step
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
        if ask > 0:
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
        ready_cmd = math.ceil(s.warm_w)
        to_floor = policy.mode == REGULATE and ready_cmd > 0 and last.signed_w < ready_cmd <= cmd.signed_w
        # A stop at 0 W goes out however small, as at the floor or a refusal: it is not a trim.
        if (not to_floor and cmd.watts and cmd.soc == last.soc and abs(cmd.signed_w - last.signed_w) < s.small_trim_w
                and since < (s.import_trim_gap_s if cmd.signed_w > last.signed_w else s.small_trim_gap_s)):
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
        return since >= self.settings.min_send_gap_s and cmd.signed_w > last.signed_w

    def _send(self, t: float, cmd: Command) -> Command:
        last = self.sent
        if (self.settings.integral_reset_w > 0 and last is not None
                and abs(cmd.signed_w - last.signed_w) >= self.settings.integral_reset_w):
            self.integral_w = 0.0
        if last is None or (last.kind, last.signed_w) != (cmd.kind, cmd.signed_w):
            self._t_change = t
        self.sent, self._t_sent, self._t_ack, self._ack_power, self._failed = cmd, t, None, False, False
        self._t_arrive, self._arrive_w = t, self.battery_w or 0.0
        self._sent_w = cmd.signed_w + self.table(cmd.signed_w)
        return cmd
