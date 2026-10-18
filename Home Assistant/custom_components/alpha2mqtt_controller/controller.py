"""Home Assistant glue: feeds the regulator from MQTT and entities, publishes its commands."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .regulator import FIXED, HOLD, REGULATE, RELEASE, Command, Policy, Regulator, estimate_grid
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
CONF_LOAD_ENTITY = "load_entity"
CONF_MAX_CHARGE_ENTITY = "max_charge_entity"
CONF_MAX_DISCHARGE_ENTITY = "max_discharge_entity"

DEFAULT_STATE_TOPIC = "Alpha2MQTT/state/fast"  # every second; older firmware has only state/second/ten
DEFAULT_BASE_TOPIC = "Alpha2MQTT"
DEFAULT_POLICY_ENTITY = "sensor.battery_auto_action"
DEFAULT_MAX_W = 3000.0
# clamp: scale x grid clamp + correction. estimate: correction - battery, for a clamp that cannot see the house.
GRID_SOURCES = ["clamp", "estimate"]

# A plausible reading is finite and under this. A kW value read as W, or a NaN, would otherwise drive the battery.
MAX_PLAUSIBLE_W = 20000.0
# Net demand rewrites every few seconds, 26 s at most over a measured day. Older than this, a plug or
# template has frozen and its last value is not the house any more.
CORRECTION_MAX_AGE_S = 90.0
STEPLOG_EVERY_S = 5.0
STEPLOG_MOVE_W = 25.0

GRID_KEY = "REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1"
BATTERY_KEY = "REG_BATTERY_HOME_R_BATTERY_POWER"


def _plausible(x: float) -> bool:
    return math.isfinite(x) and abs(x) <= MAX_PLAUSIBLE_W


def _number(hass: HomeAssistant, entity_id: str | None, max_age_s: float | None = None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    try:
        x = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    if not _plausible(x):
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
        self.grid_w: float | None = None
        self._raw_grid: float | None = None
        self._battery_w: float | None = None
        self._sample_seq = None
        self._cmd_seq = 0
        self.last_command: Command | None = None
        self.last_sent: datetime | None = None
        self.last_seq: int | None = None
        self.ack: dict = {}
        self._sent_mono: dict[int, float] = {}
        self._sent_times: deque[float] = deque()
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._steplog: StepLog | None = None
        self._logged: tuple[float, str, float | None] = (float("-inf"), "", None)
        self._failing: str | None = None  # what is failing now, so it is logged once, not every second

    @property
    def status(self) -> str:
        return self.regulator.status if self.enabled else "disabled"

    @property
    def commands_last_hour(self) -> int:
        while self._sent_times and time.monotonic() - self._sent_times[0] > 3600:
            self._sent_times.popleft()
        return len(self._sent_times)

    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    async def async_start(self) -> None:
        cfg = self.config
        base = cfg.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        try:
            self._steplog = await self.hass.async_add_executor_job(
                StepLog, self.hass.config.path(DOMAIN, file_name(base)))
        except OSError as err:
            _LOGGER.warning("Battery controller step log is off: %s", err)
        self._unsubs.append(await mqtt.async_subscribe(
            self.hass, cfg.get(CONF_STATE_TOPIC, DEFAULT_STATE_TOPIC), self._on_state))
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{base}/response/set/+", self._on_response))
        watched = [cfg.get(k) for k in (CONF_POLICY_ENTITY, CONF_GRID_SCALE_ENTITY, CONF_CORRECTION_ENTITY,
                                        CONF_LOAD_ENTITY, CONF_MAX_CHARGE_ENTITY, CONF_MAX_DISCHARGE_ENTITY)]
        self._unsubs.append(async_track_state_change_event(
            self.hass, [e for e in watched if e], self._on_entity))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=1)))
        load = _number(self.hass, cfg.get(CONF_LOAD_ENTITY))
        if load is not None:
            self.regulator.update_load(time.monotonic(), load)

    async def async_stop(self) -> None:
        try:
            await self.async_set_enabled(False)
        finally:
            while self._unsubs:
                self._unsubs.pop()()
            if self._steplog is not None:
                steplog, self._steplog = self._steplog, None
                await self.hass.async_add_executor_job(steplog.close)

    async def async_set_enabled(self, enabled: bool) -> None:
        was, self.enabled = self.enabled, enabled
        if was and not enabled:
            # Hand the inverter back now rather than waiting for the lease to run out. Off is off even
            # when this publish fails: the lease then returns the inverter to normal within two minutes.
            cmd = self.regulator.step(time.monotonic(), Policy(RELEASE), self._inputs_ok())
            if cmd is not None:
                await self._publish(cmd)
        self._notify()

    def _grid_estimate(self) -> float | None:
        """Grid power as the utility meter would see it, from the clamp or from the correction alone."""
        if self._raw_grid is None:
            return None
        if self.config.get(CONF_GRID_SOURCE) == "estimate":
            correction = _number(self.hass, self.config.get(CONF_CORRECTION_ENTITY), CORRECTION_MAX_AGE_S)
            return None if correction is None else estimate_grid(correction, self._battery_w)
        scale = _number(self.hass, self.config.get(CONF_GRID_SCALE_ENTITY))
        if scale is None:
            if self.config.get(CONF_GRID_SCALE_ENTITY):
                return None
            scale = float(self.config.get(CONF_GRID_SCALE, 1.0))
        correction = 0.0
        if self.config.get(CONF_CORRECTION_ENTITY):
            correction = _number(self.hass, self.config[CONF_CORRECTION_ENTITY])
            if correction is None:
                return None
        return estimate_grid(correction, self._battery_w, self._raw_grid, scale)

    def _inputs_ok(self) -> bool:
        return self._grid_estimate() is not None

    def _policy(self) -> Policy:
        state = self.hass.states.get(self.config.get(CONF_POLICY_ENTITY, DEFAULT_POLICY_ENTITY))
        if state is None:
            return Policy(RELEASE)
        if state.state == "hold":
            return Policy(HOLD)
        if state.state not in ("charge", "discharge"):
            return Policy(RELEASE)
        try:
            soc = int(float(state.attributes.get("soc_target")))
        except (TypeError, ValueError, OverflowError):
            return Policy(RELEASE)
        if str(state.attributes.get("fixed")).lower() == "true":
            # A manual override: the policy's watts, not regulation.
            try:
                watts = float(state.attributes.get("watts"))
            except (TypeError, ValueError):
                return Policy(RELEASE)
            if not _plausible(watts):
                return Policy(RELEASE)
            if state.state == "charge":
                return Policy(FIXED, fixed_w=-watts, charge_soc=soc)
            return Policy(FIXED, fixed_w=watts, discharge_soc=soc)
        charge_cap = _number(self.hass, self.config.get(CONF_MAX_CHARGE_ENTITY))
        discharge_cap = _number(self.hass, self.config.get(CONF_MAX_DISCHARGE_ENTITY))
        charge_cap = DEFAULT_MAX_W if charge_cap is None else charge_cap
        discharge_cap = DEFAULT_MAX_W if discharge_cap is None else discharge_cap
        # With both, the policy also allows the other direction, so a load edge turns the battery round
        # here, in the next step, instead of waiting for the policy's next decision. other_soc is that
        # direction's limit.
        both = str(state.attributes.get("both")).lower() == "true"
        try:
            other_soc = int(float(state.attributes.get("other_soc")))
        except (TypeError, ValueError, OverflowError):
            both = False
        if state.state == "charge":
            # The policy's grid_offset moves the grid target while charging: + tops up from the grid.
            try:
                offset = float(state.attributes.get("grid_offset", 0))
            except (TypeError, ValueError):
                offset = 0.0
            if not _plausible(offset):
                offset = 0.0
            return Policy(REGULATE, max_charge_w=charge_cap, charge_soc=soc, target_offset_w=offset,
                          max_discharge_w=discharge_cap if both else 0.0, discharge_soc=other_soc if both else 10)
        return Policy(REGULATE, max_discharge_w=discharge_cap, discharge_soc=soc,
                      max_charge_w=charge_cap if both else 0.0, charge_soc=other_soc if both else 100)

    @callback
    def _on_state(self, msg) -> None:
        try:
            data = json.loads(msg.payload)
            raw_grid, battery = float(data[GRID_KEY]), float(data[BATTERY_KEY])
        except (ValueError, KeyError, TypeError):
            return
        if not (_plausible(raw_grid) and _plausible(battery)):
            return
        seq = data.get("seq")
        if seq is not None and seq == self._sample_seq:
            return  # a repeat, not a new reading
        self._sample_seq = seq
        self._raw_grid, self._battery_w = raw_grid, battery
        self._measure(fresh=True)

    def _measure(self, fresh: bool) -> None:
        self.grid_w = self._grid_estimate()
        if self.grid_w is not None and self._battery_w is not None:
            self.regulator.update_grid(time.monotonic(), self.grid_w, self._battery_w, fresh=fresh)
        self._run()

    @callback
    def _on_entity(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self.config.get(CONF_LOAD_ENTITY):
            load = _number(self.hass, entity_id)
            if load is not None:
                self.regulator.update_load(time.monotonic(), load)
        if entity_id in (self.config.get(CONF_CORRECTION_ENTITY), self.config.get(CONF_GRID_SCALE_ENTITY)):
            self._measure(fresh=False)
        else:
            self._run()

    @callback
    def _on_tick(self, _now) -> None:
        self._run()

    @callback
    def _run(self) -> None:
        if self.enabled:
            now = time.monotonic()
            try:
                cmd = self.regulator.step(now, self._policy(), self._inputs_ok())
            except Exception:  # one bad value must not stop regulation silently
                self._fail("step", "Battery controller step failed, handing the inverter back")
                cmd = self.regulator.step(now, Policy(RELEASE))
            else:
                self._recover("step")
            if cmd is not None:
                self.hass.async_create_task(self._publish(cmd))
            if self._steplog is not None:
                self._log_step(cmd)
        self._notify()

    def _fail(self, what: str, message: str) -> None:
        if self._failing != what:
            self._failing = what
            _LOGGER.exception(message)

    def _recover(self, what: str) -> None:
        if self._failing == what:
            self._failing = None
            _LOGGER.info("Battery controller %s works again", what)

    def _log_step(self, cmd: Command | None) -> None:
        # Commands, status changes and grid moves go in at once; quiet steps every few seconds.
        # Every step at 1 Hz was about 100 MB a day of SD card writes per controller.
        r = self.regulator
        t_last, status_last, grid_last = self._logged
        now = time.monotonic()
        moved = r.grid_w is not None and (grid_last is None or abs(r.grid_w - grid_last) >= STEPLOG_MOVE_W)
        if cmd is None and r.status == status_last and not moved and now - t_last < STEPLOG_EVERY_S:
            return
        self._logged = (now, r.status, r.grid_w)
        watts = {k: None if v is None else round(v, 1) for k, v in (
            ("grid_w", r.grid_w), ("battery_w", r.battery_w), ("load_w", r.load_w), ("error_w", r.error_w),
            ("integral_w", r.integral_w), ("ff_w", r.ff_w), ("request_w", r.request_w), ("output_w", r.output_w))}
        self._steplog.write({
            "t": round(time.time(), 2), "status": r.status, **watts,
            "sent": None if cmd is None else {"kind": cmd.kind, "watts": cmd.watts, "soc": cmd.soc,
                                              "power_only": cmd.power_only},
        })

    async def _publish(self, cmd: Command) -> None:
        self._cmd_seq += 1
        seq = self._cmd_seq
        base = self.config.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        if cmd.kind == "normal":
            payload = {"void": 0, "seq": seq}
        else:
            payload = {"watts": cmd.watts, "socPercent": cmd.soc, "duration": cmd.duration, "seq": seq}
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
        self._notify()

    @callback
    def _on_response(self, msg) -> None:
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        seq = data.get("seq")
        sent = self._sent_mono.get(seq)
        self.ack = {
            "ack_seq": seq,
            "ack_status": data.get("responseStatus"),
            "ack_detail": data.get("failureDetail"),
            "ack_after_ms": None if sent is None else round((time.monotonic() - sent) * 1000),
            "ack_write": data.get("write"),  # power or block; older firmware sends neither
        }
        ok = "success" in str(data.get("responseStatus")).lower()
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
