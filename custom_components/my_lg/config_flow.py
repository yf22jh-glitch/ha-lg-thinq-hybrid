"""Config & options flow for LG ThinQ Hybrid (my_lg)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CLIENT_ID_PREFIX,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY,
    CONF_LANGUAGE,
    CONF_WIDEQ_CLIENT_ID,
    CONF_WIDEQ_TOKEN,
    DEFAULT_AC_ACTIVE_INTERVAL,
    DEFAULT_APPLIANCE_ACTIVE_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_LANGUAGE,
    DOMAIN,
    MIN_AC_ACTIVE_INTERVAL,
    MIN_APPLIANCE_ACTIVE_INTERVAL,
    MIN_IDLE_INTERVAL,
    OPT_AC_ACTIVE_INTERVAL,
    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
    OPT_ALLOW_HAZARDOUS_CONTROLS,
    OPT_APPLIANCE_ACTIVE_INTERVAL,
    OPT_IDLE_INTERVAL,
)
from .local_provider import (
    OPT_LOCAL_BINDINGS,
    LocalProviderConfigurationError,
    local_bindings_for_form,
    merge_local_shadow_options,
)
from .rethink_event_relay import (
    CONF_RETHINK_EVENT_TOKEN,
    MAX_TOKEN_LENGTH,
    MIN_TOKEN_LENGTH,
)

_LOGGER = logging.getLogger(__name__)


async def _validate(hass, token: str, country: str, client_id: str) -> int:
    """Return device count if credentials work, else raise."""
    from thinqconnect import ThinQApi

    session = async_get_clientsession(hass)
    api = ThinQApi(
        session=session,
        access_token=token,
        country_code=country,
        client_id=client_id,
    )
    devices = await api.async_get_device_list()
    return len(devices or [])


class MyLgConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            token = user_input[CONF_ACCESS_TOKEN].strip()
            country = user_input.get(CONF_COUNTRY, DEFAULT_COUNTRY).strip().upper()
            # Fresh client_id, distinct from official lg_thinq.
            client_id = f"{CLIENT_ID_PREFIX}-{uuid.uuid4()}"
            try:
                count = await _validate(self.hass, token, country, client_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_ACCESS_TOKEN: token,
                    CONF_COUNTRY: country,
                    CONF_CLIENT_ID: client_id,
                    CONF_LANGUAGE: DEFAULT_LANGUAGE,
                }
                # Optional wideq credentials (Stage 2+: AC power/energy, etc.).
                if user_input.get(CONF_WIDEQ_TOKEN, "").strip():
                    data[CONF_WIDEQ_TOKEN] = user_input[CONF_WIDEQ_TOKEN].strip()
                    if user_input.get(CONF_WIDEQ_CLIENT_ID, "").strip():
                        data[CONF_WIDEQ_CLIENT_ID] = user_input[
                            CONF_WIDEQ_CLIENT_ID
                        ].strip()
                return self.async_create_entry(
                    title=f"LG ThinQ Hybrid ({count} devices)", data=data
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_TOKEN): str,
                vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
                vol.Optional(CONF_WIDEQ_TOKEN, default=""): str,
                vol.Optional(CONF_WIDEQ_CLIENT_ID, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MyLgOptionsFlow()


class MyLgOptionsFlow(OptionsFlow):
    """Polling-interval options. Hard floors prevent unsafe (block-inducing) values."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        form_defaults = dict(opts)
        try:
            local_bindings_default = await self.hass.async_add_executor_job(
                local_bindings_for_form, opts
            )
        except LocalProviderConfigurationError:
            local_bindings_default = "[]"
        if user_input is not None:
            try:
                normalized = await self.hass.async_add_executor_job(
                    merge_local_shadow_options, user_input, opts
                )
            except LocalProviderConfigurationError:
                errors["base"] = "local_shadow_invalid"
                form_defaults.update(
                    {
                        key: value
                        for key, value in user_input.items()
                        if key != OPT_LOCAL_BINDINGS
                    }
                )
            else:
                return self.async_create_entry(title="", data=normalized)

        schema = vol.Schema(
            {
                vol.Required(
                    OPT_AC_ACTIVE_INTERVAL,
                    default=form_defaults.get(
                        OPT_AC_ACTIVE_INTERVAL, DEFAULT_AC_ACTIVE_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_AC_ACTIVE_INTERVAL)),
                vol.Required(
                    OPT_APPLIANCE_ACTIVE_INTERVAL,
                    default=form_defaults.get(
                        OPT_APPLIANCE_ACTIVE_INTERVAL, DEFAULT_APPLIANCE_ACTIVE_INTERVAL
                    ),
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_APPLIANCE_ACTIVE_INTERVAL)
                ),
                vol.Required(
                    OPT_IDLE_INTERVAL,
                    default=form_defaults.get(OPT_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_IDLE_INTERVAL)),
                vol.Optional(
                    OPT_ALLOW_HAZARDOUS_CONTROLS,
                    default=form_defaults.get(OPT_ALLOW_HAZARDOUS_CONTROLS, False),
                ): bool,
                vol.Optional(
                    OPT_ALLOW_EXPERIMENTAL_CONTROLS,
                    default=form_defaults.get(OPT_ALLOW_EXPERIMENTAL_CONTROLS, False),
                ): bool,
                vol.Optional(
                    CONF_RETHINK_EVENT_TOKEN,
                    default=form_defaults.get(CONF_RETHINK_EVENT_TOKEN, ""),
                ): vol.Any(
                    "",
                    vol.All(
                        selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.PASSWORD
                            )
                        ),
                        vol.Length(min=MIN_TOKEN_LENGTH, max=MAX_TOKEN_LENGTH),
                    ),
                ),
                vol.Required(
                    OPT_LOCAL_BINDINGS,
                    default=local_bindings_default,
                ): vol.All(str, vol.Length(max=64 * 1024)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
