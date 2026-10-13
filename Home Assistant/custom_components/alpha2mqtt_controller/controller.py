"""Home Assistant glue: feeds the regulator from MQTT and entities, publishes its commands."""

from __future__ import annotations

import json
import logging
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

DEFAULT_STATE_TOPIC = "Alpha2MQTT/state/second/ten"
DEFAULT_BASE_TOPIC = "Alpha2MQTT"
DEFAULT_POLICY_ENTITY = "sensor.battery_auto_action"
DEFAULT_MAX_W = 3000.0
# clamp: scale x grid clamp + correction. estimate: correction - battery, for a clamp that cannot see the house.
GRID_SOURCES = ["clamp", "estimate"]

GRID_KEY = "REG_GRID_METER_R_TOTAL_ACTIVE_POWER_1"
BATTERY_KEY = "REG_BATTERY_HOME_R_BATTERY_POWER"


def _number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    try:
        return float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None


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
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.error("MQTT is not available, the battery controller will not run")
            return
        self._steplog = await self.hass.async_add_executor_job(
            StepLog, self.hass.config.path(DOMAIN, file_name(base)))
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
        await self.async_set_enabled(False)
        while self._unsubs:
            self._unsubs.pop()()
        if self._steplog is not None:
            steplog, self._steplog = self._steplog, None
            await self.hass.async_add_executor_job(steplog.close)

    async def async_set_enabled(self, enabled: bool) -> None:
        if not enabled and self.enabled:
            # Hand the inverter back now rather than waiting for the lease to run out.
            cmd = self.regulator.step(time.monotonic(), Policy(RELEASE), self._inputs_ok())
            if cmd is not None:
                await self._publish(cmd)
        self.enabled = enabled
        self._notify()

    def _grid_estimate(self) -> float | None:
        """Grid power as the utility meter would see it, from the clamp or from the correction alone."""
        if self._raw_grid is None:
            return None
        if self.config.get(CONF_GRID_SOURCE) == "estimate":
            correction = _number(self.hass, self.config.get(CONF_CORRECTION_ENTITY))
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
        except (TypeError, ValueError):
            return Policy(RELEASE)
        if str(state.attributes.get("fixed")).lower() == "true":
            # A manual override: the policy's watts, not regulation.
            try:
                watts = float(state.attributes.get("watts"))
            except (TypeError, ValueError):
                return Policy(RELEASE)
            if state.state == "charge":
                return Policy(FIXED, fixed_w=-watts, charge_soc=soc)
            return Policy(FIXED, fixed_w=watts, discharge_soc=soc)
        if state.state == "charge":
            cap = _number(self.hass, self.config.get(CONF_MAX_CHARGE_ENTITY))
            return Policy(REGULATE, max_charge_w=DEFAULT_MAX_W if cap is None else cap, charge_soc=soc)
        cap = _number(self.hass, self.config.get(CONF_MAX_DISCHARGE_ENTITY))
        return Policy(REGULATE, max_discharge_w=DEFAULT_MAX_W if cap is None else cap, discharge_soc=soc)

    @callback
    def _on_state(self, msg) -> None:
        try:
            data = json.loads(msg.payload)
            raw_grid, battery = float(data[GRID_KEY]), float(data[BATTERY_KEY])
        except (ValueError, KeyError, TypeError):
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
            cmd = self.regulator.step(time.monotonic(), self._policy(), self._inputs_ok())
            if cmd is not None:
                self.hass.async_create_task(self._publish(cmd))
            if self._steplog is not None:
                self._log_step(cmd)
        self._notify()

    def _log_step(self, cmd: Command | None) -> None:
        r = self.regulator
        watts = {k: None if v is None else round(v, 1) for k, v in (
            ("grid_w", r.grid_w), ("battery_w", r.battery_w), ("load_w", r.load_w), ("error_w", r.error_w),
            ("integral_w", r.integral_w), ("ff_w", r.ff_w), ("request_w", r.request_w), ("output_w", r.output_w))}
        self._steplog.write({
            "t": round(time.time(), 2), "status": r.status, **watts,
            "sent": None if cmd is None else {"kind": cmd.kind, "watts": cmd.watts, "soc": cmd.soc},
        })

    async def _publish(self, cmd: Command) -> None:
        self._cmd_seq += 1
        seq = self._cmd_seq
        base = self.config.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        if cmd.kind == "normal":
            payload = {"void": 0, "seq": seq}
        else:
            payload = {"watts": cmd.watts, "socPercent": cmd.soc, "duration": cmd.duration, "seq": seq}
        # The Pico reads at most 127 bytes of payload; these stay under 80.
        await mqtt.async_publish(self.hass, f"{base}/request/set/{cmd.kind}", json.dumps(payload, separators=(",", ":")))
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
        }
        if "success" not in str(data.get("responseStatus")).lower():
            _LOGGER.warning("Alpha2MQTT refused %s: %s", msg.topic, msg.payload)
        self._notify()

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()
