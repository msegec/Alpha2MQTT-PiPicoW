"""What the controller sees and does. The regulator steps every second, but control never reads these
entities, so a power sensor writes only when it moves by MOVE_W or every EVERY_S. Each write is a recorder
row and a push to every open dashboard; at every step it was about 3,900 rows an hour per sensor."""

from __future__ import annotations

import time

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

MOVE_W = 25
EVERY_S = 10.0


def _round(x):
    return None if x is None else round(x)


def _last_command(c):
    cmd = c.last_command
    if cmd is None:
        return None
    return cmd.kind if cmd.kind == "normal" else f"{cmd.kind} {cmd.watts} W"


# key, name, power, value, attributes
SENSORS = [
    # How long an input has been missing, and the settings no number exposes. Each changes only at an edge.
    ("status", "Status", False, lambda c: c.status, lambda c: {
        "policy_gap_since": c.policy_gap_since,
        "sample_gap_since": c.sample_gap_since,
        "integral_band_w": c.regulator.settings.integral_band_w,
        "integral_limit_w": c.regulator.settings.integral_limit_w,
        "lease_s": c.regulator.settings.duration_s,
    }),
    ("grid", "Grid estimate", True, lambda c: _round(c.grid_w), None),
    ("error", "Error", True, lambda c: _round(c.regulator.error_w), None),
    ("output", "Output", True, lambda c: _round(c.regulator.output_w), lambda c: {
        "request": _round(c.regulator.request_w),
        "integral": _round(c.regulator.integral_w),
        "feed_forward": _round(c.regulator.ff_w),
        # The proportional term, and the output before the slew. These move with every step, so they ride on this
        # power sensor's own writes (MOVE_W or EVERY_S) and add none.
        "p": _round(c.regulator.p_w),
        "u": _round(c.regulator.u_w),
        "slewed": c.regulator.slewed,
    }),
    # What holds the output (floor, refused, full, warm, a cap, turn_round, none), and the three numbers that say
    # why the grid sits off target: the battery as read, the watts the inverter was told, and the integral trim.
    # The caps the request was clipped to, the direction and when it last turned, and the refusal clock and latch.
    ("limit", "Limit", False, lambda c: c.regulator.limit if c.enabled else None, lambda c: {
        "lo": _round(c.regulator.lo_w),
        "hi": _round(c.regulator.hi_w),
        "direction": c.regulator.direction,
        "direction_since": c.wall("direction", c.regulator.direction_since),
        "refusal_since": c.wall("refusal", c.regulator.refusal_since),
        "refused_soc": c.regulator.refused_soc,
    }),
    ("battery", "Battery", True, lambda c: _round(c.regulator.battery_w), None),
    ("sent", "Sent", True, lambda c: c.last_command.signed_w if c.last_command else None, None),
    ("integral", "Integral", True, lambda c: _round(c.regulator.integral_w), None),
    ("commands", "Commands in the last hour", False, lambda c: c.commands_last_hour, None),
    ("last_command", "Last command", False, _last_command, lambda c: {
        "soc": c.last_command.soc if c.last_command else None,
        "seq": c.last_seq,
        "sent": c.last_sent.isoformat() if c.last_sent else None,
        **c.ack,
    }),
    # Seconds the battery took to reach 80 % of the last large step, unknown when it did not within the timeout.
    # The count is its own sensor so it has history and can raise an alert; attributes are not recorded.
    ("step_response", "Step response time", False, lambda c: (c.last_response or {}).get("t80_s"),
     lambda c: dict(c.last_response or {})),
    ("slow_responses", "Slow step responses today", False, lambda c: c.slow_responses_today, None),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add(ControllerSensor(entry, *s) for s in SENSORS)


class ControllerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, entry: ConfigEntry, key, name, power, value, attributes) -> None:
        self._controller = entry.runtime_data
        self._value, self._attributes = value, attributes
        self._power = power
        self._written: tuple[float, object] = (float("-inf"), object())
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = self._controller.device_info
        if power:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._controller.add_listener(self._update))
        self._update()

    @callback
    def _update(self) -> None:
        value = self._value(self._controller)
        t_last, last = self._written
        now = time.monotonic()
        if value == last and not self._attributes and not self._power:
            return
        if self._power and isinstance(value, (int, float)) and isinstance(last, (int, float)):
            if abs(value - last) < MOVE_W and now - t_last < EVERY_S:
                return
        self._written = (now, value)
        self._attr_native_value = value
        if self._attributes:
            self._attr_extra_state_attributes = self._attributes(self._controller)
        self.async_write_ha_state()
