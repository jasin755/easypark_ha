"""Whether a parking session bought here is running."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ActiveSensor(entry.runtime_data, entry.entry_id, "active")])


class ActiveSensor(EasyParkEntity, BinarySensorEntity):
    _attr_icon = "mdi:parking"

    @property
    def is_on(self) -> bool:
        return self.manager.active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self.manager.session or {}
        return {"zone": session.get("zone"), "plate": session.get("plate")}
