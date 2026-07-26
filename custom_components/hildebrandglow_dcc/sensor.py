"""Platform for sensor integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging

import requests

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)
TARIFF_SCAN_INTERVAL = timedelta(minutes=5)

USAGE_CLASSIFIERS = ("electricity.consumption", "gas.consumption")
COST_CLASSIFIERS = ("electricity.consumption.cost", "gas.consumption.cost")


async def api_call(hass: HomeAssistant, func: Callable, description: str, *args):
    """Run a blocking Glow API call, logging any failure consistently.

    Returns None if the call failed for any reason.
    """
    try:
        result = await hass.async_add_executor_job(func, *args)
        _LOGGER.debug("Successful %s", description)
        return result
    except requests.Timeout as ex:
        _LOGGER.error("Timeout during %s: %s", description, ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect during %s: %s", description, ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.error(
                "Non-200 Status Code during %s. The Glow API may be experiencing issues",
                description,
            )
        else:
            _LOGGER.exception(
                "Unexpected exception during %s: %s. Please open an issue",
                description,
                ex,
            )
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list = []

    # Get API object from the config flow
    glowmarkt = hass.data[DOMAIN][entry.entry_id]

    # Gather all virtual entities on the account
    virtual_entities = (
        await api_call(
            hass,
            glowmarkt.get_virtual_entities,
            f"GET to {glowmarkt.url}virtualentity",
        )
        or []
    )

    for virtual_entity in virtual_entities:
        # Meters are scoped to a single virtual entity so that a cost resource is
        # never paired with a usage resource belonging to a different meter
        meters: dict = {}

        # Gather all resources for each virtual entity
        resources = (
            await api_call(
                hass,
                virtual_entity.get_resources,
                f"GET to {glowmarkt.url}virtualentity/{virtual_entity.id}/resources",
            )
            or []
        )

        # Loop through all resources and create sensors
        for resource in resources:
            if resource.classifier in USAGE_CLASSIFIERS:
                usage_sensor = Usage(hass, resource, virtual_entity)
                entities.append(usage_sensor)
                # Save the usage sensor as a meter so that the cost sensor can reference it
                meters[resource.classifier] = usage_sensor

                # Standing and Rate sensors share a single coordinator per resource
                coordinator = TariffCoordinator(hass, entry, resource)
                entities.append(Standing(coordinator, resource, virtual_entity))
                entities.append(Rate(coordinator, resource, virtual_entity))

        # Cost sensors must be created after usage sensors as they reference them as a meter
        for resource in resources:
            if resource.classifier in COST_CLASSIFIERS:
                meter_classifier = resource.classifier.removesuffix(".cost")
                meter = meters.get(meter_classifier)
                if meter is None:
                    _LOGGER.error(
                        "No matching usage sensor found for %s (id: %s). Please open an issue",
                        resource.classifier,
                        resource.id,
                    )
                    continue
                cost_sensor = Cost(hass, resource, virtual_entity)
                cost_sensor.meter = meter
                entities.append(cost_sensor)

    # Get data for all entities on initial startup
    async_add_entities(entities, update_before_add=True)

    return True


def supply_type(resource) -> str:
    """Return supply type."""
    if "electricity.consumption" in resource.classifier:
        return "electricity"
    if "gas.consumption" in resource.classifier:
        return "gas"
    _LOGGER.error("Unknown classifier: %s. Please open an issue", resource.classifier)
    return "unknown"


def device_name(resource, virtual_entity) -> str:
    """Return device name. Includes name of virtual entity if it exists."""
    supply = supply_type(resource)
    # First letter of device name should be capitalised
    if virtual_entity.name is not None:
        name = f"{virtual_entity.name} smart {supply} meter"
    else:
        name = f"Smart {supply} meter"
    return name


def should_update() -> bool:
    """Check if time is between 0-5 or 30-35 minutes past the hour."""
    minutes = datetime.now().minute
    return (0 <= minutes <= 5) or (30 <= minutes <= 35)


async def daily_data(
    hass: HomeAssistant, resource
) -> tuple[float, datetime] | tuple[None, None]:
    """Get daily usage from the API.

    Returns the total for the day along with the start of that day, which the
    cost sensor exposes as its last_reset.
    """
    # If it's before 01:06, we need to fetch yesterday's data
    # Should only need to be before 00:36 but gas data can be 30 minutes behind electricity data
    if datetime.now().time() <= time(1, 5):
        _LOGGER.debug("Fetching yesterday's data")
        now = datetime.now() - timedelta(days=1)
    else:
        now = datetime.now()
    # Timezone-aware start of the day the readings cover
    day_start = dt_util.start_of_local_day(now.date())
    # Round to the day to set time to 00:00:00
    t_from = await hass.async_add_executor_job(resource.round, now, "P1D")
    # Round to the minute
    t_to = await hass.async_add_executor_job(resource.round, now, "PT1M")

    # Tell Hildebrand to pull latest DCC data
    await api_call(hass, resource.catchup, f"catchup for resource {resource.id}")

    _LOGGER.debug(
        "Get readings from %s to %s for %s", t_from, t_to, resource.classifier
    )
    readings = await api_call(
        hass,
        resource.get_readings,
        f"readings for resource {resource.id}",
        t_from,
        t_to,
        "P1D",
        "sum",
        True,
    )
    if not readings:
        _LOGGER.debug("No readings returned for resource id %s", resource.id)
        return None, None

    _LOGGER.debug("Readings for %s has %s entries", resource.classifier, len(readings))
    total = 0.0
    # The API can split the requested day across two buckets around a DST change
    for reading in readings[:2]:
        if reading[1] is not None and reading[1].value is not None:
            total += reading[1].value
    return total, day_start


async def tariff_data(hass: HomeAssistant, resource):
    """Get tariff data from the API."""
    try:
        tariff = await hass.async_add_executor_job(resource.get_tariff)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/tariff",
            resource.id,
        )
        return tariff
    # pyglowmarkt raises UnboundLocalError when the API returns an empty tariff list
    except (UnboundLocalError, KeyError, TypeError):
        _LOGGER.warning(
            "No tariff data returned by the Glow API for the %s meter (id: %s). "
            "If you don't see tariff data for this meter in the Bright app, please "
            "disable the associated rate and standing charge sensors",
            supply_type(resource),
            resource.id,
        )
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)
    return None


class Usage(SensorEntity):
    """Sensor object for daily usage."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_name = "Usage (today)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id

        self.hass = hass
        self.initialised = False
        self.resource = resource
        self.virtual_entity = virtual_entity

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    @property
    def icon(self) -> str | None:
        """Icon to use in the frontend."""
        # Only the gas usage sensor needs an icon as the others inherit from their device class
        if self.resource.classifier == "gas.consumption":
            return "mdi:fire"
        return None

    async def async_update(self) -> None:
        """Fetch new data for the sensor."""
        # Get data on initial startup, then only when new data might be available
        if self.initialised and not should_update():
            return
        value, _ = await daily_data(self.hass, self.resource)
        if value is not None:
            self._attr_native_value = round(value, 2)
            self.initialised = True


