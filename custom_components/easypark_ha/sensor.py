"""Parking session sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity
from .manager import ParkingManager


@dataclass(frozen=True, kw_only=True)
class EasyParkSensorDescription(SensorEntityDescription):
    value: Callable[[ParkingManager], Any]
    attributes: Callable[[ParkingManager], dict[str, Any]] | None = None


def _session(m: ParkingManager, key: str) -> Any:
    return (m.session or {}).get(key)


SENSORS = (
    EasyParkSensorDescription(
        key="price",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="CZK",
        suggested_display_precision=2,
        icon="mdi:cash",
        value=lambda m: m.price.get("total"),
        attributes=lambda m: {
            "service_fee": m.price.get("service_fee"),
            "source": m.price.get("source"),
            "read_at": m.price.get("read_at"),
        },
    ),
    EasyParkSensorDescription(
        key="service_fee",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="CZK",
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value=lambda m: m.price.get("service_fee"),
    ),
    EasyParkSensorDescription(
        key="started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda m: m.started,
    ),
    EasyParkSensorDescription(
        key="ends",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda m: m.ends,
    ),
    EasyParkSensorDescription(
        key="zone",
        icon="mdi:map-marker-radius",
        value=lambda m: _session(m, "zone") if m.active else m.zone,
    ),
    EasyParkSensorDescription(
        key="last_result",
        icon="mdi:information-outline",
        value=lambda m: m.last_result[:255],
        attributes=lambda m: m.last_details,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    async_add_entities(EasyParkSensor(manager, entry.entry_id, d) for d in SENSORS)


class EasyParkSensor(EasyParkEntity, SensorEntity):
    entity_description: EasyParkSensorDescription

    def __init__(self, manager: ParkingManager, entry_id: str, description: EasyParkSensorDescription) -> None:
        super().__init__(manager, entry_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.manager)
