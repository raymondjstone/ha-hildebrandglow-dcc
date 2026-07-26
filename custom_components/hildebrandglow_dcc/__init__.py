"""The Hildebrand Glow (DCC) integration."""
from __future__ import annotations

import logging

from glowmarkt import BrightClient
import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

AUTH_ERRORS = ("Authentication failed", "Expected an authentication token")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hildebrand Glow (DCC) from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Authenticate with the API
    try:
        glowmarkt = await hass.async_add_executor_job(
            BrightClient, entry.data["username"], entry.data["password"]
        )
    except requests.Timeout as ex:
        raise ConfigEntryNotReady(f"Timeout: {ex}") from ex
    except requests.exceptions.ConnectionError as ex:
        raise ConfigEntryNotReady(f"Cannot connect: {ex}") from ex
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        # Bad credentials will never fix themselves, so prompt for re-authentication
        # instead of retrying the setup forever
        if any(error in str(ex) for error in AUTH_ERRORS):
            raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
        raise ConfigEntryNotReady(f"Unexpected exception: {ex}") from ex

    _LOGGER.debug("Successful Post to %sauth", glowmarkt.url)

    # Set API object
    hass.data[DOMAIN][entry.entry_id] = glowmarkt

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
