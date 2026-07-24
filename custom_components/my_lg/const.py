"""Constants for the LG ThinQ Hybrid (my_lg) integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "my_lg"
SERVICE_WIDEQ_COMMAND = "wideq_command"

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
    Platform.FAN,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.TIME,
    Platform.TEXT,
]
# NUMBER: fridge/freezer target-temp (write via temperatureInUnits + locationName).
# BUTTON: washer/dryer/styler operation (START/STOP) via *OperationMode.

# --- ThinQ device types (thinqconnect deviceType strings) ---
DEVICE_TYPE_AIR_CONDITIONER = "DEVICE_AIR_CONDITIONER"
DEVICE_TYPE_DEHUMIDIFIER = "DEVICE_DEHUMIDIFIER"
DEVICE_TYPE_HUMIDIFIER = "DEVICE_HUMIDIFIER"
DEVICE_TYPE_AIR_PURIFIER = "DEVICE_AIR_PURIFIER"
DEVICE_TYPE_WASHTOWER = "DEVICE_WASHTOWER"
DEVICE_TYPE_STYLER = "DEVICE_STYLER"
DEVICE_TYPE_DISH_WASHER = "DEVICE_DISH_WASHER"
DEVICE_TYPE_REFRIGERATOR = "DEVICE_REFRIGERATOR"
DEVICE_TYPE_KIMCHI_REFRIGERATOR = "DEVICE_KIMCHI_REFRIGERATOR"
DEVICE_TYPE_WATER_PURIFIER = "DEVICE_WATER_PURIFIER"
DEVICE_TYPE_OVEN = "DEVICE_OVEN"
DEVICE_TYPE_COOKTOP = "DEVICE_COOKTOP"

# Whitelist: device types this integration sets up. Stage 5 = all our devices
# (my_lg fully replaces both the official lg_thinq and the smartthinq fork).
SUPPORTED_DEVICE_TYPES: set[str] = {
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_AIR_PURIFIER,
    DEVICE_TYPE_WASHTOWER,
    DEVICE_TYPE_STYLER,
    DEVICE_TYPE_DISH_WASHER,
    DEVICE_TYPE_REFRIGERATOR,
    DEVICE_TYPE_KIMCHI_REFRIGERATOR,
    DEVICE_TYPE_WATER_PURIFIER,
    DEVICE_TYPE_OVEN,
    DEVICE_TYPE_COOKTOP,
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

# Startup calls use the official PAT API and are independent from the WideQ
# limiter below.  Keep a small bounded fan-out so 16 devices do not initialize
# serially, while avoiding an unbounded restart burst against LG.
PAT_DEVICE_LIST_TIMEOUT = 30
PAT_PREPARE_CONCURRENCY = 3
PAT_PREPARE_CALL_TIMEOUT = 15
MQTT_SETUP_CALL_TIMEOUT = 20
MQTT_SUBSCRIBE_CONCURRENCY = 3
MQTT_SUBSCRIBE_CALL_TIMEOUT = 15

# --- wideq polling intervals (Stage 2+; user-configurable via OptionsFlow) ---
# Defaults and HARD FLOORS. Floors are enforced in the options flow so a user
# can never reintroduce the 30s-polling that caused the original 24h block.
OPT_AC_ACTIVE_INTERVAL = "ac_active_interval"
OPT_APPLIANCE_ACTIVE_INTERVAL = "appliance_active_interval"
OPT_IDLE_INTERVAL = "idle_interval"
OPT_ALLOW_HAZARDOUS_CONTROLS = "allow_hazardous_controls"
OPT_ALLOW_EXPERIMENTAL_CONTROLS = "allow_experimental_controls"

DEFAULT_AC_ACTIVE_INTERVAL = 600
DEFAULT_APPLIANCE_ACTIVE_INTERVAL = 300
DEFAULT_IDLE_INTERVAL = 1800

MIN_AC_ACTIVE_INTERVAL = 60
MIN_APPLIANCE_ACTIVE_INTERVAL = 300
MIN_IDLE_INTERVAL = 600

# Global wideq rate-limiter backstop (applies regardless of option values).
WIDEQ_MAX_CALLS_PER_HOUR = 200
WIDEQ_MIN_CALL_SPACING = 3.0

# After repeated wideq failures, suspend normal polling and issue one real
# snapshot request at this cadence. The request doubles as a recovery probe:
# success supplies fresh data and restores the MQTT-derived normal interval.
WIDEQ_CIRCUIT_FAILURE_THRESHOLD = 3
WIDEQ_PROBE_INTERVAL = 900

# Energy history is a separate ThinQ Web endpoint, not part of the normal
# all-device snapshot. Keep it deliberately slow and retain the last successful
# values when that optional endpoint is unavailable. A failed batch waits longer
# before trying again and never opens/closes the main snapshot circuit.
WIDEQ_ENERGY_HISTORY_INTERVAL = 1800
WIDEQ_ENERGY_HISTORY_FAILURE_RETRY = 3600
WIDEQ_ENERGY_HISTORY_STORE_VERSION = 3
WIDEQ_ENERGY_HISTORY_PREVIOUS_STORE_VERSION = 2
WIDEQ_ENERGY_HISTORY_LEGACY_STORE_VERSION = 1
WIDEQ_ENERGY_HISTORY_STORE_SAVE_DELAY = 5
WIDEQ_DEVICE_MAP_STORE_VERSION = 1
