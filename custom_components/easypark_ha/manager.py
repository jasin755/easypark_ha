"""Parking state and the buy action, shared by all entities of one entry."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
    DOMAIN,
    STORAGE_VERSION,
)
from .driver import (
    ActiveSession,
    AlreadyActiveError,
    AppInterruptedError,
    DriverError,
    EasyParkDriver,
    NoActiveParkingError,
    StopResult,
)

_LOGGER = logging.getLogger(__name__)

# Another app on the phone (typically the car integration's poll) took the
# foreground before anything was paid or stopped: wait and try again.
RETRY_DELAY_S = 15
RETRY_ATTEMPTS = 3


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
        self.duration_min: int = DEFAULT_DURATION_MIN
        self.dry_run: bool = True
        self.session: dict[str, Any] | None = None
        # The price EasyPark shows, incl. service fee: the quote on Confirm
        # parking, then the running session, then the final summary.
        self.price: dict[str, Any] = {}
        self.last_result: str = "idle"
        self.last_details: dict[str, Any] = {}

    # -- persistence / listeners ---------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.duration_min = int(data.get("duration_min", self.duration_min))
        self.dry_run = bool(data.get("dry_run", self.dry_run))
        if data.get("plate") in self.plates:
            self.plate = data["plate"]
        self.session = data.get("session")
        self.price = data.get("price") or {}
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
                "duration_min": self.duration_min,
                "dry_run": self.dry_run,
                "plate": self.plate,
                "session": self.session,
                "price": self.price,
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

    def _set_price(self, total: float | None, fee: float | None, source: str) -> None:
        if total is None:
            return
        self.price = {"total": total, "service_fee": fee, "source": source, "read_at": dt_util.now().isoformat()}

    # -- actions --------------------------------------------------------------

    def _session_from_app(self, active: ActiveSession, now: datetime, started: datetime | None) -> dict[str, Any]:
        ends = active.ends_at(now)
        return {
            "zone": active.zone,
            "plate": active.plate,
            "started": started.isoformat() if started else None,
            "ends": ends.isoformat() if ends else None,
            "adjusted": active.adjusted,
        }

    async def _run(self, label: str, operation: Callable[[], Awaitable[None]]) -> None:
        """One phone operation at a time; always hand the phone back afterwards."""
        if self._lock.locked():
            raise HomeAssistantError("Another EasyPark operation is running")
        try:
            async with self._lock:
                self.last_result = label
                self._notify()
                try:
                    await operation()
                except (DriverError, AgentError) as err:
                    self.last_result = f"error: {err}"[:255]
                    _LOGGER.warning("EasyPark %s failed: %s", label, err)
                    raise HomeAssistantError(f"EasyPark: {err}") from err
                finally:
                    await self._return_to_previous_app()
                    self._save()
        finally:
            # After the lock is released, so the buttons see the new state.
            self._notify()

    async def _retrying(self, label: str, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``call``; while another app holds the phone, wait and retry.

        The driver raises AppInterruptedError only before anything was paid or
        stopped, so repeating the whole call is safe.
        """
        for attempt in range(1, RETRY_ATTEMPTS + 2):
            try:
                return await call()
            except AppInterruptedError as err:
                if attempt > RETRY_ATTEMPTS:
                    raise
                _LOGGER.info("EasyPark %s interrupted (%s); retry %d/%d in %ds",
                             label, err, attempt, RETRY_ATTEMPTS, RETRY_DELAY_S)
                self.last_result = f"{label}: phone busy, retry {attempt}/{RETRY_ATTEMPTS}"
                self._notify()
                await asyncio.sleep(RETRY_DELAY_S)
        raise AssertionError("unreachable")

    async def async_buy(self) -> None:
        await self._run("buying", self._buy)

    async def _buy(self) -> None:
        dry_run = self.dry_run
        now = dt_util.now()

        async def buy_once() -> Any:
            nonlocal now
            # Each attempt starts from the current time, so the duration stays right.
            now = dt_util.now()
            end = (now + timedelta(minutes=self.duration_min)).replace(second=0, microsecond=0)
            return await self._driver.buy(
                zone=self.zone, plate=self.plate, end=end, now=now, dry_run=dry_run, max_price=self.max_price
            )

        try:
            result = await self._retrying("buying", buy_once)
        except AlreadyActiveError as err:
            self.session = self._session_from_app(err.session, now, None)
            self._set_price(err.session.price, err.session.service_fee, "running")
            raise

        quote = result.quote
        self.last_details = {
            "zone": quote.zone,
            "plate": quote.plate,
            "requested_end": quote.ends.isoformat(),
            "total": quote.total,
            "service_fee": quote.service_fee,
            "duration": quote.duration_text,
            "zone_info": quote.zone_info,
            "dry_run": dry_run,
            "notes": result.after_start_texts,
        }
        if dry_run:
            self.last_result = "dry_run_ok"
            return
        if result.session is None:
            # Start Parking was tapped but the app shows no running parking.
            # Never retried: it may still have been bought.
            raise DriverError(
                "Start Parking was tapped but EasyPark shows no running parking; check the app "
                + "; ".join(result.after_start_texts)
            )
        self.session = self._session_from_app(result.session, now, now)
        self._set_price(quote.total, quote.service_fee, "quote")
        self._set_price(result.session.price, result.session.service_fee, "running")
        self.last_details["ends"] = self.session["ends"]
        self.last_result = "started"

    async def async_stop(self) -> None:
        await self._run("stopping", self._stop)

    async def _stop(self) -> None:
        plate = (self.session or {}).get("plate") or self.plate
        attempts = 0

        async def stop_once() -> StopResult:
            nonlocal attempts
            attempts += 1
            try:
                return await self._driver.stop(plate)
            except NoActiveParkingError:
                if attempts == 1:
                    raise
                # An interrupted earlier attempt got as far as stopping it.
                return StopResult(plate=plate, total=None, duration_text="", period_text="")

        result = await self._retrying("stopping", stop_once)
        self.last_details = {
            "plate": result.plate,
            "total": result.total,
            "service_fee": result.service_fee,
            "duration": result.duration_text,
            "period": result.period_text,
        }
        self._set_price(result.total, result.service_fee, "final")
        self.session = None
        self.last_result = "stopped"

    async def async_refresh(self) -> None:
        await self._run("reading", self._refresh)

    async def _refresh(self) -> None:
        plate = (self.session or {}).get("plate") or self.plate
        active = await self._retrying("reading", lambda: self._driver.active_session(plate))
        now = dt_util.now()
        if active is None:
            self.session = None
            self.last_result = "no_active_parking"
            return
        started = self.started if self.session and self.session.get("plate") == active.plate else None
        self.session = {**(self.session or {}), **self._session_from_app(active, now, started)}
        self._set_price(active.price, active.service_fee, "running")
        self.last_result = "active"

    async def _return_to_previous_app(self) -> None:
        if not self.return_package:
            return
        try:
            await self._agent.launch(self.return_package)
        except AgentError as err:
            _LOGGER.debug("Could not return to %s: %s", self.return_package, err)
