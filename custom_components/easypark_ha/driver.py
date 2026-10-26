"""Drive the EasyPark Android app through the companion Agent.

Flow verified on EasyPark 26.40.1 (English UI, Pixel 4a):

  home map ─ Search ─ type zone ─ zone result ─ Select vehicle ─ wheel screen
  ─ "Ends …" chip ─ Set end time (day / hour / minute) ─ Confirm ─ Continue
  ─ Confirm parking ─ Start Parking ─ "Parking started" ─ (Allow notifications
  prompt) ─ Parking session

  home map ─ "N active parking session(s)" card ─ Parking session ─ Stop
  ─ Stop parking? ─ Parking stopped ─ Summary

Nothing is paid before the Confirm parking screen has been read back and its
plate, zone, end time and total match what was asked for. A purchase counts as
done only when the app shows the running Parking session for that plate.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .agent import AgentClient, AgentError, AgentPausedError
from .const import EASYPARK_PACKAGE
from .ui import Node, Screen

_LOGGER = logging.getLogger(__name__)

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PRICE = re.compile(r"(\d+(?:[.,]\d+)?)")
_DURATION = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:min|m))?")
_ENDS = re.compile(r"Ends\b.*?(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")

RID_HOME_SEARCH = "search_redesign_button"
RID_SEARCH_BAR = "search_locations_search_bar"
RID_ZONE_ITEM = "parking_area_list_item"
RID_SEARCH_CLOSE = "close_button_small"
RID_WHEEL = "wheel_screen"
RID_CONTINUE = "WheelTimePicker_ContinueButton"
RID_FORM_CLOSE = "RallyTopBarClose"
RID_TOP_BACK = "RallyTopBarBack"
RID_TITLE = "RallyTopBarExpandedTitle"
RID_TOP_TITLE = "RallyTopBarTitle"
RID_ERROR_BANNER = "RallyErrorBanner"
RID_ONGOING_CARDS = "MapScreen_OngoingParkingsCardsContainer"
RID_SESSION_VEHICLE = "OngoingParkingDetails_ListItem_Vehicle"
RID_SESSION_PRICE = "OngoingParkingDetails_ListItem_Price"
RID_SESSION_STOP = "OngoingParkingDetails_StopButton"
RID_STOP_SHEET = "StopConfirmationSheet"
RID_SHEET_CONFIRM = "ConfirmationSheet_ConfirmButton"

NOTIFICATIONS_PROMPT = "Allow notifications to get parking reminders"
_FEE = re.compile(r"(?:incl\.|including)\s+service fee\s+(.*)", re.IGNORECASE)
_FAILURE = re.compile(r".*(could not|couldn't|failed|try again).*", re.IGNORECASE)


class DriverError(Exception):
    """The app was not in the expected state; nothing further was tapped."""


class AppInterruptedError(DriverError):
    """Another app took the foreground before parking was started."""


@dataclass
class ActiveSession:
    """A running parking as the app's Parking session screen shows it."""

    plate: str
    zone: str
    ends_text: str
    ends_hm: tuple[int, int] | None
    ends_tomorrow: bool
    price: float | None
    service_fee: float | None = None
    adjusted: bool = False

    def ends_at(self, now: datetime) -> datetime | None:
        if self.ends_hm is None:
            return None
        end = now.replace(hour=self.ends_hm[0], minute=self.ends_hm[1], second=0, microsecond=0)
        if self.ends_tomorrow or end < now - timedelta(minutes=1):
            end += timedelta(days=1)
        return end


class NoActiveParkingError(DriverError):
    """The vehicle has no running parking."""


class AlreadyActiveError(DriverError):
    """The vehicle already has a running parking; nothing was bought."""

    def __init__(self, session: ActiveSession) -> None:
        super().__init__(f"{session.plate} already has an active parking in {session.zone} ({session.ends_text})")
        self.session = session


@dataclass
class StopResult:
    plate: str
    total: float | None
    duration_text: str
    period_text: str
    service_fee: float | None = None


@dataclass
class Quote:
    zone: str
    plate: str
    ends: datetime
    total: float | None
    service_fee: float | None
    duration_text: str
    zone_info: str


@dataclass
class PurchaseResult:
    quote: Quote
    started: bool
    dry_run: bool
    session: ActiveSession | None = None
    after_start_texts: list[str] = field(default_factory=list)


