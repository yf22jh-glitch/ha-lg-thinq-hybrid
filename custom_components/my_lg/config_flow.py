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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CLIENT_ID_PREFIX,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY,
    DEFAULT_AC_ACTIVE_INTERVAL,
    DEFAULT_APPLIANCE_ACTIVE_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    MIN_AC_ACTIVE_INTERVAL,
    MIN_APPLIANCE_ACTIVE_INTERVAL,
    MIN_IDLE_INTERVAL,
    OPT_AC_ACTIVE_INTERVAL,
    OPT_APPLIANCE_ACTIVE_INTERVAL,
    OPT_IDLE_INTERVAL,
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
                return self.async_create_entry(
                    title=f"LG ThinQ Hybrid ({count} devices)",
                    data={
                        CONF_ACCESS_TOKEN: token,
                        CONF_COUNTRY: country,
                        CONF_CLIENT_ID: client_id,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCESS_TOKEN): str,
                vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MyLgOptionsFlow()


class MyLgOptionsFlow(OptionsFlow):
    """Polling-interval options. Hard floors prevent unsafe (block-inducing) values."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_AC_ACTIVE_INTERVAL,
                    default=opts.get(OPT_AC_ACTIVE_INTERVAL, DEFAULT_AC_ACTIVE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_AC_ACTIVE_INTERVAL)),
                vol.Required(
                    OPT_APPLIANCE_ACTIVE_INTERVAL,
                    default=opts.get(
                        OPT_APPLIANCE_ACTIVE_INTERVAL, DEFAULT_APPLIANCE_ACTIVE_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_APPLIANCE_ACTIVE_INTERVAL)),
                vol.Required(
                    OPT_IDLE_INTERVAL,
                    default=opts.get(OPT_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_IDLE_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
