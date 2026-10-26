"""Test mode: go through the purchase up to Confirm parking, then back out."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DryRunSwitch(entry.runtime_data, entry.entry_id, "dry_run")])


class DryRunSwitch(EasyParkEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:test-tube"

    @property
    def is_on(self) -> bool:
        return self.manager.dry_run

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.manager.set_dry_run(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.manager.set_dry_run(False)
