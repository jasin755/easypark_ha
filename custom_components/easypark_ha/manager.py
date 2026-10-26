"""Parking state and the buy action, shared by all entities of one entry."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .agent import AgentClient, AgentError
from .const import (
    CONF_MAX_PRICE,
    CONF_PLATES,
    CONF_RETURN_PACKAGE,
    CONF_ZONE,
    DEFAULT_DURATION_MIN,
    DEFAULT_HOURLY_RATE,
    DOMAIN,
    STORAGE_VERSION,
)
from .driver import AppInterruptedError, DriverError, EasyParkDriver

_LOGGER = logging.getLogger(__name__)

RETRY_DELAY_S = 30


def parse_plates(value: str | list[str]) -> list[str]:
    items = value if isinstance(value, list) else value.replace(";", ",").split(",")
    return [p.strip().upper() for p in items if p.strip()]


class ParkingManager:
    def __init__(self, hass: HomeAssistant, entry_id: str, options: dict[str, Any], agent: AgentClient) -> None:
        self.hass = hass
        self._agent = agent
        self._driver = EasyParkDriver(agent)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsub_tick: CALLBACK_TYPE | None = None

        self.zone: str = options[CONF_ZONE]
        self.plates: list[str] = parse_plates(options[CONF_PLATES])
        self.max_price: float | None = options.get(CONF_MAX_PRICE)
        self.return_package: str = options.get(CONF_RETURN_PACKAGE, "")

        self.plate: str = self.plates[0]
        self.hourly_rate: float = DEFAULT_HOURLY_RATE
        self.duration_min: int = DEFAULT_DURATION_MIN
        self.dry_run: bool = True
        self.session: dict[str, Any] | None = None
        self.last_result: str = "idle"
        self.last_details: dict[str, Any] = {}

    # -- persistence / listeners ---------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.hourly_rate = float(data.get("hourly_rate", self.hourly_rate))
        self.duration_min = int(data.get("duration_min", self.duration_min))
        self.dry_run = bool(data.get("dry_run", self.dry_run))
        if data.get("plate") in self.plates:
            self.plate = data["plate"]
        self.session = data.get("session")
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, timedelta(minutes=1)
        )

    async def async_unload(self) -> None:
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "hourly_rate": self.hourly_rate,
                "duration_min": self.duration_min,
                "dry_run": self.dry_run,
                "plate": self.plate,
                "session": self.session,
            },
            1,
        )

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._notify()

    # -- settings -------------------------------------------------------------

    def set_hourly_rate(self, value: float) -> None:
        self.hourly_rate = value
        self._save()
        self._notify()

    def set_duration(self, minutes: int) -> None:
        self.duration_min = minutes
        self._save()
        self._notify()

    def set_plate(self, plate: str) -> None:
        self.plate = plate
        self._save()
        self._notify()

    def set_dry_run(self, enabled: bool) -> None:
        self.dry_run = enabled
        self._save()
        self._notify()

    # -- derived state --------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _dt(self, key: str) -> datetime | None:
        if not self.session or not self.session.get(key):
            return None
        return dt_util.parse_datetime(self.session[key])

    @property
    def started(self) -> datetime | None:
        return self._dt("started")

    @property
    def ends(self) -> datetime | None:
        return self._dt("ends")

    @property
    def active(self) -> bool:
        ends = self.ends
        return ends is not None and dt_util.now() < ends

    def cost_so_far(self) -> float | None:
        started, ends = self.started, self.ends
        if started is None or ends is None:
            return None
        until = min(dt_util.now(), ends)
        hours = max((until - started).total_seconds(), 0) / 3600
        return round(hours * float(self.session.get("hourly_rate", self.hourly_rate)), 2)

    # -- actions --------------------------------------------------------------

    async def async_buy(self) -> None:
        if self._lock.locked():
            raise HomeAssistantError("A parking purchase is already running")
        async with self._lock:
            self.last_result = "running"
            self._notify()
            now = dt_util.now()
            end = (now + timedelta(minutes=self.duration_min)).replace(second=0, microsecond=0)
            dry_run = self.dry_run
            try:
                try:
                    result = await self._driver.buy(
                        zone=self.zone, plate=self.plate, end=end, now=now,
                        dry_run=dry_run, max_price=self.max_price,
                    )
                except AppInterruptedError as err:
                    # Typically the car integration's poll; it is done within seconds.
                    _LOGGER.info("EasyPark purchase interrupted (%s), retrying once", err)
                    await asyncio.sleep(RETRY_DELAY_S)
                    result = await self._driver.buy(
                        zone=self.zone, plate=self.plate, end=end, now=now,
                        dry_run=dry_run, max_price=self.max_price,
                    )
            except (DriverError, AgentError) as err:
                self.last_result = f"error: {err}"
                self.last_details = {}
                _LOGGER.warning("EasyPark purchase failed: %s", err)
                raise HomeAssistantError(f"EasyPark: {err}") from err
            finally:
                await self._return_to_previous_app()
                self._notify()

            quote = result.quote
            self.last_details = {
                "zone": quote.zone,
                "plate": quote.plate,
                "ends": quote.ends.isoformat(),
                "total": quote.total,
                "service_fee": quote.service_fee,
                "duration": quote.duration_text,
                "zone_info": quote.zone_info,
                "dry_run": dry_run,
                "after_start_texts": result.after_start_texts[:20],
            }
            if result.started:
                self.session = {
                    "zone": quote.zone,
                    "plate": quote.plate,
                    "started": now.isoformat(),
                    "ends": quote.ends.isoformat(),
                    "total": quote.total,
                    "service_fee": quote.service_fee,
                    "hourly_rate": self.hourly_rate,
                }
                self.last_result = "started"
            else:
                self.last_result = "dry_run_ok"
            self._save()
            self._notify()

    async def async_stop(self) -> None:
        raise HomeAssistantError("Ending a parking session is not implemented yet")

    async def _return_to_previous_app(self) -> None:
        if not self.return_package:
            return
        try:
            await self._agent.launch(self.return_package)
        except AgentError as err:
            _LOGGER.debug("Could not return to %s: %s", self.return_package, err)
