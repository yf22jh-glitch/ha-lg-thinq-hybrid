"""Config flow for Kocom Energy."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import API
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .exceptions import IpAddressNotFoundError
from .util import md5_hashing, string_to_padded_hex

_LOGGER = logging.getLogger(__name__)

_SERVER_LOOKUP_URL = "http://221.141.3.28/SvrInfo.php?uid={username}"
_INTERVALS = {300: "5분", 3600: "1시간", 86400: "1일"}


def _account_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("ip", default=defaults.get("ip", "")): str,
            vol.Required("username", default=defaults.get("username", "")): str,
            vol.Required("password"): str,
            vol.Required(
                "update_interval",
                default=defaults.get("update_interval", DEFAULT_UPDATE_INTERVAL),
            ): vol.In(_INTERVALS),
        }
    )


def _protocol_credentials(user_input: dict[str, Any]) -> dict[str, str]:
    """Transform account fields into the legacy fixed-width protocol fields."""
    return {
        "ip": user_input["ip"],
        "username": string_to_padded_hex(md5_hashing(user_input["username"]), 80),
        "password": string_to_padded_hex(md5_hashing(user_input["password"]), 80),
        "fcm": string_to_padded_hex("", 512),
        "phone": string_to_padded_hex("", 32),
    }


class KocomEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a Kocom Energy account."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                session = async_get_clientsession(self.hass)
                async with asyncio.timeout(10):
                    response = await session.get(
                        _SERVER_LOOKUP_URL.format(username=user_input["username"])
                    )
                    response.raise_for_status()
                    response_text = await response.text()
                match = re.search(r"3 => ([\d.]+)", response_text)
                if not match:
                    raise IpAddressNotFoundError
                return self.async_show_form(
                    step_id="account",
                    data_schema=_account_schema(
                        {
                            "ip": match.group(1),
                            "username": user_input["username"],
                            "update_interval": DEFAULT_UPDATE_INTERVAL,
                        }
                    ),
                )
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except TimeoutError:
                errors["base"] = "timeout"
            except IpAddressNotFoundError:
                errors["base"] = "ip_not_found"
            except Exception:
                _LOGGER.exception("Unexpected error while discovering Kocom server")
                errors["base"] = "unknown_error"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("username"): str}),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _protocol_credentials(user_input)
            if await API(**credentials).authenticate():
                await self.async_set_unique_id(user_input["username"].casefold())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"사용자({user_input['username']})",
                    data={
                        **credentials,
                        "update_interval": user_input["update_interval"],
                        "original_username": user_input["username"],
                    },
                )
            errors["base"] = "auth_error"

        return self.async_show_form(
            step_id="account",
            data_schema=_account_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return KocomEnergyOptionsFlow()


class KocomEnergyOptionsFlow(config_entries.OptionsFlow):
    """Update the polling interval without rewriting credential data."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            "update_interval",
            self.config_entry.data.get("update_interval", DEFAULT_UPDATE_INTERVAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "update_interval", default=current_interval
                    ): vol.In(_INTERVALS)
                }
            ),
        )
