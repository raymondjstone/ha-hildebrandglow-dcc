"""The Hildebrand Glow (DCC) integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .glow_api import GlowApiClient, GlowAuthError, GlowError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hildebrand Glow (DCC) from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    client = GlowApiClient(
        async_get_clientsession(hass), entry.data["username"], entry.data["password"]
    )
    # Authenticate with the API. The client re-authenticates by itself when
    # the token approaches its 7 day expiry, so this also validates that the
    # stored credentials still work.
    try:
        await client.authenticate()
    except GlowAuthError as ex:
        # Bad credentials will never fix themselves, so prompt for
        # re-authentication instead of retrying the setup forever
        raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
    except GlowError as ex:
        raise ConfigEntryNotReady(str(ex)) from ex

    # Set API object
    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
