"""Buy / stop parking buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EasyParkConfigEntry
from .entity import EasyParkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EasyParkConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    async_add_entities(
        [
            BuyButton(manager, entry.entry_id, "buy"),
            StopButton(manager, entry.entry_id, "stop"),
            RefreshButton(manager, entry.entry_id, "refresh"),
        ]
    )


class _PhoneButton(EasyParkEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return not self.manager.busy


class BuyButton(_PhoneButton):
    """Only while no parking is known to run; Stop takes its place."""

    _attr_icon = "mdi:parking"

    @property
    def available(self) -> bool:
        return super().available and not self.manager.active

    async def async_press(self) -> None:
        await self.manager.async_buy()


class StopButton(_PhoneButton):
    _attr_icon = "mdi:stop-circle-outline"

    @property
    def available(self) -> bool:
        return super().available and self.manager.active

    async def async_press(self) -> None:
        await self.manager.async_stop()


class RefreshButton(_PhoneButton):
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        await self.manager.async_refresh()