class Cost(SensorEntity):
    """Sensor usage for daily cost."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Cost (today)"
    _attr_native_unit_of_measurement = "GBP"
    # The monetary device class only permits TOTAL. The sensor resets to zero
    # each day, so last_reset is published alongside it to mark the boundary.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id

        self.hass = hass
        self.initialised = False
        self.meter = None
        self.resource = resource
        self.virtual_entity = virtual_entity

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            # Get the identifier from the meter so that the cost sensors have the same device
            identifiers={(DOMAIN, self.meter.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    async def async_update(self) -> None:
        """Fetch new data for the sensor."""
        # Get data on initial startup, then only when new data might be available
        if self.initialised and not should_update():
            return
        value, day_start = await daily_data(self.hass, self.resource)
        if value is not None:
            self._attr_native_value = round(value / 100, 2)
            self._attr_last_reset = day_start
            self.initialised = True


class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the tariff sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, resource) -> None:
        """Initialize tariff coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing the config entry explicitly is required from HA 2026.8
            config_entry=entry,
            # Name of the data. For logging purposes.
            name="tariff",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=TARIFF_SCAN_INTERVAL,
        )

        self.resource = resource

    async def _async_update_data(self):
        """Fetch data from tariff API endpoint."""
        # Always fetch until we have data, then only when updated data might be available
        if self.data is not None and not should_update():
            # Return the previous value so the sensors keep their state
            return self.data

        tariff = await tariff_data(self.hass, self.resource)
        if tariff is None:
            if self.data is not None:
                # A transient failure shouldn't wipe out a known good value
                return self.data
            raise UpdateFailed(
                f"No tariff data available for resource {self.resource.id}"
            )
        return tariff


class BaseTariff(CoordinatorEntity, SensorEntity):
    """Base entity for the tariff sensors driven by TariffCoordinator."""

    _attr_has_entity_name = True
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self.resource = resource
        self.virtual_entity = virtual_entity

    @staticmethod
    def _extract(current_rates) -> float | None:
        """Return the raw pence value from the tariff's current rates."""
        raise NotImplementedError

    def _update_value(self) -> None:
        """Read the latest value from the coordinator without writing state."""
        tariff = self.coordinator.data
        current_rates = getattr(tariff, "current_rates", None)
        if current_rates is None:
            return
        value = self._extract(current_rates)
        if value is None:
            return
        try:
            self._attr_native_value = round(float(value) / 100, 4)
        except (TypeError, ValueError):
            _LOGGER.debug("Could not parse tariff value %s", value)

    async def async_added_to_hass(self) -> None:
        """Populate the sensor with any data the coordinator already has."""
        await super().async_added_to_hass()
        self._update_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_value()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )


class Standing(BaseTariff):
    """Sensor for the daily standing charge."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_name = "Standing charge"
    _attr_native_unit_of_measurement = "GBP"

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Initialize the standing charge sensor."""
        super().__init__(coordinator, resource, virtual_entity)

        self._attr_unique_id = resource.id + "-tariff"

    @staticmethod
    def _extract(current_rates) -> float | None:
        """Return the standing charge in pence."""
        standing_charge = getattr(current_rates, "standing_charge", None)
        return getattr(standing_charge, "value", None)


class Rate(BaseTariff):
    """Sensor for the unit rate."""

    # No device class as there isn't one for a price per unit of energy
    _attr_device_class = None
    _attr_icon = "mdi:cash-multiple"
    _attr_name = "Rate"
    _attr_native_unit_of_measurement = "GBP/kWh"

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Initialize the rate sensor."""
        super().__init__(coordinator, resource, virtual_entity)

        self._attr_unique_id = resource.id + "-rate"

    @staticmethod
    def _extract(current_rates) -> float | None:
        """Return the unit rate in pence."""
        rate = getattr(current_rates, "rate", None)
        return getattr(rate, "value", None)
