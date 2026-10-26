from unittest.mock import MagicMock, patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_real_http_session():
    """The Agent client never talks to a phone in tests."""
    with (
        patch("custom_components.easypark_ha.async_get_clientsession", return_value=MagicMock()),
        patch("custom_components.easypark_ha.config_flow.async_get_clientsession", return_value=MagicMock()),
    ):
        yield
