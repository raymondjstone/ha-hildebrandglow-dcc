"""Shared helpers for the Hildebrand Glow (DCC) platforms."""
from __future__ import annotations

from collections.abc import Coroutine
import logging

from homeassistant.exceptions import ConfigEntryAuthFailed

from .glow_api import (
    GlowApiError,
    GlowAuthError,
    GlowConnectionError,
    GlowError,
    GlowTimeoutError,
)

_LOGGER = logging.getLogger(__name__)


async def api_call(coro: Coroutine, description: str):
    """Await a Glow API call, logging any failure consistently.

    Returns None if the call failed for any transient reason. Raises
    ConfigEntryAuthFailed if the stored credentials are rejected, so that
    Home Assistant starts a re-authentication flow.
    """
    try:
        result = await coro
        _LOGGER.debug("Successful %s", description)
        return result
    except GlowAuthError as ex:
        raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
    except GlowTimeoutError as ex:
        _LOGGER.error("Timeout during %s: %s", description, ex)
    except GlowConnectionError as ex:
        _LOGGER.error("Cannot connect during %s: %s", description, ex)
    except GlowApiError as ex:
        _LOGGER.error(
            "Error during %s: %s. The Glow API may be experiencing issues",
            description,
            ex,
        )
    return None


async def probe_call(coro: Coroutine, description: str):
    """Await an API call for a feature that may not exist on this account.

    The hardware-only endpoints (instantaneous readings, meter registers,
    device status) fail or return nothing for accounts without Glow
    hardware. That is expected, so any failure short of an auth rejection
    is logged at debug level only and treated as "feature not available".
    """
    try:
        result = await coro
        _LOGGER.debug("Successful %s", description)
        return result
    except GlowAuthError as ex:
        raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
    except GlowError as ex:
        _LOGGER.debug("Skipping %s: %s", description, ex)
        return None
