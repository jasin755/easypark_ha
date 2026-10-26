"""Drive the EasyPark Android app through the companion Agent.

Flow verified on EasyPark 26.40.1 (English UI, Pixel 4a):

  home map ─ Search ─ type zone ─ zone result ─ Select vehicle ─ wheel screen
  ─ "Ends …" chip ─ Set end time (day / hour / minute) ─ Confirm ─ Continue
  ─ Confirm parking ─ Start Parking

Nothing is paid before the Confirm parking screen has been read back and its
plate, zone, end time and total match what was asked for.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .agent import AgentClient
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


class DriverError(Exception):
    """The app was not in the expected state; nothing further was tapped."""


class AppInterruptedError(DriverError):
    """Another app took the foreground before parking was started."""


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

    async def _wait(
        self, predicate: Callable[[Screen], bool], what: str, timeout_s: float = 10.0
    ) -> Screen:
        waited = 0.0
        while True:
            screen = await self._screen()
            if screen.package == EASYPARK_PACKAGE and predicate(screen):
                return screen
            if waited >= timeout_s:
                if screen.package != EASYPARK_PACKAGE:
                    raise AppInterruptedError(f"{screen.package} took the foreground while waiting for {what}")
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
            screen = await self._screen()
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

    async def _open_zone(self, zone: str) -> None:
        home = await self.go_home()
        await self._tap_clickable(home, home.first_rid(RID_HOME_SEARCH))  # type: ignore[arg-type]
        search = await self._wait(lambda s: s.first_rid(RID_SEARCH_BAR) is not None, "search")
        await self._tap(search, search.first_rid(RID_SEARCH_BAR))  # type: ignore[arg-type]
        await self._agent.set_text(zone)
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
        matches = [n for n in screen.nodes if n.text and normalize_plate(n.text) == wanted]
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
        await self._open_zone(zone)
        await self._pick_vehicle(plate)
        await self._set_end_time(end, now.date())
        wheel = await self._screen()
        cont = wheel.first_rid(RID_CONTINUE)
        if cont is None or not cont.clickable:
            raise DriverError("Continue is not available")
        await self._tap(wheel, cont)
        confirm = await self._wait(lambda s: s.has_text("Confirm parking"), "Confirm parking")
        quote = self._read_quote(confirm, zone, plate, end, now)
        if quote.total is None:
            raise DriverError("could not read the total price")
        if max_price is not None and quote.total > max_price:
            raise DriverError(f"total {quote.total} CZK exceeds the limit {max_price} CZK")
        start = confirm.first_text("Start Parking")
        if start is None:
            raise DriverError("Start Parking button not found")
        if dry_run:
            _LOGGER.info("Dry run: would start parking %s", quote)
            await self.go_home()
            return PurchaseResult(quote=quote, started=False, dry_run=True)

        await self._tap_clickable(confirm, start)
        # From here on the parking may be running: never raise, so nothing
        # upstream can retry and pay twice.
        try:
            after = await self._wait(
                lambda s: not s.has_text("Confirm parking"), "the started parking", timeout_s=20
            )
            texts = after.all_texts()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Start Parking tapped, but the result could not be read: %s", err)
            texts = [f"unverified: {err}"]
        _LOGGER.info("Parking started, app shows: %s", texts[:20])
        return PurchaseResult(quote=quote, started=True, dry_run=False, after_start_texts=texts)
