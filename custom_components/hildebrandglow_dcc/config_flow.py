"""Config flow for Hildebrand Glow (DCC) integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .glow_api import GlowApiClient, GlowAuthError, GlowConnectionError, GlowTimeoutError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("password"): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    client = GlowApiClient(
        async_get_clientsession(hass), data["username"], data["password"]
    )
    await client.authenticate()

    # Return title of the entry to be added
    return {"title": "Hildebrand Glow (DCC)"}


def _error_key(ex: Exception) -> str:
    """Map an exception raised by the API client onto a config flow error key."""
    if isinstance(ex, GlowTimeoutError):
        _LOGGER.debug("Timeout: %s", ex)
        return "timeout_connect"
    if isinstance(ex, GlowConnectionError):
        _LOGGER.debug("Cannot connect: %s", ex)
        return "cannot_connect"
    if isinstance(ex, GlowAuthError):
        _LOGGER.debug("Authentication failed: %s", ex)
        return "invalid_auth"
    _LOGGER.exception("Unexpected exception: %s", ex)
    return "unknown"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hildebrand Glow (DCC)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # If left empty, simply show the form again
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors: dict[str, str] = {}

        # Test authenticating with the API
        try:
            info = await validate_input(self.hass, user_input)
        except Exception as ex:  # pylint: disable=broad-except
            errors["base"] = _error_key(ex)
        else:
            # Only claim the unique ID once the credentials are known to be valid
            await self.async_set_unique_id(user_input["username"].lower())
            self._abort_if_unique_id_configured()

            return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]  # pylint: disable=unused-argument
    ) -> ConfigFlowResult:
        """Handle re-authentication when the stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for a new password."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            data = {**reauth_entry.data, "password": user_input["password"]}
            try:
                await validate_input(self.hass, data)
            except Exception as ex:  # pylint: disable=broad-except
                errors["base"] = _error_key(ex)
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates={"password": user_input["password"]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"username": reauth_entry.data["username"]},
            errors=errors,
        )
