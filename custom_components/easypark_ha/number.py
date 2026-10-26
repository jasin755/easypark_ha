"""Hourly rate and purchase duration."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    async_add_entities(
        [
            HourlyRateNumber(manager, entry.entry_id, "hourly_rate"),
            DurationNumber(manager, entry.entry_id, "duration"),
        ]
    )


class HourlyRateNumber(EasyParkEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 1000
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "CZK/h"
    _attr_icon = "mdi:cash-clock"

    @property
    def native_value(self) -> float:
        return self.manager.hourly_rate

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_hourly_rate(value)


class DurationNumber(EasyParkEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 5
    _attr_native_max_value = 24 * 60
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-outline"

    @property
    def native_value(self) -> int:
        return self.manager.duration_min

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_duration(int(value))
