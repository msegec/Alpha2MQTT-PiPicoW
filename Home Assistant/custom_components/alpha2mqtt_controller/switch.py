"""On and off for the controller, and for each on or off setting. The controller is off by default; turning it off
hands the inverter back at once."""

from __future__ import annotations

from dataclasses import fields

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .number import BASIC
from .regulator import Settings

# key, name, group. Each writes a Settings flag, goes in the step log and survives a restart, as a number does.
SWITCHES = [
    ("ff_guard", "Feed forward guard", BASIC),  # off: the solar lead may turn the battery round
]

_DEFAULTS = {f.name: f.default for f in fields(Settings)}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([EnableSwitch(entry), *(SettingSwitch(entry, *w) for w in SWITCHES)])


class EnableSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Enabled"
    _attr_icon = "mdi:home-battery"

    def __init__(self, entry: ConfigEntry) -> None:
        self._controller = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = self._controller.device_info

    @property
    def is_on(self) -> bool:
        return self._controller.enabled

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            await self._controller.async_set_enabled(True)

    async def async_turn_on(self, **kwargs) -> None:
        await self._controller.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._controller.async_set_enabled(False)
        self.async_write_ha_state()


class SettingSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key, name, group) -> None:
        self._controller = entry.runtime_data
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = self._controller.device_info
        self._attr_extra_state_attributes = {"group": group}

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._controller.regulator.settings, self._key))

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        on = last.state == "on" if last is not None and last.state in ("on", "off") else _DEFAULTS[self._key]
        self._controller.set_setting(self._key, on)

    async def async_turn_on(self, **kwargs) -> None:
        self._controller.set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._controller.set_setting(self._key, False)
        self.async_write_ha_state()
