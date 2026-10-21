"""Tuning numbers. Each writes straight into the regulator's settings, goes in the step log and survives a restart."""

from __future__ import annotations

from dataclasses import fields

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .regulator import Settings

# The section each setting shows in: what to tune, what keeps the battery and inverter safe, and the rest.
BASIC, SAFETY, ADVANCED = "Basic", "Safety", "Advanced"

# key, name, min, max, step, unit, group
NUMBERS = [
    ("target_w", "Grid target", -500, 500, 5, "W", BASIC),
    ("kp", "Proportional gain", 0, 1.5, 0.05, None, BASIC),
    ("ki", "Integral gain", 0, 0.2, 0.005, None, BASIC),
    ("kff", "Solar feed forward gain", 0, 2, 0.05, None, BASIC),
    ("ki_import_x", "Integral import factor", 1, 5, 0.5, None, ADVANCED),  # 1 is symmetric
    ("solar_ff_min_w", "Feed forward minimum solar", 0, 1000, 10, "W", ADVANCED),  # 0 is off
    ("deadband_w", "Deadband", 0, 200, 5, "W", BASIC),
    ("smoothing_s", "Smoothing", 0, 300, 0.5, "s", ADVANCED),
    ("slew_w_per_s", "Slew limit", 50, 5000, 50, "W/s", ADVANCED),
    ("write_threshold_w", "Write threshold", 10, 500, 10, "W", ADVANCED),
    ("min_interval_s", "Minimum command interval", 2, 30, 0.5, "s", ADVANCED),  # closer, commands queue behind the writes
    ("keepalive_s", "Keepalive", 10, 100, 0.5, "s", ADVANCED),
    ("flip_hysteresis_w", "Charge discharge margin", 0, 1000, 10, "W", ADVANCED),
    ("flip_dwell_s", "Charge discharge dwell", 0, 600, 0.5, "s", ADVANCED),
    ("stale_after_s", "Stale after", 3, 100, 0.5, "s", SAFETY),
    ("power_write_gap_s", "Power write gap", 0, 5, 0.5, "s", ADVANCED),  # 0 is off: every command is a full block write
    ("warm_w", "Keep warm", 0, 500, 5, "W", BASIC),  # the ready floor; 0 is off: the battery idles at 0 W between loads
    ("offset_comp", "Offset correction", 0, 1.5, 0.05, None, BASIC),  # 0 is off, 1 takes the measured offset off in full
    ("spike_confirm_w", "Spike confirm", 0, 3000, 50, "W", ADVANCED),  # 0 is off: every jump in demand goes out at once
    ("integral_reset_w", "Integral reset step", 0, 2000, 50, "W", ADVANCED),  # 0 is off: the integral carries across levels
    ("import_trim_gap_s", "Import trim wait", 0, 30, 0.5, "s", ADVANCED),  # 6 is the trim gap: the old rule
    ("trim_rate", "Offset learning rate", 0, 0.2, 0.005, None, ADVANCED),  # share of the integral moved per settled second; 0 off
    ("trim_limit_w", "Learned offset limit", 0, 400, 10, "W", SAFETY),  # bound on each learned node
    ("trim_settle_s", "Offset learning settle", 2, 120, 1, "s", ADVANCED),  # command unchanged this long before learning
    ("trim_band_w", "Offset learning band", 10, 300, 5, "W", ADVANCED),  # raw grid error must be within this to learn
    ("trim_min_w", "Offset learning minimum", 20, 1000, 10, "W", ADVANCED),  # smaller setpoints teach nothing
    ("trim_max_soc", "Offset learning top SOC", 50, 100, 1, "%", ADVANCED),  # BMS taper above this teaches nothing
    ("arrive_restart_w", "Arrival restart step", 0, 500, 10, "W", ADVANCED),  # 0 is off: the arrival wait runs from the send
    ("full_register", "Full charge register", 250, 255, 1, None, SAFETY),  # dispatch SOC register for a 100 % charge
    ("floor_margin_soc", "Floor margin", 0, 5, 0.1, "%", SAFETY),  # no discharge this close above the policy SOC
    ("refused_w", "Refusal power", 0, 200, 5, "W", SAFETY),  # a battery under this gives nothing
    ("refused_after_s", "Refusal time", 5, 120, 1, "s", SAFETY),  # giving nothing this long is a refusal
    ("refused_hold_s", "Refusal hold", 10, 600, 10, "s", SAFETY),  # no discharge this long after one
    ("refused_ask_w", "Refusal minimum ask", 50, 1000, 10, "W", SAFETY),  # smaller asks are never refused
    ("refused_near_soc", "Refusal latch margin", 0, 10, 0.5, "%", SAFETY),  # a refusal this close to the floor latches
    ("rearm_soc", "Refusal rearm rise", 0.4, 5, 0.1, "%", SAFETY),  # a latched refusal ends on this SOC rise
    ("integral_limit_w", "Integral limit", 0, 1000, 10, "W", SAFETY),
    ("duration_s", "Dispatch lease", 60, 600, 10, "s", SAFETY),  # the inverter returns to normal after it
    ("min_send_gap_s", "Early send gap", 0.5, 10, 0.5, "s", ADVANCED),  # an acked import cut may go this soon
    ("max_integrate_s", "Integration gap limit", 1, 30, 1, "s", ADVANCED),  # a longer sample gap counts as this
    ("lag_w", "Arrival lag", 0, 500, 10, "W", ADVANCED),  # a battery this far short has not arrived
    ("arrive_s", "Arrival wait", 0, 120, 1, "s", ADVANCED),  # the integral waits for arrival this long at most
    ("small_trim_w", "Small trim", 0, 200, 5, "W", ADVANCED),  # smaller moves are trims
    ("small_trim_gap_s", "Small trim wait", 0, 30, 0.5, "s", ADVANCED),  # trims wait this long since the last command
    ("warm_band_w", "Keep warm band", 0, 300, 10, "W", ADVANCED),  # keep warm replaces a charge smaller than this
    ("solar_deadband_w", "Solar feed forward deadband", 0, 200, 5, "W", ADVANCED),  # smaller solar steps are noise
    ("solar_max_gap_s", "Solar feed forward gap", 1, 15, 0.5, "s", ADVANCED),  # no feed forward over a longer gap
    ("integral_band_w", "Integral band", 0, 2000, 10, "W", ADVANCED),  # bigger errors are load steps: no integral
    ("command_step_w", "Command rounding", 1, 50, 1, "W", ADVANCED),  # commands go out in steps of this
]

_DEFAULTS = {f.name: f.default for f in fields(Settings)}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add(TuningNumber(entry, *n) for n in NUMBERS)


class TuningNumber(RestoreNumber):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, key, name, lo, hi, step, unit, group) -> None:
        self._controller = entry.runtime_data
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = self._controller.device_info
        self._attr_extra_state_attributes = {"group": group}  # so a dashboard can build its sections from the entities

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
