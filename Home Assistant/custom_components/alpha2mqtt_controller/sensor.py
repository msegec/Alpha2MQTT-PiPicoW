"""What the controller sees and does. Updated on every regulator step. States are recorded; the per-step
attributes are not, so each step adds one small row rather than a new attributes row."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback


def _round(x):
    return None if x is None else round(x)


def _last_command(c):
    cmd = c.last_command
    if cmd is None:
        return None
    return cmd.kind if cmd.kind == "normal" else f"{cmd.kind} {cmd.watts} W"


# key, name, power, value, attributes
SENSORS = [
    ("status", "Status", False, lambda c: c.status, None),
    ("grid", "Grid estimate", True, lambda c: _round(c.grid_w), None),
    ("error", "Error", True, lambda c: _round(c.regulator.error_w), None),
    ("output", "Output", True, lambda c: _round(c.regulator.output_w), lambda c: {
        "request": _round(c.regulator.request_w),
        "integral": _round(c.regulator.integral_w),
        "feed_forward": _round(c.regulator.ff_w),
    }),
    ("commands", "Commands in the last hour", False, lambda c: c.commands_last_hour, None),
    ("last_command", "Last command", False, _last_command, lambda c: {
        "soc": c.last_command.soc if c.last_command else None,
        "seq": c.last_seq,
        "sent": c.last_sent.isoformat() if c.last_sent else None,
        **c.ack,
    }),
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
        self._attr_native_value = self._value(self._controller)
        if self._attributes:
            self._attr_extra_state_attributes = self._attributes(self._controller)
        self.async_write_ha_state()
