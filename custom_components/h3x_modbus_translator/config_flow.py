"""Editable network configuration, without ever changing inverter or Shelly settings."""

from __future__ import annotations

from dataclasses import asdict

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, NAME
from .model import Settings


def schema(defaults=None):
    defaults = {**asdict(Settings("shelly-pro3em.local")), **(defaults or {})}
    fields = {}
    for key in ("host", "listen_host", "allowed_clients"):
        fields[vol.Required(key, default=defaults[key])] = str
    for key, low, high in (
        ("source_port", 1, 65535),
        ("listen_port", 1024, 65535),
        ("source_unit", 1, 247),
        ("meter_unit", 1, 247),
    ):
        fields[vol.Required(key, default=defaults[key])] = vol.All(
            vol.Coerce(int), vol.Range(min=low, max=high)
        )
    for key, low, high in (("poll_seconds", 1, 30), ("stale_seconds", 3, 120)):
        fields[vol.Required(key, default=defaults[key])] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=low,
                max=high,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
    fields[vol.Required("reverse_power", default=defaults["reverse_power"])] = bool
    return vol.Schema(fields)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                settings = Settings(**user_input)
            except ValueError:
                errors["base"] = "invalid_settings"
            else:
                # One translator per HA instance keeps the dashboard and listener unambiguous.
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=asdict(settings))
        return self.async_show_form(step_id="user", data_schema=schema(user_input), errors=errors)


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        defaults = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            try:
                settings = Settings(**user_input)
            except ValueError:
                errors["base"] = "invalid_settings"
            else:
                return self.async_create_entry(title="", data=asdict(settings))
        return self.async_show_form(
            step_id="init", data_schema=schema(user_input or defaults), errors=errors
        )
