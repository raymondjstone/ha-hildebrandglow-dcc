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


# Statuses that mean this account will never serve the endpoint, as opposed
# to a rate limit or an outage that will have cleared by the next attempt
UNSUPPORTED_STATUSES = (400, 401, 403, 404, 405, 501)


async def probe_call(coro: Coroutine, description: str):
    """Await an API call for a feature that may not exist on this account.

    The hardware-only endpoints (instantaneous readings, meter registers,
    device status) fail for accounts without Glow hardware, which is
    expected and logged at debug level only.

    A transient failure is different, and is logged as a warning: probes
    run once at startup, so treating an outage or a rate limit as "not
    supported" would drop the entity until the next restart.
    """
    try:
        result = await coro
        _LOGGER.debug("Successful %s", description)
        return result
    except GlowAuthError as ex:
        raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
    except GlowApiError as ex:
        if ex.status in UNSUPPORTED_STATUSES:
            _LOGGER.debug("Skipping %s: not available on this account", description)
        else:
            _LOGGER.warning(
                "Could not complete %s: %s. If an entity is missing, reload "
                "the integration once the API is responding again",
                description,
                ex,
            )
        return None
    except GlowError as ex:
        _LOGGER.warning(
            "Could not complete %s: %s. If an entity is missing, reload the "
            "integration once the API is responding again",
            description,
            ex,
        )
        return None
