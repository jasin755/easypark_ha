"""Drive the real EasyPark screens (captured on a Pixel 4a, plate/card anonymised)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from custom_components.easypark_ha.const import EASYPARK_PACKAGE
from custom_components.easypark_ha.driver import (
    DriverError,
    EasyParkDriver,
    day_label,
    parse_duration,
    parse_ends,
    parse_price,
)
from custom_components.easypark_ha.ui import Screen

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 26)
END = datetime(2026, 10, 26, 20, 58)
NOW = datetime(2026, 10, 26, 18, 59)


def xml(name: str) -> str:
    return (FIXTURES / f"{name}.xml").read_text()


# (current screen, what was tapped) -> next screen. A tap is identified by the
# resource-id of the clicked node, or by its first text when it has none.
TRANSITIONS = {
    ("home", "search_redesign_button"): "search",
    ("search", "search_locations_search_bar"): "search",
    ("search_typed", "parking_area_list_item"): "vehicles",
    ("vehicles", "1AB2345"): "wheel",
    ("wheel", "Ends 6:57 PM"): "picker",
    ("picker", "20"): "picker_hour20",
    ("picker_hour20", "Confirm"): "wheel_set",
    ("wheel_set", "WheelTimePicker_ContinueButton"): "confirm",
    ("confirm", "RallyTopBarBack"): "wheel_after_back",
    ("wheel_after_back", "RallyTopBarClose"): "home_after",
    ("confirm", "Start Parking"): "started",
}


class FakeAgent:
    def __init__(self, start: str = "home") -> None:
        self.state = start
        self.taps: list[str] = []
        self.typed: list[str] = []
        self.foreground = True

    async def snapshot(self) -> str:
        if self.state == "started":
            return '<hierarchy><node package="net.easypark.android" text="Parking started" bounds="[0,0][10,10]"/></hierarchy>'
        return xml(self.state)

    async def is_foreground(self, package: str) -> bool:
        return self.foreground and package == EASYPARK_PACKAGE

    async def launch(self, package: str) -> None:
        self.foreground = True

    async def tap(self, x: int, y: int) -> None:
        screen = Screen(xml(self.state))
        hits = [n for n in screen.nodes if n.clickable and n.bounds[0] <= x < n.bounds[2] and n.bounds[1] <= y < n.bounds[3]]
        node = max(hits, key=lambda n: n.depth)
        keys = [k for k in (node.rid, *(node.texts() or [node.desc])) if k]
        key = next((k for k in keys if (self.state, k) in TRANSITIONS), None)
        if key is None:
            raise AssertionError(f"unexpected tap {keys!r} on {self.state}")
        self.taps.append(key)
        self.state = TRANSITIONS[(self.state, key)]

    async def swipe(self, *args: int) -> None:
        raise AssertionError("no swipe expected")

    async def back(self) -> None:
        raise AssertionError("no back expected")

    async def set_text(self, text: str) -> None:
        assert self.state == "search"
        self.typed.append(text)
        self.state = "search_typed"


async def _no_sleep(_s: float) -> None:
    return None


def _driver(agent: FakeAgent) -> EasyParkDriver:
    return EasyParkDriver(agent, sleep=_no_sleep, step_delay=0)  # type: ignore[arg-type]


def test_helpers():
    assert parse_ends("Ends 8:58 PM") == (20, 58)
    assert parse_ends("Ends 12:05 AM") == (0, 5)
    assert parse_ends("Ends 20:58") == (20, 58)
    assert parse_ends("Ends Tomorrow, 5:40 AM") == (5, 40)
    assert parse_duration("1h 59m") == 119
    assert parse_duration("45m") == 45
    assert parse_duration("29 min") == 29
    assert parse_duration("1d 2h") == 1560
    assert parse_duration("41925") is None
    assert parse_price("CZK15.40") == 15.40
    assert parse_price("15,40 Kč") == 15.40
    assert day_label(TODAY, TODAY) == "Today"
    assert day_label(date(2026, 10, 27), TODAY) == "Tue Oct 27"


async def test_dry_run_reads_quote_and_backs_out():
    agent = FakeAgent()
    result = await _driver(agent).buy(zone="P4-1925", plate="1AB2345", end=END, now=NOW, dry_run=True, max_price=100)
    assert agent.typed == ["P4-1925"]
    assert "Start Parking" not in agent.taps
    assert agent.state == "home_after"
    assert result.started is False
    assert result.quote.total == 15.40
    assert result.quote.service_fee == 4.90
    assert result.quote.duration_text == "1h 59m"
    assert "P4-1925" in result.quote.zone_info


async def test_real_purchase_taps_start_once():
    agent = FakeAgent()
    result = await _driver(agent).buy(zone="P4-1925", plate="1AB2345", end=END, now=NOW, dry_run=False, max_price=100)
    assert agent.taps.count("Start Parking") == 1
    assert result.started is True
    assert result.after_start_texts == ["Parking started"]


async def test_price_limit_blocks_start():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="exceeds the limit"):
        await _driver(agent).buy(zone="P4-1925", plate="1AB2345", end=END, now=NOW, dry_run=False, max_price=10)
    assert "Start Parking" not in agent.taps


async def test_unknown_plate_stops_before_paying():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="not found"):
        await _driver(agent).buy(zone="P4-1925", plate="9ZZ9999", end=END, now=NOW, dry_run=False, max_price=100)
    assert agent.state == "vehicles"


async def test_wrong_zone_is_never_selected():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="zone P4-1940"):
        await _driver(agent).buy(zone="P4-1940", plate="1AB2345", end=END, now=NOW, dry_run=False, max_price=100)
    assert "parking_area_list_item" not in agent.taps


async def test_quote_mismatch_end_time_blocks_start():
    agent = FakeAgent()
    driver = _driver(agent)
    with pytest.raises(DriverError, match="different end time"):
        driver._read_quote(Screen(xml("confirm")), "P4-1925", "1AB2345", datetime(2026, 10, 26, 21, 0), NOW)


async def test_foreign_app_in_front_is_never_tapped():
    agent = FakeAgent(start="confirm")
    driver = _driver(agent)
    vw = '<hierarchy><node package="com.volkswagen.weconnect" text="Start Parking" clickable="true" bounds="[0,0][100,100]"/></hierarchy>'
    screen = Screen(vw)
    with pytest.raises(DriverError, match="not in the foreground"):
        await driver._tap(screen, screen.nodes[0])
    assert agent.taps == []


async def test_cold_start_home_is_recognised():
    agent = FakeAgent(start="cold_home")
    screen = await _driver(agent).go_home()
    assert screen.first_rid("search_redesign_button") is not None


async def test_interruption_after_start_is_not_an_error():
    """A retry after Start Parking could pay twice, so buy() must not raise."""
    agent = FakeAgent()

    async def vw_snapshot_after_start() -> str:
        if agent.state == "started":
            return '<hierarchy><node package="com.volkswagen.weconnect" text="Overview" bounds="[0,0][1,1]"/></hierarchy>'
        return xml(agent.state)

    agent.snapshot = vw_snapshot_after_start  # type: ignore[method-assign]
    result = await _driver(agent).buy(zone="P4-1925", plate="1AB2345", end=END, now=NOW, dry_run=False, max_price=100)
    assert result.started is True
    assert result.after_start_texts[0].startswith("unverified")
    assert agent.taps.count("Start Parking") == 1


async def test_wrong_day_is_caught_by_duration():
    driver = _driver(FakeAgent())
    with pytest.raises(DriverError, match="duration"):
        driver._read_quote(Screen(xml("confirm")), "P4-1925", "1AB2345", END, datetime(2026, 10, 25, 18, 59))
