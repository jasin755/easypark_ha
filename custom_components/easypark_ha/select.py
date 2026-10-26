"""Which configured vehicle the next purchase is for."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([VehicleSelect(entry.runtime_data, entry.entry_id, "vehicle")])


class VehicleSelect(EasyParkEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:car"

    @property
    def options(self) -> list[str]:
        return self.manager.plates

    @property
    def current_option(self) -> str:
        return self.manager.plate

    async def async_select_option(self, option: str) -> None:
        self.manager.set_plate(option)
