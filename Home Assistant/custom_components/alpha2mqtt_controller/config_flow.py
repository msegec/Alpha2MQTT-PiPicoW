"""Set up from the UI, or imported from YAML. One controller per Alpha2MQTT base topic, so a new state topic edits it.
Configure on the integration shows the same form, filled in; saving it replaces the entry's data and reloads the
controller, so a changed entity or topic takes effect without a restart."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .controller import (
    CONF_BASE_TOPIC,
    CONF_CORRECTION_ENTITY,
    CONF_GRID_SCALE,
    CONF_GRID_SCALE_ENTITY,
    CONF_GRID_SOURCE,
    CONF_LOAD_ENTITY,
    CONF_LOG_DIR,
    CONF_LOG_ENTITIES,
    CONF_MAX_CHARGE_ENTITY,
    CONF_MAX_DISCHARGE_ENTITY,
    CONF_POLICY_ENTITY,
    CONF_STATE_TOPIC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_POLICY_ENTITY,
    DEFAULT_STATE_TOPIC,
    DOMAIN,
    GRID_SOURCES,
)

_TEXT = selector.TextSelector()
_ENTITY = selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"]))

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STATE_TOPIC, default=DEFAULT_STATE_TOPIC): _TEXT,
        vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): _TEXT,
        vol.Required(CONF_POLICY_ENTITY, default=DEFAULT_POLICY_ENTITY): selector.EntitySelector(),
        vol.Required(CONF_GRID_SOURCE, default="clamp"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=GRID_SOURCES, translation_key=CONF_GRID_SOURCE)
        ),
        vol.Required(CONF_GRID_SCALE, default=1.0): selector.NumberSelector(
            selector.NumberSelectorConfig(min=-100, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_GRID_SCALE_ENTITY): _ENTITY,
        vol.Optional(CONF_CORRECTION_ENTITY): _ENTITY,
        vol.Optional(CONF_LOAD_ENTITY): _ENTITY,
        vol.Optional(CONF_MAX_CHARGE_ENTITY): _ENTITY,
        vol.Optional(CONF_MAX_DISCHARGE_ENTITY): _ENTITY,
        vol.Optional(CONF_LOG_DIR): _TEXT,
        vol.Optional(CONF_LOG_ENTITIES, default=[]): selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True)
        ),
    }
)


class Alpha2MqttControllerFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> Alpha2MqttControllerOptionsFlow:
        return Alpha2MqttControllerOptionsFlow(entry)

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_BASE_TOPIC])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Battery controller", data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA)

    async def async_step_import(self, import_data: dict) -> ConfigFlowResult:
        # YAML stays the owner: a changed file updates the existing entry.
        await self.async_set_unique_id(import_data[CONF_BASE_TOPIC])
        self._abort_if_unique_id_configured(updates=import_data)
        return self.async_create_entry(title="Battery controller", data=import_data)


class Alpha2MqttControllerOptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # The entry's data stays the one owner of the configuration, as it is for YAML and the first setup.
            self.hass.config_entries.async_update_entry(self._entry, data=user_input)
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="init", data_schema=self.add_suggested_values_to_schema(SCHEMA, self._entry.data))
