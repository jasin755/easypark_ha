"""Config flow, entities and the buy button inside Home Assistant."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.easypark_ha.agent import AgentError
from custom_components.easypark_ha.const import DOMAIN
from custom_components.easypark_ha.driver import (
    ActiveSession,
    AlreadyActiveError,
    AppInterruptedError,
    DriverError,
    NoActiveParkingError,
    PurchaseResult,
    Quote,
    StopResult,
)

DATA = {
    "host": "192.168.1.50",
    "port": 8765,
    "token": "t" * 40,
    "zone": "P4-1925",
    "plates": "1AB2345, 2CD6789",
    "max_price": 300.0,
    "return_package": "com.example.home",
}


async def test_config_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with (
        patch("custom_components.easypark_ha.config_flow.AgentClient.health", AsyncMock(return_value={"status": "ok"})),
        patch("custom_components.easypark_ha.config_flow.AgentClient.version", AsyncMock(return_value="26.40.1")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**DATA, "zone": " p4-1925 ", "plates": "1ab2345;2CD6789"}
        )
    assert result["type"] == "create_entry"
    assert result["data"]["zone"] == "P4-1925"
    assert result["data"]["plates"] == "1AB2345, 2CD6789"


async def test_config_flow_requires_easypark(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with (
        patch("custom_components.easypark_ha.config_flow.AgentClient.health", AsyncMock(return_value={"status": "ok"})),
        patch("custom_components.easypark_ha.config_flow.AgentClient.version", AsyncMock(return_value="")),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "easypark_missing"}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _session(end: datetime, plate: str = "2CD6789") -> ActiveSession:
    return ActiveSession(
        plate=plate, zone="P4-1925", ends_text=f"Ends {end:%H:%M}", ends_hm=(end.hour, end.minute),
        ends_tomorrow=False, price=3.5, service_fee=1.0, adjusted=False,
    )


def _result(end: datetime, started: bool, dry_run: bool | None = None) -> PurchaseResult:
    quote = Quote(
        zone="P4-1925", plate="2CD6789", ends=end, total=15.4, service_fee=4.9,
        duration_text="2h", zone_info="Kód úseku: P4-1925",
    )
    return PurchaseResult(
        quote=quote, started=started, dry_run=not started if dry_run is None else dry_run,
        session=_session(end) if started else None,
        after_start_texts=[] if started else ["no session"],
    )


async def test_entities_and_dry_run_default(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get("switch.easypark_test_mode").state == "on"
    assert hass.states.get("select.easypark_vehicle").attributes["options"] == ["1AB2345", "2CD6789"]
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"
    assert hass.states.get("sensor.easypark_zone").state == "P4-1925"

    buy = AsyncMock(side_effect=lambda **kw: _result(kw["end"], started=False))
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", buy),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()) as launch,
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    assert buy.await_args.kwargs["dry_run"] is True
    assert buy.await_args.kwargs["zone"] == "P4-1925"
    launch.assert_awaited_once_with("com.example.home")
    assert hass.states.get("sensor.easypark_last_result").state == "dry_run_ok"
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"


async def test_real_purchase_tracks_session_price_and_buttons(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get("button.easypark_buy_parking").state != "unavailable"
    assert hass.states.get("button.easypark_end_parking").state == "unavailable"
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.easypark_test_mode"}, blocking=True)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.easypark_vehicle", "option": "2CD6789"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.easypark_parking_duration", "value": 60}, blocking=True
    )

    buy = AsyncMock(side_effect=lambda **kw: _result(kw["end"], started=True))
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", buy),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)

    kwargs = buy.await_args.kwargs
    assert kwargs["plate"] == "2CD6789" and kwargs["dry_run"] is False
    assert timedelta(minutes=59) <= kwargs["end"] - dt_util.now() <= timedelta(minutes=60)
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "on"
    # The running session's price from EasyPark wins over the Confirm parking quote.
    price = hass.states.get("sensor.easypark_price_easypark")
    assert price.state == "3.5" and price.attributes["source"] == "running"
    assert hass.states.get("sensor.easypark_service_fee").state == "1.0"
    # While parking runs only Stop is offered.
    assert hass.states.get("button.easypark_buy_parking").state == "unavailable"
    assert hass.states.get("button.easypark_end_parking").state != "unavailable"


async def test_failure_is_reported_and_app_restored(hass: HomeAssistant) -> None:
    await _setup(hass)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", AsyncMock(side_effect=DriverError("zone missing"))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()) as launch,
        pytest.raises(HomeAssistantError, match="zone missing"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    launch.assert_awaited_once()
    assert hass.states.get("sensor.easypark_last_result").state == "error: zone missing"


async def test_options_change_agent_address(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({**DATA})["host"] == "192.168.1.50"

    new = {**DATA, "host": " 192.168.1.77 ", "port": 8766}
    with (
        patch("custom_components.easypark_ha.config_flow.AgentClient.health", AsyncMock(side_effect=AgentError("down"))),
    ):
        failed = await hass.config_entries.options.async_configure(result["flow_id"], new)
    assert failed["errors"] == {"base": "cannot_connect"}

    with (
        patch("custom_components.easypark_ha.config_flow.AgentClient.health", AsyncMock(return_value={"status": "ok"})),
        patch("custom_components.easypark_ha.config_flow.AgentClient.version", AsyncMock(return_value="26.40.1")),
        patch("custom_components.easypark_ha.AgentClient") as client,
    ):
        done = await hass.config_entries.options.async_configure(failed["flow_id"], new)
        await hass.async_block_till_done()
    assert done["type"] == "create_entry"
    assert entry.options["host"] == "192.168.1.77"
    # The entry reloaded and the Agent client now uses the new address.
    assert client.call_args.args[1:3] == ("192.168.1.77", 8766)


async def _real_mode(hass: HomeAssistant) -> None:
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.easypark_test_mode"}, blocking=True)


async def test_start_without_running_parking_is_an_error_and_not_retried(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _real_mode(hass)
    buy = AsyncMock(side_effect=lambda **kw: _result(kw["end"], started=False, dry_run=False))
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", buy),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
        pytest.raises(HomeAssistantError, match="no running parking"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    assert buy.await_count == 1
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"
    assert hass.states.get("sensor.easypark_last_result").state.startswith("error: Start Parking was tapped")


async def test_already_running_parking_is_adopted(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _real_mode(hass)
    end = dt_util.now() + timedelta(minutes=40)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy",
              AsyncMock(side_effect=AlreadyActiveError(_session(end, "1AB2345")))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
        pytest.raises(HomeAssistantError, match="already has an active parking"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    state = hass.states.get("binary_sensor.easypark_parking_active")
    assert state.state == "on" and state.attributes["plate"] == "1AB2345"


async def test_stop_clears_the_session(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _real_mode(hass)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy",
              AsyncMock(side_effect=lambda **kw: _result(kw["end"], started=True))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "on"

    stop = AsyncMock(return_value=StopResult(
        plate="2CD6789", total=7.5, duration_text="20 min", period_text="x", service_fee=4.9
    ))
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.stop", stop),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()) as launch,
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_end_parking"}, blocking=True)
    stop.assert_awaited_once_with("2CD6789")
    launch.assert_awaited_once()
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"
    result = hass.states.get("sensor.easypark_last_result")
    assert result.state == "stopped" and result.attributes["total"] == 7.5
    # The final price from EasyPark's summary replaces the running one.
    price = hass.states.get("sensor.easypark_price_easypark")
    assert price.state == "7.5" and price.attributes["source"] == "final"
    assert hass.states.get("sensor.easypark_service_fee").state == "4.9"
    assert hass.states.get("button.easypark_buy_parking").state != "unavailable"
    assert hass.states.get("button.easypark_end_parking").state == "unavailable"


async def test_refresh_reads_the_app(hass: HomeAssistant) -> None:
    await _setup(hass)
    end = dt_util.now() + timedelta(minutes=30)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.active_session",
              AsyncMock(return_value=_session(end, "1AB2345"))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.easypark_read_state_from_easypark"}, blocking=True
        )
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "on"
    assert hass.states.get("sensor.easypark_price_easypark").state == "3.5"

    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.active_session", AsyncMock(return_value=None)),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.easypark_read_state_from_easypark"}, blocking=True
        )
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"
    assert hass.states.get("sensor.easypark_last_result").state == "no_active_parking"


async def test_manual_rate_entities_of_older_versions_are_removed(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("number", DOMAIN, f"{entry.entry_id}_hourly_rate", config_entry=entry)
    registry.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_cost_so_far", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_hourly_rate") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_cost_so_far") is None


@pytest.fixture
def no_retry_wait():
    with patch("custom_components.easypark_ha.manager.RETRY_DELAY_S", 0):
        yield


async def test_buy_waits_out_a_busy_phone(hass: HomeAssistant, no_retry_wait) -> None:
    await _setup(hass)
    await _real_mode(hass)
    calls = []

    async def buy(**kw):
        calls.append(kw["now"])
        if len(calls) < 3:
            raise AppInterruptedError("com.volkswagen.weconnect is in the foreground")
        return _result(kw["end"], started=True)

    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", AsyncMock(side_effect=buy)),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    assert len(calls) == 3
    assert hass.states.get("sensor.easypark_last_result").state == "started"


async def test_buy_gives_up_after_three_retries(hass: HomeAssistant, no_retry_wait) -> None:
    await _setup(hass)
    await _real_mode(hass)
    buy = AsyncMock(side_effect=AppInterruptedError("busy"))
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy", buy),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
        pytest.raises(HomeAssistantError, match="busy"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)
    assert buy.await_count == 4


async def _running(hass: HomeAssistant) -> None:
    await _real_mode(hass)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.buy",
              AsyncMock(side_effect=lambda **kw: _result(kw["end"], started=True))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_buy_parking"}, blocking=True)


async def test_interrupted_stop_that_already_stopped_counts_as_stopped(hass: HomeAssistant, no_retry_wait) -> None:
    await _setup(hass)
    await _running(hass)
    stop = AsyncMock(side_effect=[AppInterruptedError("busy"), NoActiveParkingError("2CD6789 has no active parking")])
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.stop", stop),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_end_parking"}, blocking=True)
    assert stop.await_count == 2
    assert hass.states.get("binary_sensor.easypark_parking_active").state == "off"
    assert hass.states.get("sensor.easypark_last_result").state == "stopped"


async def test_stop_without_running_parking_is_an_error(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _running(hass)
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.stop",
              AsyncMock(side_effect=NoActiveParkingError("2CD6789 has no active parking"))),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
        pytest.raises(HomeAssistantError, match="no active parking"),
    ):
        await hass.services.async_call("button", "press", {"entity_id": "button.easypark_end_parking"}, blocking=True)


async def test_refresh_waits_out_a_busy_phone(hass: HomeAssistant, no_retry_wait) -> None:
    await _setup(hass)
    read = AsyncMock(side_effect=[AppInterruptedError("busy"), None])
    with (
        patch("custom_components.easypark_ha.manager.EasyParkDriver.active_session", read),
        patch("custom_components.easypark_ha.manager.AgentClient.launch", AsyncMock()),
    ):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.easypark_read_state_from_easypark"}, blocking=True
        )
    assert read.await_count == 2
    assert hass.states.get("sensor.easypark_last_result").state == "no_active_parking"
