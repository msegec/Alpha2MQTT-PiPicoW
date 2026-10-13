"""On and off for the controller. Off by default; turning it off hands the inverter back at once."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([EnableSwitch(entry)])


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
