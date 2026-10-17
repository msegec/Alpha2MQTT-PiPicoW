"""Alpha2MQTT battery controller: holds the grid near a target by choosing dispatch watts each sample."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .controller import (
    CONF_BASE_TOPIC,
    CONF_CORRECTION_ENTITY,
    CONF_GRID_SCALE,
    CONF_GRID_SCALE_ENTITY,
    CONF_GRID_SOURCE,
    CONF_LOAD_ENTITY,
    CONF_MAX_CHARGE_ENTITY,
    CONF_MAX_DISCHARGE_ENTITY,
    CONF_POLICY_ENTITY,
    CONF_STATE_TOPIC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_POLICY_ENTITY,
    DEFAULT_STATE_TOPIC,
    DOMAIN,
    GRID_SOURCES,
    Controller,
)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_STATE_TOPIC, default=DEFAULT_STATE_TOPIC): cv.string,
                vol.Optional(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): cv.string,
                vol.Optional(CONF_POLICY_ENTITY, default=DEFAULT_POLICY_ENTITY): cv.entity_id,
                vol.Optional(CONF_GRID_SOURCE, default="clamp"): vol.In(GRID_SOURCES),
                vol.Optional(CONF_GRID_SCALE, default=1.0): vol.Coerce(float),
                vol.Optional(CONF_GRID_SCALE_ENTITY): cv.entity_id,
                vol.Optional(CONF_CORRECTION_ENTITY): cv.entity_id,
                vol.Optional(CONF_LOAD_ENTITY): cv.entity_id,
                vol.Optional(CONF_MAX_CHARGE_ENTITY): cv.entity_id,
                vol.Optional(CONF_MAX_DISCHARGE_ENTITY): cv.entity_id,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(config[DOMAIN]))
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not available yet")  # Home Assistant retries the setup
    controller = Controller(hass, dict(entry.data), entry.entry_id)
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
