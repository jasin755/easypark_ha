"""Config and options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .agent import AgentClient, AgentError
from .const import (
    CONF_HOST,
    CONF_MAX_PRICE,
    CONF_PLATES,
    CONF_PORT,
    CONF_RETURN_PACKAGE,
    CONF_TOKEN,
    CONF_ZONE,
    DEFAULT_MAX_PRICE,
    DEFAULT_PORT,
    DEFAULT_RETURN_PACKAGE,
    DOMAIN,
    EASYPARK_PACKAGE,
)
from .manager import parse_plates


def _connection_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
        vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, "")): str,
    }


async def _validate(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    if not parse_plates(user_input[CONF_PLATES]):
        return {CONF_PLATES: "no_plates"}
    agent = AgentClient(
        async_get_clientsession(hass),
        user_input[CONF_HOST].strip(),
        user_input[CONF_PORT],
        user_input[CONF_TOKEN].strip(),
    )
    try:
        await agent.health()
        if not await agent.version(EASYPARK_PACKAGE):
            return {"base": "easypark_missing"}
    except AgentError:
        return {"base": "cannot_connect"}
    return {}


def _parking_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_ZONE, default=defaults.get(CONF_ZONE, "")): str,
        vol.Required(CONF_PLATES, default=defaults.get(CONF_PLATES, "")): str,
        vol.Required(
            CONF_MAX_PRICE, default=defaults.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE)
        ): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(
            CONF_RETURN_PACKAGE,
            default=defaults.get(CONF_RETURN_PACKAGE, DEFAULT_RETURN_PACKAGE),
        ): str,
    }


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(user_input)
    cleaned[CONF_HOST] = user_input[CONF_HOST].strip()
    cleaned[CONF_TOKEN] = user_input[CONF_TOKEN].strip()
    cleaned[CONF_ZONE] = user_input[CONF_ZONE].strip().upper()
    cleaned[CONF_PLATES] = ", ".join(parse_plates(user_input[CONF_PLATES]))
    cleaned[CONF_RETURN_PACKAGE] = user_input.get(CONF_RETURN_PACKAGE, "").strip()
    return cleaned


class EasyParkConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, user_input)
            if not errors:
                data = _clean(user_input)
                await self.async_set_unique_id(f"{data[CONF_HOST]}_{data[CONF_ZONE]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"EasyPark {data[CONF_ZONE]}", data=data)

        defaults = user_input or {}
        schema = vol.Schema({**_connection_schema(defaults), **_parking_schema(defaults)})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EasyParkOptionsFlow()


class EasyParkOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
        defaults = user_input or {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({**_connection_schema(defaults), **_parking_schema(defaults)}),
            errors=errors,
        )