def normalize_plate(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


def day_label(target: date, today: date) -> str:
    if target == today:
        return "Today"
    return f"{_DAYS[target.weekday()]} {_MONTHS[target.month - 1]} {target.day}"


def parse_price(text: str) -> float | None:
    match = _PRICE.search(text.replace(" ", ""))
    return float(match.group(1).replace(",", ".")) if match else None


def find_fee(texts: list[str]) -> float | None:
    """``Including service fee CZK4.90`` / ``incl. service fee CZK 0.00`` → 4.9."""
    for text in texts:
        match = _FEE.fullmatch(text.strip())
        if match:
            return parse_price(match.group(1))
    return None


def parse_duration(text: str) -> int | None:
    """``1h 59m`` / ``29 min`` / ``1d 2h`` → minutes."""
    match = _DURATION.fullmatch(text.strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return days * 1440 + hours * 60 + minutes


def parse_ends(text: str) -> tuple[int, int] | None:
    """``Ends 8:58 PM`` / ``Ends Tomorrow, 5:40 AM`` / ``Ends 20:58`` → (hour, minute)."""
    match = _ENDS.search(text)
    if not match:
        return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return hour, minute


class EasyParkDriver:
    def __init__(
        self,
        agent: AgentClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        step_delay: float = 1.0,
    ) -> None:
        self._agent = agent
        self._sleep = sleep
        self._delay = step_delay

    # -- primitives -----------------------------------------------------------

    async def _screen(self) -> Screen:
        return Screen(await self._agent.snapshot())

    async def _screen_or_empty(self) -> Screen:
        """A snapshot, or an empty screen while no window is readable (dialogs in transition)."""
        try:
            return await self._screen()
        except AgentPausedError:
            raise
        except AgentError:
            return Screen("<hierarchy/>")

    async def _wait(
        self, predicate: Callable[[Screen], bool], what: str, timeout_s: float = 10.0
    ) -> Screen:
        waited = 0.0
        while True:
            screen = await self._screen_or_empty()
            if screen.package == EASYPARK_PACKAGE and predicate(screen):
                return screen
            if waited >= timeout_s:
                if screen.package != EASYPARK_PACKAGE:
                    raise AppInterruptedError(f"{screen.package or 'no app'} is in the foreground while waiting for {what}")
                raise DriverError(f"EasyPark did not show {what}: {screen.all_texts()[:12]}")
            await self._sleep(0.5)
            waited += 0.5

    async def _tap(self, screen: Screen, node: Node) -> None:
        if screen.package != EASYPARK_PACKAGE:
            raise AppInterruptedError(f"EasyPark is not in the foreground ({screen.package})")
        x, y = node.center
        await self._agent.tap(x, y)
        await self._sleep(self._delay)

    async def _tap_clickable(self, screen: Screen, node: Node) -> None:
        target = screen.clickable_ancestor(node)
        if target is None:
            raise DriverError(f"'{node.label}' is not clickable")
        await self._tap(screen, target)

    # -- navigation -----------------------------------------------------------

    async def go_home(self) -> Screen:
        """Bring EasyPark to its map screen from whatever state it is in.

        Only close/back controls are used here, never a confirm control.
        """
        for _ in range(10):
            if not await self._agent.is_foreground(EASYPARK_PACKAGE):
                await self._agent.launch(EASYPARK_PACKAGE)
                await self._sleep(3)
                continue
            screen = await self._screen_or_empty()
            if not screen.nodes:
                await self._sleep(self._delay)
                continue
            if screen.first_rid(RID_HOME_SEARCH) is not None:
                return screen
            for rid in (RID_SEARCH_CLOSE, RID_FORM_CLOSE, RID_TOP_BACK):
                node = screen.first_rid(rid)
                if node is not None and screen.clickable_ancestor(node) is not None:
                    await self._tap_clickable(screen, node)
                    break
            else:
                await self._agent.back()
                await self._sleep(self._delay)
        raise DriverError("could not reach the EasyPark map screen")

    # -- running sessions -----------------------------------------------------

    @staticmethod
    def _is_session_screen(screen: Screen) -> bool:
        title = screen.first_rid(RID_TOP_TITLE)
        return title is not None and title.text == "Parking session"

    @staticmethod
    def _parse_session(screen: Screen) -> ActiveSession:
        vehicle = screen.first_rid(RID_SESSION_VEHICLE)
        plate = next((t for t in vehicle.texts() if re.fullmatch(r"[A-Z0-9 -]{4,}", t)), "") if vehicle else ""
        zone_text = next((t for t in screen.all_texts() if re.fullmatch(r".*\([A-Z0-9-]+\)", t)), "")
        zone_match = re.search(r"\(([A-Z0-9-]+)\)$", zone_text)
        ends_text = next((t for t in screen.all_texts() if t.startswith("Ends")), "")
        price_node = screen.first_rid(RID_SESSION_PRICE)
        price_texts = price_node.texts() if price_node else []
        price_text = next((t for t in price_texts if t.startswith("CZK")), "")
        return ActiveSession(
            plate=plate,
            zone=zone_match.group(1) if zone_match else "",
            ends_text=ends_text,
            ends_hm=parse_ends(ends_text),
            ends_tomorrow="Tomorrow" in ends_text,
            price=parse_price(price_text) if price_text else None,
            service_fee=find_fee(price_texts),
            adjusted=screen.has_text("Parking time was adjusted"),
        )

    @staticmethod
    def _error_message(screen: Screen) -> str | None:
        """Text of an EasyPark error dialog ("Attention!" … "could not …"), if one is shown."""
        texts = screen.all_texts()
        if "Attention!" in texts or any(_FAILURE.fullmatch(t) for t in texts):
            return " ".join(t for t in texts if t not in ("Close", "OK"))
        return None

    async def _close_error(self, screen: Screen) -> None:
        button = screen.first_text("Close") or screen.first_text("OK")
        if button is not None and screen.clickable_ancestor(button) is not None:
            await self._tap_clickable(screen, button)
        else:
            await self._agent.back()
            await self._sleep(self._delay)

    async def _close_prompts(self, screen: Screen) -> bool:
        """Dismiss EasyPark's own prompts (never grant anything). True if one was closed."""
        if screen.has_text(NOTIFICATIONS_PROMPT):
            close = screen.first_rid(RID_FORM_CLOSE)
            if close is not None:
                await self._tap_clickable(screen, close)
                return True
        return False

    async def _wait_for_started(self, plate: str, timeout_s: float) -> tuple[ActiveSession | None, str | None]:
        """After Start Parking: wait for the Parking session of ``plate`` ("Parking
        started" and the notifications prompt come first). Returns the session
        and EasyPark's error message, if it showed one."""
        wanted = normalize_plate(plate)
        waited = 0.0
        while waited <= timeout_s:
            screen = await self._screen_or_empty()
            if screen.package == EASYPARK_PACKAGE:
                error = self._error_message(screen)
                if error:
                    await self._close_error(screen)
                    return None, error
                if await self._close_prompts(screen):
                    continue
                if self._is_session_screen(screen):
                    session = self._parse_session(screen)
                    return (session if normalize_plate(session.plate) == wanted else None), None
                if screen.first_rid(RID_HOME_SEARCH) is not None:
                    return None, None  # back on the map: let the caller read the card
            await self._sleep(0.5)
            waited += 0.5
        return None, None

    async def active_session(self, plate: str) -> ActiveSession | None:
        """Read the running parking of ``plate`` from the app, or None."""
        home = await self.go_home()
        card = home.first_rid(RID_ONGOING_CARDS)
        if card is None:
            return None
        target = next((n for n in card.descendants() if n.clickable), None)
        if target is None:
            raise DriverError("the active parking card is not clickable")
        await self._tap(home, target)
        wanted = normalize_plate(plate)
        for _ in range(3):
            screen = await self._wait(
                lambda s: self._is_session_screen(s) or s.has_text(NOTIFICATIONS_PROMPT)
                or any(n.text and normalize_plate(n.text) == wanted for n in s.nodes),
                "the active parking",
            )
            if await self._close_prompts(screen):
                continue
            if self._is_session_screen(screen):
                session = self._parse_session(screen)
                return session if normalize_plate(session.plate) == wanted else None
            # Several sessions are listed: open the one for this plate.
            item = next(n for n in screen.nodes if n.text and normalize_plate(n.text) == wanted)
            await self._tap_clickable(screen, item)
        raise DriverError(f"could not open the active parking of {plate}")

    async def _open_zone(self, zone: str) -> None:
        home = await self.go_home()
        await self._tap_clickable(home, home.first_rid(RID_HOME_SEARCH))  # type: ignore[arg-type]
        search = await self._wait(lambda s: s.first_rid(RID_SEARCH_BAR) is not None, "search")
        await self._tap(search, search.first_rid(RID_SEARCH_BAR))  # type: ignore[arg-type]
        for attempt in range(3):
            # set-text acts on whatever window is active, so check it first.
            focused = await self._wait(lambda s: s.first_rid(RID_SEARCH_BAR) is not None, "search")
            try:
                await self._agent.set_text(zone)
                break
            except AgentError:
                if attempt == 2:
                    raise
                await self._tap(focused, focused.first_rid(RID_SEARCH_BAR))  # type: ignore[arg-type]
        wanted = re.compile(rf".*\({re.escape(zone)}\)|{re.escape(zone)}")

        def zone_item(screen: Screen) -> Node | None:
            for item in screen.by_rid(RID_ZONE_ITEM):
                if any(wanted.fullmatch(t) for t in item.texts()):
                    return item
            return None

        results = await self._wait(lambda s: zone_item(s) is not None, f"zone {zone}")
        await self._tap(results, zone_item(results))  # type: ignore[arg-type]

    async def _pick_vehicle(self, plate: str) -> None:
        def is_vehicle_list(s: Screen) -> bool:
            title = s.first_rid(RID_TITLE)
            return title is not None and title.text == "Select vehicle"

        screen = await self._wait(
            lambda s: is_vehicle_list(s) or s.first_rid(RID_WHEEL) is not None,
            "vehicle selection",
        )
        if not is_vehicle_list(screen):
            return  # single-vehicle accounts skip the list; plate checked on Confirm parking
        wanted = normalize_plate(plate)

        def plates(s: Screen) -> list[Node]:
            return [n for n in s.nodes if n.text and normalize_plate(n.text) == wanted]

        # The list renders a moment after its title; "Add vehicle" closes it.
        screen = await self._wait(lambda s: bool(plates(s)) or s.has_text("Add vehicle"), "the vehicle list")
        matches = plates(screen)
        if len(matches) != 1:
            raise DriverError(f"vehicle {plate} not found in EasyPark")
        await self._tap_clickable(screen, matches[0])

    # -- end time -------------------------------------------------------------

    @staticmethod
    def _columns(screen: Screen) -> list[Node]:
        return sorted((n for n in screen.nodes if n.scrollable), key=lambda n: n.bounds[0])

    @staticmethod
    def _items(column: Node) -> list[tuple[Node, str]]:
        items = []
        for child in column.children:
            texts = child.texts()
            if child.clickable and texts:
                items.append((child, texts[0]))
        return items

    async def _spin(self, index: int, wanted: int, key: Callable[[str], int | None]) -> None:
        """Bring the row whose ``key`` equals ``wanted`` under the selection line.

        Tapping a row scrolls it to the selection line. After such a scroll the
        Compose accessibility tree of that column reports stale bounds for a
        while, so only the first read of a column is trusted: it gives the
        selected value and the row height, and every later tap is computed
        from those. Rows hold consecutive values (days, hours, minutes). The
        result is verified afterwards through the "Ends …" text.
        """
        for _ in range(10):
            screen = await self._wait(lambda s: s.has_text("Set end time"), "the end-time picker")
            columns = self._columns(screen)
            if len(columns) != 3:
                raise DriverError(f"end-time picker has {len(columns)} columns, expected 3")
            column = columns[index]
            center_y = (column.bounds[1] + column.bounds[3]) // 2
            rows = sorted(
                ((node.bounds[1], k) for node, label in self._items(column) if (k := key(label)) is not None)
            )
            selected = next((k for node, label in self._items(column)
                             if node.contains_y(center_y) and (k := key(label)) is not None), None)
            tops = [top for top, _ in rows]
            steps = sorted(b - a for a, b in zip(tops, tops[1:], strict=False) if b > a)
            if selected is not None and steps:
                break
            await self._sleep(0.5)
        else:
            raise DriverError("could not read the end-time picker")

        row = steps[len(steps) // 2]
        reach = max(1, (min(center_y - column.bounds[1], column.bounds[3] - center_y) - row // 2) // row)
        x = column.center[0]
        current = selected
        for _ in range(40):
            if current == wanted:
                return
            step = max(-reach, min(reach, wanted - current))
            await self._tap_point(x, center_y + step * row)
            current += step
        raise DriverError(f"could not select {wanted} in the end-time picker")

    async def _tap_point(self, x: int, y: int) -> None:
        screen = await self._screen()
        if screen.package != EASYPARK_PACKAGE or not screen.has_text("Set end time"):
            raise AppInterruptedError("the end-time picker is no longer in the foreground")
        await self._agent.tap(x, y)
        await self._sleep(self._delay)

    async def _set_end_time(self, end: datetime, today: date) -> None:
        wheel = await self._wait(lambda s: s.first_rid(RID_WHEEL) is not None, "the time wheel")
        chip = wheel.first_text(r"Ends .*")
        if chip is None:
            raise DriverError("end-time chip not found")
        await self._tap_clickable(wheel, chip)

        def day_key(label: str) -> int | None:
            for offset in range(0, 31):
                if day_label(today + timedelta(days=offset), today) == label:
                    return offset
            return None

        def number_key(label: str) -> int | None:
            return int(label) if label.strip().isdigit() else None

        await self._spin(0, (end.date() - today).days, day_key)
        await self._spin(1, end.hour, number_key)
        await self._spin(2, end.minute, number_key)
        dialog = await self._screen()
        confirm = dialog.first_text("Confirm")
        if confirm is None:
            raise DriverError("end-time Confirm button not found")
        await self._tap_clickable(dialog, confirm)
        wheel = await self._wait(lambda s: s.first_rid(RID_WHEEL) is not None, "the time wheel")
        chip = wheel.first_text(r"Ends .*")
        tomorrow = end.date() == today + timedelta(days=1)
        if (
            chip is None
            or parse_ends(chip.text) != (end.hour, end.minute)
            or (end.date() == today and "Tomorrow" in chip.text)
            or (tomorrow and "Tomorrow" not in chip.text)
        ):
            raise DriverError(f"end time not applied: {chip.text if chip else None}")

    # -- confirm --------------------------------------------------------------

    @staticmethod
    def _value_after(screen: Screen, label: str) -> str | None:
        texts = screen.all_texts()
        if label in texts:
            for text in texts[texts.index(label) + 1:]:
                if _PRICE.search(text):
                    return text
        return None

    def _read_quote(self, screen: Screen, zone: str, plate: str, end: datetime, now: datetime) -> Quote:
        texts = screen.all_texts()
        if not any(normalize_plate(t) == normalize_plate(plate) for t in texts):
            raise DriverError(f"Confirm parking does not show plate {plate}")
        if not any(f"({zone})" in t or t == zone for t in texts):
            raise DriverError(f"Confirm parking does not show zone {zone}")
        ends = next((parse_ends(t) for t in texts if t.startswith("Ends")), None)
        if ends != (end.hour, end.minute):
            raise DriverError(f"Confirm parking shows a different end time: {ends}")
        total = self._value_after(screen, "Total")
        fee = self._value_after(screen, "Including service fee")
        duration = next((t for t in texts if parse_duration(t) is not None), "")
        # The end-time text has no date; the duration catches a wrong day.
        expected = (end - now).total_seconds() / 60
        if parse_duration(duration) is None or abs(parse_duration(duration) - expected) > 3:
            raise DriverError(f"Confirm parking shows duration {duration!r}, expected about {expected:.0f} min")
        info = next((t for t in texts if "Kód úseku" in t or "Zone code" in t), "")
        return Quote(
            zone=zone,
            plate=plate,
            ends=end,
            total=parse_price(total) if total else None,
            service_fee=parse_price(fee) if fee else None,
            duration_text=duration,
            zone_info=info,
        )

    # -- public ---------------------------------------------------------------

    async def buy(
        self,
        *,
        zone: str,
        plate: str,
        end: datetime,
        now: datetime,
        dry_run: bool,
        max_price: float | None,
    ) -> PurchaseResult:
        existing = await self.active_session(plate)
        if existing is not None:
            raise AlreadyActiveError(existing)
        await self._open_zone(zone)
        await self._pick_vehicle(plate)
        await self._set_end_time(end, now.date())
        wheel = await self._screen()
        cont = wheel.first_rid(RID_CONTINUE)
        if cont is None or not cont.clickable:
            raise DriverError("Continue is not available")
        await self._tap(wheel, cont)
        # The price arrives a moment after the screen; an error banner may come instead.
        confirm = await self._wait(
            lambda s: s.has_text("Confirm parking")
            and (self._value_after(s, "Total") is not None or s.first_rid(RID_ERROR_BANNER) is not None),
            "Confirm parking with its price",
            timeout_s=15,
        )
        banner = confirm.first_rid(RID_ERROR_BANNER)
        if banner is not None:
            raise DriverError("EasyPark refuses: " + " ".join(banner.texts()))
        quote = self._read_quote(confirm, zone, plate, end, now)
        if quote.total is None:
            raise DriverError("could not read the total price")
        if max_price is not None and quote.total > max_price:
            raise DriverError(f"total {quote.total} CZK exceeds the limit {max_price} CZK")
        start = confirm.first_text("Start Parking")
        start_button = confirm.clickable_ancestor(start) if start is not None else None
        if start_button is None or not start_button.enabled:
            raise DriverError("Start Parking is not available")
        if dry_run:
            _LOGGER.info("Dry run: would start parking %s", quote)
            await self.go_home()
            return PurchaseResult(quote=quote, started=False, dry_run=True)

        await self._tap(confirm, start_button)
        # From here on the parking may be running: never raise, so nothing
        # upstream can retry and pay twice. Success is what the app shows.
        session: ActiveSession | None = None
        notes: list[str] = []
        try:
            session, error = await self._wait_for_started(plate, timeout_s=30)
            if error:
                notes.append(f"EasyPark: {error}")
            if session is None:
                session = await self.active_session(plate)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Start Parking tapped, but the session could not be read: %s", err)
            notes.append(str(err))
        if session is not None and not session.zone:
            # A warning banner can push the zone row out of the read tree; it
            # was verified on Confirm parking.
            session.zone = zone
        if session is not None and session.zone != zone:
            notes.append(f"running parking is in {session.zone}, not {zone}")
        _LOGGER.info("After Start Parking: %s %s", session, notes)
        return PurchaseResult(
            quote=quote, started=session is not None, dry_run=False, session=session, after_start_texts=notes
        )

    async def stop(self, plate: str) -> StopResult:
        """End the running parking of ``plate`` and read the summary.

        EasyPark sometimes answers Stop with an error dialog; then the app is
        read again and the stop is tried once more.
        """
        error = ""
        for attempt in range(2):
            if await self.active_session(plate) is None:
                if attempt == 0:
                    raise NoActiveParkingError(f"{plate} has no active parking")
                return StopResult(plate=plate, total=None, duration_text="", period_text="")
            detail = await self._wait(self._is_session_screen, "the active parking")
            stop = detail.first_rid(RID_SESSION_STOP)
            if stop is None:
                raise DriverError("Stop is not available")
            await self._tap(detail, stop)
            sheet = await self._wait(lambda s: s.first_rid(RID_STOP_SHEET) is not None, "Stop parking?")
            if not any(plate in t for t in sheet.all_texts()):
                raise DriverError(f"Stop parking? does not name {plate}")
            confirm = sheet.first_rid(RID_SHEET_CONFIRM)
            if confirm is None:
                raise DriverError("Stop parking confirmation not found")
            await self._tap(sheet, confirm)

            def summary_loaded(s: Screen) -> bool:
                title = s.first_rid(RID_TOP_TITLE)
                return title is not None and title.text == "Summary" and any(t.startswith("CZK") for t in s.all_texts())

            outcome = await self._wait(
                lambda s: summary_loaded(s) or self._error_message(s) is not None,
                "the parking summary",
                timeout_s=20,
            )
            message = self._error_message(outcome)
            if message:
                error = message
                _LOGGER.warning("EasyPark refused to stop (attempt %d): %s", attempt + 1, message)
                await self._close_error(outcome)
                await self._sleep(3)
                continue
            texts = outcome.all_texts()
            total = next((t for t in texts if t.startswith("CZK")), "")
            duration = next((t for t in texts if parse_duration(t) is not None), "")
            period = next((t for t in texts if " - " in t and ":" in t), "")
            await self.go_home()
            return StopResult(
                plate=plate,
                total=parse_price(total) if total else None,
                duration_text=duration,
                period_text=period,
                service_fee=find_fee(texts),
            )
        raise DriverError(f"EasyPark could not stop the parking: {error}")
