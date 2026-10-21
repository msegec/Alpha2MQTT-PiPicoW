"""Tuning numbers. Each writes straight into the regulator's settings, goes in the step log and survives a restart."""

from __future__ import annotations

from dataclasses import fields

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .regulator import Settings

# key, name, min, max, step, unit
NUMBERS = [
    ("target_w", "Grid target", -500, 500, 5, "W"),
    ("kp", "Proportional gain", 0, 1.5, 0.05, None),
    ("ki", "Integral gain", 0, 0.2, 0.005, None),
    ("kff", "Solar feed forward gain", 0, 2, 0.05, None),
    ("deadband_w", "Deadband", 0, 200, 5, "W"),
    ("smoothing_s", "Smoothing", 0, 300, 0.5, "s"),
    ("slew_w_per_s", "Slew limit", 50, 5000, 50, "W/s"),
    ("write_threshold_w", "Write threshold", 10, 500, 10, "W"),
    ("min_interval_s", "Minimum command interval", 2, 30, 0.5, "s"),  # closer, commands queue behind the writes
    ("keepalive_s", "Keepalive", 10, 100, 0.5, "s"),
    ("flip_hysteresis_w", "Charge discharge margin", 0, 1000, 10, "W"),
    ("flip_dwell_s", "Charge discharge dwell", 0, 600, 0.5, "s"),
    ("stale_after_s", "Stale after", 3, 100, 0.5, "s"),
    ("power_write_gap_s", "Power write gap", 0, 5, 0.5, "s"),  # 0 is off: every command is a full block write
    ("warm_w", "Keep warm", 0, 500, 5, "W"),  # the ready floor; 0 is off: the battery idles at 0 W between loads
]

_DEFAULTS = {f.name: f.default for f in fields(Settings)}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add(TuningNumber(entry, *n) for n in NUMBERS)


class TuningNumber(RestoreNumber):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, key, name, lo, hi, step, unit) -> None:
        self._controller = entry.runtime_data
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = self._controller.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        value = last.native_value if last and last.native_value is not None else _DEFAULTS[self._key]
        # A value saved before a range changed comes back as it was, so bring it inside.
        self._apply(min(max(value, self._attr_native_min_value), self._attr_native_max_value))

    def _apply(self, value: float) -> None:
        self._controller.set_setting(self._key, value)
        self._attr_native_value = value

    async def async_set_native_value(self, value: float) -> None:
        self._apply(value)
        self.async_write_ha_state()
