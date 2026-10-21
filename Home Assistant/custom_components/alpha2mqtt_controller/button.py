"""Forget learned offsets: zeroes the long term trim table and saves it, so learning starts again from the static table."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .number import BASIC


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([ForgetTrimButton(entry)])


class ForgetTrimButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Forget learned offsets"
    _attr_icon = "mdi:table-remove"

    def __init__(self, entry: ConfigEntry) -> None:
        self._controller = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_forget_trim"
        self._attr_device_info = self._controller.device_info
        self._attr_extra_state_attributes = {"group": BASIC}

    async def async_press(self) -> None:
        await self._controller.async_forget_trim()
