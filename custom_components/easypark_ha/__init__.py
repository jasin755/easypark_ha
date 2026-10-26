"""EasyPark parking purchases through an Android companion phone."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .agent import AgentClient
from .const import CONF_HOST, CONF_PORT, CONF_TOKEN, DOMAIN
from .manager import ParkingManager

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type EasyParkConfigEntry = ConfigEntry[ParkingManager]


def entry_options(entry: ConfigEntry) -> dict:
    return {**entry.data, **entry.options}


async def async_setup_entry(hass: HomeAssistant, entry: EasyParkConfigEntry) -> bool:
    options = entry_options(entry)
    agent = AgentClient(
        async_get_clientsession(hass),
        options[CONF_HOST],
        options[CONF_PORT],
        options[CONF_TOKEN],
    )
    _remove_retired_entities(hass, entry)
    manager = ParkingManager(hass, entry.entry_id, options, agent)
    await manager.async_load()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


RETIRED_KEYS = ("hourly_rate", "cost_so_far", "quoted_total")


def _remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """0.3.0 reads prices from EasyPark; drop the manual-rate entities of 0.1/0.2."""
    registry = er.async_get(hass)
    for key in RETIRED_KEYS:
        for platform in ("number", "sensor"):
            entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
            if entity_id:
                registry.async_remove(entity_id)


async def _async_reload(hass: HomeAssistant, entry: EasyParkConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EasyParkConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded
