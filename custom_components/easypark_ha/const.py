"""Constants for the EasyPark (Android companion) integration."""
from __future__ import annotations

DOMAIN = "easypark_ha"

EASYPARK_PACKAGE = "net.easypark.android"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_ZONE = "zone"
CONF_PLATES = "plates"
CONF_RETURN_PACKAGE = "return_package"
CONF_MAX_PRICE = "max_price"

DEFAULT_PORT = 8765
DEFAULT_RETURN_PACKAGE = "com.volkswagen.weconnect"
DEFAULT_MAX_PRICE = 300.0
DEFAULT_DURATION_MIN = 120

STORAGE_VERSION = 1
