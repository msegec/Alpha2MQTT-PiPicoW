"""Home Assistant glue: feeds the regulator from MQTT and entities, publishes its commands."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .regulator import FIXED, HOLD, REGULATE, RELEASE, Command, Policy, Regulator, estimate_grid
from .response import ResponseMonitor
from .steplog import StepLog, file_name

_LOGGER = logging.getLogger(__name__)

DOMAIN = "alpha2mqtt_controller"

CONF_STATE_TOPIC = "state_topic"
CONF_BASE_TOPIC = "base_topic"
CONF_POLICY_ENTITY = "policy_entity"
CONF_GRID_SOURCE = "grid_source"
CONF_GRID_SCALE = "grid_scale"
CONF_GRID_SCALE_ENTITY = "grid_scale_entity"
CONF_CORRECTION_ENTITY = "correction_entity"
CONF_MAX_CHARGE_ENTITY = "max_charge_entity"
CONF_MAX_DISCHARGE_ENTITY = "max_discharge_entity"
CONF_LOG_DIR = "log_dir"  # default <config>/alpha2mqtt_controller
CONF_LOG_ENTITIES = "log_entities"  # also logged on every change, for replay

DEFAULT_STATE_TOPIC = "Alpha2MQTT/state/fast"  # every second; older firmware has only state/second/ten
DEFAULT_BASE_TOPIC = "Alpha2MQTT"
DEFAULT_POLICY_ENTITY = "sensor.battery_auto_action"
# The firmware refuses a socPercent over 105 (dispatchRequestValid), and a negative one wraps to the same refusal.
SOC_MAX = 105
# A direction the policy did not open gets a 0 W cap and Policy's default SOC limit, 10 % or 100 %. The limits still
# bound offset learning, which runs only between the two.
CLOSED = Policy()
# The Commands in the last hour sensor counts publishes this recent.
COMMAND_WINDOW_S = 3600
# The firmware reads a request's seq with strtoul into a 32-bit unsigned long and echoes it as %lu, so a seq stays in
# 1..SEQ_MAX; 0 is what it echoes for a request with none.
SEQ_MAX = 2**32 - 1
# A controller numbers its commands on from the wall clock in tenths of a second. The regulator sends at most about two
# a second, so after a reload no seq repeats one a late response from the run before could still carry.
SEQ_PER_S = 10
# The responseStatus of each set request that was written, as Definitions.h spells them. Any other status is a failure.
SET_SUCCESS = frozenset({"setChargeSuccess", "setDishargeSuccess", "setNormalSuccess"})
# The largest grid clamp scale, either sign: the form's range, and the bound on a scale entity. The scale is a factor,
# so max_plausible_w, a limit in watts, is no bound for it.
GRID_SCALE_MAX = 100.0
# clamp: scale x grid clamp + correction. estimate: correction - battery, for a clamp that cannot see the house.
GRID_SOURCES = ["clamp", "estimate"]

# Samples come every second. Once the last is this old, the status sensor says when it came; under it, nothing, so
# the attribute changes at the edges of a gap and not with every sample.
SAMPLE_LATE_S = 3.0
# While learning moves the offset table, it is saved this long after a move and goes in the step log at most this
# often. A restart within it loses at most this much learning.
TRIM_SAVE_S = 60.0

GRID_KEY = "REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1"
BATTERY_KEY = "REG_BATTERY_HOME_R_BATTERY_POWER"
SOC_KEY = "REG_BATTERY_HOME_R_SOC"
# Step log name and fast state key of each firmware field.
FAST_FIELDS = (("seq", "seq"), ("ms", "ms"), ("read_ms", "read_ms"), ("grid", GRID_KEY),
               ("pv", "REG_PV_METER_R_TOTAL_ACTIVE_POWER_1"), ("battery", BATTERY_KEY), ("soc", SOC_KEY),
               ("dispatch", "REG_DISPATCH_RW_DISPATCH_START"))


def _plausible(x: float, limit: float) -> bool:
    return math.isfinite(x) and abs(x) <= limit


def _code_version() -> str:
    """The first 12 hex of a sha256 over regulator.py and controller.py, so a log says which loop ran."""
    folder = os.path.dirname(__file__)
    digest = hashlib.sha256()
    for name in ("regulator.py", "controller.py"):
        with open(os.path.join(folder, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


def _number(hass: HomeAssistant, entity_id: str | None, limit: float, max_age_s: float | None = None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    try:
        x = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    if not _plausible(x, limit):
        return None
    if max_age_s is not None and (dt_util.utcnow() - state.last_reported).total_seconds() > max_age_s:
        return None
    return x


class Controller:
    def __init__(self, hass: HomeAssistant, config: dict, entry_id: str) -> None:
        self.hass = hass
        base = config.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        # A second controller, such as a shadow on a topic nothing acts on, gets its own entity ids.
        name = "Alpha2MQTT battery controller" if base == DEFAULT_BASE_TOPIC else f"Alpha2MQTT battery controller ({base})"
        self.device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)}, name=name)
        self.config = config
        self.regulator = Regulator()
        self.enabled = False
        self.code: str | None = None  # see _code_version
        self.grid_w: float | None = None
        self._raw_grid: float | None = None
        self._battery_w: float | None = None
        self._soc: float | None = None
        self._sample_seq = None
        self._waiting: tuple[dict, float, float, float] | None = None  # a sample waiting for its correction
        self._cancel_wait: Callable[[], None] | None = None
        self._cmd_seq = int(time.time() * SEQ_PER_S) % SEQ_MAX  # the last seq used, see SEQ_PER_S
        self.last_command: Command | None = None
        self.last_sent: datetime | None = None
        self.last_seq: int | None = None
        self.ack: dict = {}
        self._answered_seq = None  # the newest seq with a response, or given up on
        self._seq_echo = False  # the firmware echoes seq, so a missing response means a lost command
        self._sent_mono: dict[int, float] = {}
        self._sent_times: deque[float] = deque()
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._steplog: StepLog | None = None
        self._failing: set[str] = set()  # what is failing now, each logged once, not every second
        self._gaps: set[str] = set()  # inputs missing now, logged the same way
        self._unknown_policy_reason = ""  # the policy state the controller does not know, logged once per value
        self._good_policy: tuple[float, Policy] = (float("-inf"), Policy(RELEASE))
        self._response = ResponseMonitor(self.regulator.settings)
        self.last_response: dict | None = None  # the last step response timed out or reached, not cancelled
        self._slow: tuple[object, int] = (None, 0)  # local date, slow responses on it
        self._walls: dict[str, tuple[float, str]] = {}  # name: (monotonic time, the same as wall clock text)
        self._caps: dict[str, float] = {}  # the last readable value of each cap entity, by config key
        self._store = Store(hass, 1, f"{DOMAIN}.{entry_id}.learned_offsets")  # the long term trim, across restarts
        self._trim_moves = 0  # regulator.trim_moves when last seen, so a move is noticed once
        self._trim_unsaved = False
        self._t_trim_log = float("-inf")

    @property
    def status(self) -> str:
        return self.regulator.status if self.enabled else "disabled"

    @property
    def _limit(self) -> float:
        """The largest plausible reading, see Settings.max_plausible_w."""
        return self.regulator.settings.max_plausible_w

    @property
    def commands_last_hour(self) -> int:
        while self._sent_times and time.monotonic() - self._sent_times[0] > COMMAND_WINDOW_S:
            self._sent_times.popleft()
        return len(self._sent_times)

    @property
    def slow_responses_today(self) -> int:
        day, count = self._slow
        return count if day == dt_util.now().date() else 0

    def wall(self, name: str, t: float | None) -> str | None:
        """A monotonic time as UTC wall clock text, to the second. Worked out once per time, so a display attribute
        holding it stays the same text and causes no state write until the time itself changes."""
        if t is None or not math.isfinite(t):
            return None
        known = self._walls.get(name)
        if known is None or known[0] != t:
            wall = dt_util.utcnow() - timedelta(seconds=time.monotonic() - t)
            known = self._walls[name] = (t, wall.isoformat(timespec="seconds"))
        return known[1]

    @property
    def policy_gap_since(self) -> str | None:
        """While the policy is unreadable, when it was last read; it is kept until stale_after_s from then."""
        return self.wall("policy", self._good_policy[0]) if "policy" in self._gaps else None

    @property
    def sample_gap_since(self) -> str | None:
        """When the last fresh sample came, once it is SAMPLE_LATE_S old; the regulator goes stale at stale_after_s."""
        t = self.regulator.fresh_at
        return self.wall("sample", t) if t is not None and time.monotonic() - t > SAMPLE_LATE_S else None

    def _responses(self, records: list[dict]) -> None:
        for record in records:
            self._log("resp", **record)
            if record["result"] == "cancelled":
                continue
            self.last_response = record
            if record["slow"]:
                self._slow = (dt_util.now().date(), self.slow_responses_today + 1)
                _LOGGER.warning("Battery controller step response slow: %s", record)

    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    async def async_start(self) -> None:
        cfg = self.config
        base = cfg.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        log_dir = cfg.get(CONF_LOG_DIR) or self.hass.config.path(DOMAIN)
        self.code = await self.hass.async_add_executor_job(_code_version)
        try:
            self._steplog = await self.hass.async_add_executor_job(StepLog, os.path.join(log_dir, file_name(base)))
        except OSError as err:
            _LOGGER.warning("Battery controller step log is off: %s", err)
        self._unsubs.append(await mqtt.async_subscribe(
            self.hass, cfg.get(CONF_STATE_TOPIC, DEFAULT_STATE_TOPIC), self._on_state))
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{base}/response/set/+", self._on_response))
        if self._steplog is not None:
            self._log("start", code=self.code)
            self._log("cfg", **asdict(self.regulator.settings))
            # Every request on the bus, so writes by hand or by a script show next to the controller's own.
            self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{base}/request/#", self._on_request))
            if cfg.get(CONF_LOG_ENTITIES):
                self._unsubs.append(async_track_state_change_event(
                    self.hass, cfg[CONF_LOG_ENTITIES], self._on_log_entity))
        await self._load_trim()
        watched = [cfg.get(k) for k in (CONF_POLICY_ENTITY, CONF_GRID_SCALE_ENTITY, CONF_CORRECTION_ENTITY,
                                        CONF_MAX_CHARGE_ENTITY, CONF_MAX_DISCHARGE_ENTITY)]
        self._unsubs.append(async_track_state_change_event(
            self.hass, [e for e in watched if e], self._on_entity))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=1)))

    async def async_stop(self) -> None:
        try:
            await self.async_set_enabled(False)
        finally:
            while self._unsubs:
                self._unsubs.pop()()
            if self._cancel_wait is not None:
                self._cancel_wait()
                self._cancel_wait = None
            if self._trim_unsaved:
                await self._store.async_save(self._trim_data())
                self._trim_unsaved = False
            if self._steplog is not None:
                self._log("stop")
                steplog, self._steplog = self._steplog, None
                await self.hass.async_add_executor_job(steplog.close)

    async def async_set_enabled(self, enabled: bool) -> None:
        was, self.enabled = self.enabled, enabled
        if was and not enabled:
            # Hand the inverter back now rather than waiting for the lease to run out. Off is off even
            # when this publish fails: the lease then returns the inverter to normal within two minutes.
            cmd = self.regulator.step(time.monotonic(), Policy(RELEASE))
            if cmd is not None:
                await self._publish(cmd)
        self._notify()

    async def _load_trim(self) -> None:
        """Restores the learned offset table saved by an earlier run. Nothing saved logs nothing."""
        try:
            data = await self._store.async_load()
        except Exception:  # an unreadable store must not stop the start: learning starts from zero
            _LOGGER.exception("Battery controller learned offsets unreadable, starting from zero")
            self._log("trim_load", loaded=False, points=self.regulator.trim_points())
            return
        if not isinstance(data, dict) or "points" not in data:
            return
        loaded = self.regulator.load_trim(data["points"])
        self._trim_moves = self.regulator.trim_moves
        self._log("trim_load", loaded=loaded, points=self.regulator.trim_points())

    def _trim_data(self) -> dict:
        self._trim_unsaved = False
        return {"points": self.regulator.trim_points()}

    def _trim_moved(self) -> None:
        """Saves the table TRIM_SAVE_S after learning moved it, and logs it at most that often."""
        if self.regulator.trim_moves == self._trim_moves:
            return
        self._trim_moves = self.regulator.trim_moves
        self._trim_unsaved = True
        self._store.async_delay_save(self._trim_data, TRIM_SAVE_S)
        now = time.monotonic()
        if now - self._t_trim_log >= TRIM_SAVE_S:
            self._t_trim_log = now
            self._log("trim", points=self.regulator.trim_points())

    async def async_forget_trim(self) -> None:
        """Zeroes the learned offset table and saves it at once."""
        self.regulator.reset_trim()
        self._trim_moves = self.regulator.trim_moves
        await self._store.async_save(self._trim_data())
        self._log("trim", points=self.regulator.trim_points(), forgot=True)
        self._notify()

    def set_setting(self, key: str, value: float) -> None:
        """Home Assistant gives a number as a float. A whole number setting stays an int: the firmware strips a "."
        from a value, so a duration of 120.0 would be 1200."""
        current = getattr(self.regulator.settings, key)
        if isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int):
            value = int(round(value))
        setattr(self.regulator.settings, key, value)
        if key == "trim_limit_w":
            self.regulator.load_trim(self.regulator.trim_points())  # clip the learned table to the new limit
        self._log("cfg", **asdict(self.regulator.settings))

    def _grid_estimate(self) -> float | None:
        """Grid power as the utility meter would see it, from the clamp or from the correction alone."""
        if self._raw_grid is None:
            return None
        if self.config.get(CONF_GRID_SOURCE) == "estimate":
            correction = self._correction(self.regulator.settings.correction_max_age_s)
            return None if correction is None else estimate_grid(correction, self._battery_w)
        scale = _number(self.hass, self.config.get(CONF_GRID_SCALE_ENTITY), GRID_SCALE_MAX)
        if scale is None:
            if self.config.get(CONF_GRID_SCALE_ENTITY):
                return None
            scale = float(self.config.get(CONF_GRID_SCALE, 1.0))
        correction = 0.0
        if self.config.get(CONF_CORRECTION_ENTITY):
            correction = self._correction()
            if correction is None:
                return None
        return estimate_grid(correction, self._battery_w, self._raw_grid, scale)

    def _correction(self, max_age_s: float | None = None) -> float | None:
        """The correction entity's watts, None when unreadable, older than max_age_s or with age_s over
        correction_max_age_s."""
        entity_id = self.config.get(CONF_CORRECTION_ENTITY)
        correction = _number(self.hass, entity_id, self._limit, max_age_s)
        if correction is None:
            return None
        try:
            age = float(self.hass.states.get(entity_id).attributes["age_s"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return correction  # no age_s, or not a number: the entity makes no claim
        return None if age > self.regulator.settings.correction_max_age_s else correction

    def _policy(self) -> Policy:
        """The policy now. A policy that is missing or unreadable for a moment, as in a template reload,
        keeps the last good one until stale_after_s; then the inverter goes back to normal."""
        now = time.monotonic()
        policy = self._read_policy()
        self._gap("policy", policy is None)
        if policy is not None:
            self._unknown_policy(policy.reason)
            self._good_policy = (now, policy)
            return policy
        t, policy = self._good_policy
        return policy if now - t <= self.regulator.settings.stale_after_s else Policy(RELEASE)

    def _cap(self, key: str) -> float:
        """A cap entity's watts, 0 or more. Unreadable, it keeps its last value: a cap set low on purpose must not
        jump to default_max_w in a template reload. One never read yet, or not configured, is default_max_w."""
        cap = _number(self.hass, self.config.get(key), self._limit)
        if cap is not None:
            self._caps[key] = max(0.0, cap)  # a negative cap is 0 W
        return self._caps.get(key, self.regulator.settings.default_max_w)

    def _read_policy(self) -> Policy | None:
        """None when the policy cannot be read; normal is a release, and so is a state it does not know."""
        state = self.hass.states.get(self.config.get(CONF_POLICY_ENTITY, DEFAULT_POLICY_ENTITY))
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        if state.state == "hold":
            return Policy(HOLD)
        if state.state == "normal":
            return Policy(RELEASE)
        if state.state not in ("charge", "discharge"):
            return Policy(RELEASE, reason=f"unknown policy state {state.state[:40]!r}")
        try:
            soc = int(float(state.attributes.get("soc_target")))
        except (TypeError, ValueError, OverflowError):
            return None
        if not 0 <= soc <= SOC_MAX:
            return None  # every command would be refused and the lease would lapse while the status says regulating
        if str(state.attributes.get("fixed")).lower() == "true":
            # A manual override: the policy's watts, not regulation.
            try:
                watts = float(state.attributes.get("watts"))
            except (TypeError, ValueError):
                return None
            if not _plausible(watts, self._limit):
                return None
            if state.state == "charge":
                return Policy(FIXED, fixed_w=-watts, charge_soc=soc)
            return Policy(FIXED, fixed_w=watts, discharge_soc=soc)
        charge_cap = self._cap(CONF_MAX_CHARGE_ENTITY)
        discharge_cap = self._cap(CONF_MAX_DISCHARGE_ENTITY)
        # With both, the policy also allows the other direction, so a load edge turns the battery round
        # here, in the next step, instead of waiting for the policy's next decision. other_soc is that
        # direction's limit.
        both = str(state.attributes.get("both")).lower() == "true"
        try:
            other_soc = int(float(state.attributes.get("other_soc")))
        except (TypeError, ValueError, OverflowError):
            both = False
        else:
            both = both and 0 <= other_soc <= SOC_MAX
        if state.state == "charge":
            # The policy's grid_offset moves the grid target while charging: + tops up from the grid.
            try:
                offset = float(state.attributes.get("grid_offset", 0))
            except (TypeError, ValueError):
                offset = 0.0
            if not _plausible(offset, self._limit):
                offset = 0.0
            return Policy(REGULATE, max_charge_w=charge_cap, charge_soc=soc, target_offset_w=offset,
                          max_discharge_w=discharge_cap if both else 0.0,
                          discharge_soc=other_soc if both else CLOSED.discharge_soc)
        return Policy(REGULATE, max_discharge_w=discharge_cap, discharge_soc=soc,
                      max_charge_w=charge_cap if both else 0.0,
                      charge_soc=other_soc if both else CLOSED.charge_soc)

    @callback
    def _on_state(self, msg) -> None:
        try:
            data = json.loads(msg.payload)
            raw_grid, battery = float(data[GRID_KEY]), float(data[BATTERY_KEY])
        except (ValueError, KeyError, TypeError):
            self._log("drop", why="unreadable", p=msg.payload)
            return
        if not (_plausible(raw_grid, self._limit) and _plausible(battery, self._limit)):
            self._log("drop", why="implausible", p=msg.payload)
            return
        seq = data.get("seq")
        if seq is not None and seq == self._sample_seq:
            return  # a repeat, not a new reading
        self._sample_seq = seq
        if self._waiting is not None:
            self._take(*self._waiting)  # it has waited long enough
        if self._correction_behind(seq):
            self._waiting = (data, raw_grid, battery, time.monotonic())
            self._cancel_wait = async_call_later(self.hass, self.regulator.settings.match_wait_s, self._on_wait_over)
        else:
            self._take(data, raw_grid, battery, time.monotonic())

    @callback
    def _on_wait_over(self, _now) -> None:
        self._cancel_wait = None
        if self._waiting is not None:
            self._take(*self._waiting)

    def _take(self, data: dict, raw_grid: float, battery: float, t_seen: float) -> None:
        """Steps on a sample and logs the step."""
        self._waiting = None
        if self._cancel_wait is not None:
            self._cancel_wait()
            self._cancel_wait = None
        self._raw_grid, self._battery_w = raw_grid, battery
        try:
            self._soc = float(data[SOC_KEY])
        except (KeyError, TypeError, ValueError):
            self._soc = None  # a failed read is left out of the message; the regulator keeps the last
        self._responses(self._response.sample(t_seen, battery))
        self._log_step(data, t_seen, self._measure(fresh=True))

    def _log_step(self, data: dict, t_seen: float, cmd: Command | None) -> None:
        r = self.regulator
        watts = {k: None if v is None else round(v, 1) for k, v in (
            ("grid_w", self.grid_w), ("solar_w", r.solar_w), ("error_w", r.error_w), ("integral_w", r.integral_w),
            ("ff_w", r.ff_w), ("request_w", r.request_w), ("output_w", r.output_w), ("offset_w", r.offset_w))}
        self._log("step", **{name: data.get(key) for name, key in FAST_FIELDS},
                  wait_ms=round((time.monotonic() - t_seen) * 1000), status=self.status, limit=r.limit, **watts,
                  sent=None if cmd is None else {"kind": cmd.kind, "watts": cmd.watts, "soc": cmd.soc,
                                                 "power_only": cmd.power_only})

    def _correction_seq(self):
        """The seq of the fast message the correction was rendered from, None if it carries none."""
        entity_id = self.config.get(CONF_CORRECTION_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        return None if state is None else state.attributes.get("seq")

    def _solar(self) -> float | None:
        """Solar in W from the correction entity's west and east attributes, None unless both are numbers. None too
        when its west_fallback attribute is true: West from the 10 s message, so a step against the 1 s one is not a
        solar change."""
        entity_id = self.config.get(CONF_CORRECTION_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is not None and str(state.attributes.get("west_fallback")).lower() == "true":
            return None
        try:
            solar = float(state.attributes["west"]) + float(state.attributes["east"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return solar if _plausible(solar, self._limit) else None

    def _correction_behind(self, seq) -> bool:
        """True while the correction comes from an older message. Without a seq on either side it never waits."""
        correction_seq = self._correction_seq()
        return seq is not None and correction_seq is not None and str(correction_seq) != str(seq)

    def _measure(self, fresh: bool) -> Command | None:
        # A gap in the estimate sends nothing to the regulator, which holds its last command until stale_after_s.
        self.grid_w = self._grid_estimate()
        if self._raw_grid is not None:
            self._gap("grid estimate", self.grid_w is None)
        if self.grid_w is not None and self._battery_w is not None:
            self.regulator.update_grid(time.monotonic(), self.grid_w, self._battery_w, fresh=fresh,
                                       soc=self._soc, solar_w=self._solar())
            self._trim_moved()
        return self._run()

    @callback
    def _on_entity(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self.config.get(CONF_CORRECTION_ENTITY) and self._correction_seq() is not None:
            # A correction rendered from a sample steps that sample and is never paired with another.
            if self._waiting is not None and not self._correction_behind(self._waiting[0].get("seq")):
                self._take(*self._waiting)
            else:
                self._run()
        elif entity_id in (self.config.get(CONF_CORRECTION_ENTITY), self.config.get(CONF_GRID_SCALE_ENTITY)):
            self._measure(fresh=False)
        else:
            self._run()

    @callback
    def _on_tick(self, _now) -> None:
        self._check_ack()
        self._run()

    def _check_ack(self) -> None:
        seq = self.last_seq
        if not self._seq_echo or seq is None or seq == self._answered_seq:
            return
        timeout = self.regulator.settings.ack_timeout_s
        if self.regulator.sent is self.last_command and time.monotonic() - self._sent_mono[seq] > timeout:
            self._answered_seq = seq
            self._log("ack_timeout", seq=seq)
            self.regulator.fail()

    @callback
    def _run(self) -> Command | None:
        cmd = None
        if self.enabled:
            now = time.monotonic()
            last = self.regulator.sent
            refused = self.regulator.refused_soc
            try:
                policy = self._policy()
                cmd = self.regulator.step(now, policy)
            except Exception:  # one bad value must not stop regulation silently
                self._fail("step", "Battery controller step failed, handing the inverter back")
                cmd = self.regulator.step(now, Policy(RELEASE))
            else:
                self._recover("step")
                if refused is None and self.regulator.refused_soc is not None:
                    self._log("refused", soc=self.regulator.refused_soc, floor=policy.discharge_soc)
                elif refused is not None and self.regulator.refused_soc is None:
                    self._log("rearmed", soc=self.regulator.soc)
            if cmd is not None:
                if (self.regulator.settings.warm_w > 0 and last is not None
                        and {last.kind, cmd.kind} == {"charge", "discharge"}):
                    # With the ready floor on, only a turn between charge and discharge passes 0 W: log each one.
                    self._log("cross", from_w=last.signed_w, to_w=cmd.signed_w)
                self.hass.async_create_task(self._publish(cmd))
        self._notify()
        return cmd

    def _log(self, kind: str, **fields) -> None:
        if self._steplog is not None:
            self._steplog.write({"k": kind, "t": round(time.time(), 3), **fields})

    @callback
    def _on_request(self, msg) -> None:
        self._log("cmd", topic=msg.topic, p=msg.payload)

    @callback
    def _on_log_entity(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        self._log("e", e=event.data["entity_id"], s=None if state is None else state.state,
                  a={} if state is None else dict(state.attributes))

    def _gap(self, what: str, missing: bool) -> None:
        """Logs a gap in an input once when it starts and once when it ends."""
        if missing and what not in self._gaps:
            self._gaps.add(what)
            self._log("gap", what=what, missing=True)
            _LOGGER.warning("Battery controller %s missing, holding the last command", what)
        elif not missing and what in self._gaps:
            self._gaps.discard(what)
            self._log("gap", what=what, missing=False)
            _LOGGER.info("Battery controller %s back", what)

    def _unknown_policy(self, reason: str) -> None:
        """Logs a policy state the controller does not know once per value, and once when it clears."""
        if reason == self._unknown_policy_reason:
            return
        self._unknown_policy_reason = reason
        self._log("gap", what="policy state", missing=bool(reason), reason=reason or None)
        if reason:
            _LOGGER.warning("Battery controller releasing the inverter: %s", reason)
        else:
            _LOGGER.info("Battery controller policy state known again")

    def _fail(self, what: str, message: str) -> None:
        if what not in self._failing:
            self._failing.add(what)
            _LOGGER.exception(message)

    def _recover(self, what: str) -> None:
        if what in self._failing:
            self._failing.discard(what)
            _LOGGER.info("Battery controller %s works again", what)

    async def _publish(self, cmd: Command) -> None:
        if self.regulator.sent is not None and self.regulator.sent is not cmd:
            # Superseded before its task ran, as by a release from the switch: the newer command owns the inverter.
            return
        self._cmd_seq = self._cmd_seq % SEQ_MAX + 1
        seq = self._cmd_seq
        base = self.config.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        if cmd.kind == "normal":
            payload = {"void": 0, "seq": seq}
        else:
            soc = cmd.soc
            if cmd.kind == "charge" and soc == 100:
                soc = int(self.regulator.settings.full_register) - 150  # the firmware sends 101 to 105 as 251 to 255
            payload = {"watts": cmd.watts, "socPercent": soc, "duration": cmd.duration, "seq": seq}
            if cmd.power_only:
                payload["powerOnly"] = 1  # older firmware ignores it and writes the whole block
        # The Pico reads at most 127 bytes of payload; these stay under 90.
        try:
            await mqtt.async_publish(self.hass, f"{base}/request/set/{cmd.kind}", json.dumps(payload, separators=(",", ":")))
        except Exception:
            # Not sent: the regulator sends it again in full after the minimum gap.
            if self.regulator.sent is cmd:
                self.regulator.fail()
            self._fail("publish", "Battery controller could not publish, retrying")
            return
        self._recover("publish")
        self.last_command, self.last_sent, self.last_seq = cmd, dt_util.utcnow(), seq
        self._sent_mono = {seq: time.monotonic()}
        self._sent_times.append(time.monotonic())
        self._responses(self._response.command(
            time.monotonic(), None if cmd.kind == "normal" else cmd.signed_w, self._battery_w))
        self._notify()

    @callback
    def _on_response(self, msg) -> None:
        self._log("ack", topic=msg.topic, p=msg.payload)
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(data, dict):
            return  # anything may publish on the response topic; [] or 1 has no fields
        seq = data.get("seq")
        if seq is not None:
            self._seq_echo = True
        if seq is not None and seq == self.last_seq:
            self._answered_seq = seq
        sent = self._sent_mono.get(seq)
        self.ack = {
            "ack_seq": seq,
            "ack_status": data.get("responseStatus"),
            "ack_detail": data.get("failureDetail"),
            "ack_after_ms": None if sent is None else round((time.monotonic() - sent) * 1000),
            "ack_write": data.get("write"),  # power or block; older firmware sends neither
        }
        status = data.get("responseStatus")
        ok = isinstance(status, str) and status in SET_SUCCESS
        # Only the newest command's response counts, and not once the regulator has chosen another.
        newest = seq is not None and seq == self.last_seq and self.regulator.sent is self.last_command
        if not ok:
            _LOGGER.warning("Alpha2MQTT refused %s: %s", msg.topic, msg.payload)
            if newest:
                self.regulator.fail()
        elif newest:
            self.regulator.ack(time.monotonic(), power_write=data.get("write") == "power")
        self._notify()

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()
