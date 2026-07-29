"""Services for the Hildebrand Glow (DCC) integration.

Both services address the same problem: the platform is serving readings
that look stale or wrong. Catchup asks it to pull fresh data from the DCC,
while clearing the cache makes it answer the next request from the
underlying data rather than what it cached earlier.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import (
    DATA_CLIENT,
    DATA_RESOURCES,
    DOMAIN,
    SERVICE_CATCHUP,
    SERVICE_CLEAR_CACHE,
)
from .glow_api import GlowApiClient, GlowError

_LOGGER = logging.getLogger(__name__)


def _targets(hass: HomeAssistant, call: ServiceCall) -> list[tuple[GlowApiClient, str]]:
    """Resolve a service call to the (client, resource id) pairs to act on.

    With no target the call covers every meter the integration knows about;
    otherwise the targeted devices are mapped back to their resources.
    """
    entries: dict = hass.data.get(DOMAIN, {})
    device_ids = call.data.get("device_id") or []
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    if not device_ids:
        return [
            (data[DATA_CLIENT], resource_id)
            for data in entries.values()
            for resource_id in sorted(data[DATA_RESOURCES])
        ]

    registry = dr.async_get(hass)
    targets = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            raise HomeAssistantError(f"Unknown device {device_id}")
        for entry_id in device.config_entries:
            data = entries.get(entry_id)
            if data is None:
                continue
            # Meter devices are identified by their resource ID; other
            # devices, such as gateways, have no resource to act on
            for domain, identifier in device.identifiers:
                if domain == DOMAIN and identifier in data[DATA_RESOURCES]:
                    targets.append((data[DATA_CLIENT], identifier))

    if not targets:
        raise HomeAssistantError(
            "No Hildebrand Glow meters found for the selected devices"
        )
    return targets


async def _async_run(
    hass: HomeAssistant, call: ServiceCall, action: str, description: str
) -> None:
    """Call a client method for every resource the service targets."""
    failures = []
    for client, resource_id in _targets(hass, call):
        try:
            await getattr(client, action)(resource_id)
            _LOGGER.debug("%s succeeded for resource %s", description, resource_id)
        except GlowError as ex:
            _LOGGER.error("%s failed for resource %s: %s", description, resource_id, ex)
            failures.append(resource_id)

    if failures:
        raise HomeAssistantError(
            f"{description} failed for {len(failures)} meter(s): {', '.join(failures)}"
        )


async def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services, once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_CATCHUP):
        return

    async def handle_catchup(call: ServiceCall) -> None:
        """Ask the platform to pull the latest readings from the DCC."""
        await _async_run(hass, call, "catchup", "Catchup")

    async def handle_clear_cache(call: ServiceCall) -> None:
        """Drop the platform's cached data for the targeted meters."""
        await _async_run(hass, call, "clear_cache", "Cache clear")

    hass.services.async_register(DOMAIN, SERVICE_CATCHUP, handle_catchup)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_CACHE, handle_clear_cache)
