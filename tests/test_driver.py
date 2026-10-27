"""Drive the real EasyPark screens (captured on a Pixel 4a, plate/card anonymised)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from custom_components.easypark_ha.const import EASYPARK_PACKAGE
from custom_components.easypark_ha.driver import (
    AlreadyActiveError,
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
PLATE = "1AB2345"


def xml(name: str) -> str:
    return (FIXTURES / f"{name}.xml").read_text()


# (current screen, what was tapped) -> next screen. A tap is identified by the
# resource-id of the clicked node, or by one of its texts.
TRANSITIONS = {
    ("home", "search_redesign_button"): "search",
    ("search", "search_locations_search_bar"): "search",
    ("search_typed", "parking_area_list_item"): "vehicles",
    ("vehicles", PLATE): "wheel",
    ("wheel", "Ends 6:57 PM"): "picker",
    ("picker", "20"): "picker_hour20",
    ("picker_hour20", "Confirm"): "wheel_set",
    ("wheel_set", "WheelTimePicker_ContinueButton"): "confirm",
    ("confirm", "RallyTopBarBack"): "wheel_after_back",
    ("wheel_after_back", "RallyTopBarClose"): "home_after",
    ("confirm", "Start Parking"): "parking_started",
    ("notifications_prompt", "RallyTopBarClose"): "session_detail",
    ("home_active", "1 active parking session"): "session_detail",
    ("session_detail", "RallyTopBarClose"): "home_active",
    ("session_detail", "OngoingParkingDetails_StopButton"): "stop_sheet",
    ("stop_sheet", "ConfirmationSheet_ConfirmButton"): "parking_stopped",
    ("summary", "RallyTopBarBack"): "home",
}
# Screens that move on by themselves once they have been seen.
AUTO = {"parking_started": "notifications_prompt", "parking_stopped": "summary"}


class FakeAgent:
    def __init__(self, start: str = "home", transitions: dict | None = None) -> None:
        self.state = start
        self.transitions = {**TRANSITIONS, **(transitions or {})}
        self.taps: list[str] = []
        self.typed: list[str] = []

    async def snapshot(self) -> str:
        shown = xml(self.state)
        self.state = AUTO.get(self.state, self.state)
        return shown

    async def is_foreground(self, package: str) -> bool:
        return package == EASYPARK_PACKAGE

    async def launch(self, package: str) -> None:
        return None

    async def tap(self, x: int, y: int) -> None:
        screen = Screen(xml(self.state))
        hits = [n for n in screen.nodes if n.clickable and n.bounds[0] <= x < n.bounds[2] and n.bounds[1] <= y < n.bounds[3]]
        node = max(hits, key=lambda n: n.depth)
        keys = [k for k in (node.rid, *(node.texts() or [node.desc])) if k]
        key = next((k for k in keys if (self.state, k) in self.transitions), None)
        if key is None:
            raise AssertionError(f"unexpected tap {keys!r} on {self.state}")
        self.taps.append(key)
        self.state = self.transitions[(self.state, key)]

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


async def _buy(agent: FakeAgent, **kw):
    args = {"zone": "P4-1925", "plate": PLATE, "end": END, "now": NOW, "dry_run": False, "max_price": 100}
    return await _driver(agent).buy(**{**args, **kw})


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
    assert parse_price("CZK 0.00") == 0.0
    assert day_label(TODAY, TODAY) == "Today"
    assert day_label(date(2026, 10, 27), TODAY) == "Tue Oct 27"


async def test_dry_run_reads_quote_and_backs_out():
    agent = FakeAgent()
    result = await _buy(agent, dry_run=True)
    assert agent.typed == ["P4-1925"]
    assert "Start Parking" not in agent.taps
    assert agent.state == "home_after"
    assert result.started is False
    assert result.quote.total == 15.40
    assert result.quote.service_fee == 4.90
    assert result.quote.duration_text == "1h 59m"
    assert "P4-1925" in result.quote.zone_info


async def test_purchase_is_confirmed_by_the_running_session():
    agent = FakeAgent()
    result = await _buy(agent)
    assert agent.taps.count("Start Parking") == 1
    # The notifications prompt is closed, never granted.
    assert "RallyTopBarClose" in agent.taps and "Allow notifications" not in agent.taps
    assert result.started is True
    session = result.session
    assert session.plate == PLATE and session.zone == "P4-1925"
    # EasyPark moved the end; the app's value wins over the requested one.
    assert session.ends_hm == (21, 15) and session.adjusted is True
    assert session.ends_at(NOW) == datetime(2026, 10, 26, 21, 15)
    assert session.price == 0.0 and session.service_fee == 0.0


async def test_start_without_a_running_session_is_reported_not_raised():
    """A retry after Start Parking could pay twice, so buy() must not raise."""
    agent = FakeAgent()

    async def vw_after_start() -> str:
        if agent.state in ("parking_started", "notifications_prompt", "session_detail"):
            return '<hierarchy><node package="com.volkswagen.weconnect" text="Overview" bounds="[0,0][1,1]"/></hierarchy>'
        return xml(agent.state)

    agent.snapshot = vw_after_start  # type: ignore[method-assign]
    result = await _buy(agent)
    assert result.started is False and result.session is None
    assert result.after_start_texts
    assert agent.taps.count("Start Parking") == 1


async def test_already_running_parking_blocks_a_second_purchase():
    agent = FakeAgent(start="home_active")
    with pytest.raises(AlreadyActiveError) as err:
        await _buy(agent)
    assert err.value.session.plate == PLATE
    assert "search_redesign_button" not in agent.taps


async def test_error_banner_on_confirm_blocks_start():
    agent = FakeAgent(transitions={("wheel_set", "WheelTimePicker_ContinueButton"): "confirm_active_error"})
    with pytest.raises(DriverError, match="Active session"):
        await _buy(agent)
    assert "Start Parking" not in agent.taps


async def test_stop_reads_the_summary():
    agent = FakeAgent(start="home_active")
    result = await _driver(agent).stop(PLATE)
    assert agent.taps[-3:] == ["OngoingParkingDetails_StopButton", "ConfirmationSheet_ConfirmButton", "RallyTopBarBack"]
    assert result.total == 0.0 and result.service_fee == 0.0
    assert result.duration_text == "10 min"
    assert result.period_text == "Oct 26, 2026 · 8:02 PM - 8:13 PM"
    assert agent.state == "home"


async def test_stop_refuses_another_vehicle():
    agent = FakeAgent(start="home_active")
    with pytest.raises(DriverError, match="no active parking"):
        await _driver(agent).stop("9ZZ9999")
    assert "OngoingParkingDetails_StopButton" not in agent.taps


async def test_active_session_none_without_card():
    assert await _driver(FakeAgent(start="home")).active_session(PLATE) is None


async def test_price_limit_blocks_start():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="exceeds the limit"):
        await _buy(agent, max_price=10)
    assert "Start Parking" not in agent.taps


async def test_unknown_plate_stops_before_paying():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="not found"):
        await _buy(agent, plate="9ZZ9999")
    assert agent.state == "vehicles"


async def test_wrong_zone_is_never_selected():
    agent = FakeAgent()
    with pytest.raises(DriverError, match="zone P4-1940"):
        await _buy(agent, zone="P4-1940")
    assert "parking_area_list_item" not in agent.taps


async def test_quote_mismatch_end_time_blocks_start():
    driver = _driver(FakeAgent())
    with pytest.raises(DriverError, match="different end time"):
        driver._read_quote(Screen(xml("confirm")), "P4-1925", PLATE, datetime(2026, 10, 26, 21, 0), NOW)


async def test_wrong_day_is_caught_by_duration():
    driver = _driver(FakeAgent())
    with pytest.raises(DriverError, match="duration"):
        driver._read_quote(Screen(xml("confirm")), "P4-1925", PLATE, END, datetime(2026, 10, 25, 18, 59))


async def test_foreign_app_in_front_is_never_tapped():
    agent = FakeAgent(start="confirm")
    driver = _driver(agent)
    vw = '<hierarchy><node package="com.volkswagen.weconnect" text="Start Parking" clickable="true" bounds="[0,0][100,100]"/></hierarchy>'
    screen = Screen(vw)
    with pytest.raises(DriverError, match="not in the foreground"):
        await driver._tap(screen, screen.nodes[0])
    assert agent.taps == []


async def test_cold_start_home_is_recognised():
    screen = await _driver(FakeAgent(start="cold_home")).go_home()
    assert screen.first_rid("search_redesign_button") is not None


OFFER = {("home_offer", "Not now"): "home"}


async def test_offer_over_home_is_declined_before_buying():
    # 2026-10-27: an EasyPark Go trial sheet covered the map; the search tap hit the sheet.
    agent = FakeAgent(start="home_offer", transitions=OFFER)
    result = await _buy(agent)
    assert agent.taps[0] == "Not now"
    assert "Start free 30-day trial" not in agent.taps
    assert result.started is True


async def test_offer_appearing_while_waiting_is_declined():
    agent = FakeAgent(transitions={**OFFER, ("home", "search_redesign_button"): "home_offer",
                                   ("home_offer", "Not now"): "search"})
    result = await _buy(agent)
    assert agent.taps[:3] == ["search_redesign_button", "Not now", "search_locations_search_bar"]
    assert result.started is True


class FlakyStopAgent(FakeAgent):
    """EasyPark answers the first ``failures`` stop confirmations with an error dialog."""

    def __init__(self, failures: int) -> None:
        super().__init__(start="home_active", transitions={("attention", "Close"): "home_active"})
        self.failures = failures

    async def tap(self, x: int, y: int) -> None:
        before = self.state
        await super().tap(x, y)
        if before == "stop_sheet" and self.failures:
            self.failures -= 1
            self.state = "attention"


async def test_stop_is_retried_once_after_an_error_dialog():
    agent = FlakyStopAgent(failures=1)
    result = await _driver(agent).stop(PLATE)
    assert agent.taps.count("OngoingParkingDetails_StopButton") == 2
    assert "Close" in agent.taps
    assert result.total == 0.0


async def test_stop_gives_up_after_two_error_dialogs():
    agent = FlakyStopAgent(failures=2)
    with pytest.raises(DriverError, match="could not stop the parking: Attention!"):
        await _driver(agent).stop(PLATE)
    assert agent.taps.count("OngoingParkingDetails_StopButton") == 2


async def test_start_error_dialog_is_reported():
    agent = FakeAgent(transitions={("confirm", "Start Parking"): "attention", ("attention", "Close"): "home"})
    result = await _buy(agent)
    assert result.started is False
    assert any("could not be started" in n for n in result.after_start_texts)
    assert agent.taps.count("Start Parking") == 1
