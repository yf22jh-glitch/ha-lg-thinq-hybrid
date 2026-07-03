"""Constants for the LG ThinQ Hybrid (my_lg) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "my_lg"

# Config entry data keys
CONF_ACCESS_TOKEN = "access_token"
CONF_COUNTRY = "country"
CONF_CLIENT_ID = "client_id"

# Optional wideq (LG internal API) credentials — enables fields the PAT API
# cannot provide (AC realtime power/energy, dehumidifier water tank, etc.).
CONF_WIDEQ_TOKEN = "wideq_token"
CONF_WIDEQ_CLIENT_ID = "wideq_client_id"
CONF_LANGUAGE = "language"

DEFAULT_COUNTRY = "KR"
DEFAULT_LANGUAGE = "ko-KR"

# client_id prefix — MUST differ from the official lg_thinq integration
# ("home-assistant-...") so the two do not kick each other off the AWS IoT
# MQTT broker (one connection per client_id).
CLIENT_ID_PREFIX = "home-assistant-mylg"

# --- Platforms ---
PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.HUMIDIFIER,
    Platform.BINARY_SENSOR,
]

# --- ThinQ device types (thinqconnect deviceType strings) ---
DEVICE_TYPE_AIR_CONDITIONER = "DEVICE_AIR_CONDITIONER"
DEVICE_TYPE_DEHUMIDIFIER = "DEVICE_DEHUMIDIFIER"
DEVICE_TYPE_WASHTOWER = "DEVICE_WASHTOWER"
DEVICE_TYPE_STYLER = "DEVICE_STYLER"

# Whitelist: only these device types are set up by this integration.
# Everything else stays on the official lg_thinq integration.
# (Stage 3.5+ adds washtower / styler.)
SUPPORTED_DEVICE_TYPES: set[str] = {
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_DEHUMIDIFIER,
}

# --- MQTT push message types (thinqconnect) ---
PUSH_TYPE_DEVICE_STATUS = "DEVICE_STATUS"
PUSH_TYPE_DEVICE_PUSH = "DEVICE_PUSH"

# DEVICE_PUSH codes we act on. WATER_IS_FULL is dehumidifier water tank
# (edge event); it triggers a prompt wideq refresh to update the level sensor.
PUSH_CODE_WATER_IS_FULL = "WATER_IS_FULL"
WATER_PUSH_CODES: set[str] = {PUSH_CODE_WATER_IS_FULL}

# --- Polling intervals (seconds) ---
# PAT REST is only a low-frequency fallback; MQTT push carries realtime state.
PAT_FALLBACK_INTERVAL = 3600
# MQTT subscription refresh (event subscription has an expiry).
MQTT_SUBSCRIPTION_REFRESH_INTERVAL = 86400

# --- wideq polling intervals (Stage 2+; user-configurable via OptionsFlow) ---
# Defaults and HARD FLOORS. Floors are enforced in the options flow so a user
# can never reintroduce the 30s-polling that caused the original 24h block.
OPT_AC_ACTIVE_INTERVAL = "ac_active_interval"
OPT_APPLIANCE_ACTIVE_INTERVAL = "appliance_active_interval"
OPT_IDLE_INTERVAL = "idle_interval"

DEFAULT_AC_ACTIVE_INTERVAL = 120
DEFAULT_APPLIANCE_ACTIVE_INTERVAL = 300
DEFAULT_IDLE_INTERVAL = 1800

MIN_AC_ACTIVE_INTERVAL = 60
MIN_APPLIANCE_ACTIVE_INTERVAL = 300
MIN_IDLE_INTERVAL = 600

# Global wideq rate-limiter backstop (applies regardless of option values).
WIDEQ_MAX_CALLS_PER_HOUR = 200
WIDEQ_MIN_CALL_SPACING = 3.0
