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
from custom_components.easypark_ha.driver import DriverError, PurchaseResult, Quote

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


def _result(end: datetime, started: bool) -> PurchaseResult:
    quote = Quote(
        zone="P4-1925", plate="2CD6789", ends=end, total=15.4, service_fee=4.9,
        duration_text="2h", zone_info="Kód úseku: P4-1925",
    )
    return PurchaseResult(quote=quote, started=started, dry_run=not started)


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


async def test_real_purchase_tracks_session_and_cost(hass: HomeAssistant) -> None:
    await _setup(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.easypark_test_mode"}, blocking=True)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.easypark_vehicle", "option": "2CD6789"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.easypark_hourly_rate", "value": 40}, blocking=True
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
    assert hass.states.get("sensor.easypark_quoted_total").state == "15.4"

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    manager = entry.runtime_data
    manager.session["started"] = (dt_util.now() - timedelta(minutes=30)).isoformat()
    assert manager.cost_so_far() == pytest.approx(20.0, abs=0.1)


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
