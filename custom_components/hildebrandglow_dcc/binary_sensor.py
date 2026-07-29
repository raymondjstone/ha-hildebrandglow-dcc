"""Platform for binary sensor integration.

Provides a connectivity sensor for Glow gateway hardware (IHD/CAD). Devices
that never report packet timestamps — such as the meters themselves on
DCC-only accounts — are skipped, so accounts without hardware simply get no
binary sensors.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import DATA_CLIENT, DOMAIN
from .glow_api import GlowApiClient, GlowDevice
from .helpers import api_call, probe_call

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
# The CAD normally reports every few seconds; treat a long silence as offline
OFFLINE_AFTER = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the binary sensor platform."""
    client: GlowApiClient = hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]

    devices = await probe_call(client.get_devices(), "GET to device") or []

    entities = []
    for device in devices:
        last_seen = await probe_call(
            client.get_device_last_seen(device.id),
            f"status probe for device {device.id}",
        )
        if last_seen is None:
            _LOGGER.debug(
                "Device %s (%s) reports no packet timestamps; "
                "skipping the connectivity sensor",
                device.id,
                device.description,
            )
            continue
        entities.append(GatewayConnectivity(client, device, last_seen))

    async_add_entities(entities)

    return True


class GatewayConnectivity(BinarySensorEntity):
    """Whether a Glow gateway (IHD/CAD) is sending data to the platform."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Connectivity"

    def __init__(
        self, client: GlowApiClient, device: GlowDevice, last_seen: datetime
    ) -> None:
        """Initialize the sensor with the timestamp probed during setup."""
        self._attr_unique_id = device.id + "-connectivity"

        self.client = client
        self.device = device
        self._last_seen = last_seen
        self._update_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.id)},
            manufacturer="Hildebrand",
            model=self.device.description or "Glow gateway",
            name=self.device.description or "Glow gateway",
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Expose when the gateway last reported and its hardware IDs."""
        attrs = {"last_seen": self._last_seen}
        if self.device.hardware_id:
            attrs["hardware_id"] = self.device.hardware_id
        if self.device.hardware_ids:
            attrs.update(
                {
                    f"hardware_id_{key}": value
                    for key, value in self.device.hardware_ids.items()
                }
            )
        return attrs

    def _update_state(self) -> None:
        """Derive connectivity from the age of the last reported packet."""
        self._attr_is_on = (
            dt_util.utcnow() - self._last_seen < OFFLINE_AFTER
        )

    async def async_update(self) -> None:
        """Fetch the latest packet timestamp for the gateway."""
        last_seen = await api_call(
            self.client.get_device_last_seen(self.device.id),
            f"status for device {self.device.id}",
        )
        if last_seen is not None:
            self._last_seen = last_seen
        # A transient API failure keeps the previous timestamp, so the
        # sensor naturally flips to disconnected once it goes stale
        self._update_state()
