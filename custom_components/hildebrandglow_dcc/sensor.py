"""Platform for sensor integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import DATA_CLIENT, DATA_RESOURCES, DOMAIN
from .glow_api import (
    GlowApiClient,
    GlowApiError,
    GlowAuthError,
    GlowConnectionError,
    GlowTimeoutError,
    TariffRates,
)
from .helpers import api_call, probe_call

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)
TARIFF_SCAN_INTERVAL = timedelta(minutes=5)
# The CAD pushes new instantaneous readings every few seconds, but polling a
# cloud API that fast would hammer it; once a minute is a fair compromise
POWER_SCAN_INTERVAL = timedelta(seconds=60)
# If the CAD stops reporting, /current keeps echoing its last value. Mark the
# power sensor unavailable rather than showing a stale figure forever
POWER_STALE_AFTER = timedelta(minutes=10)

USAGE_CLASSIFIERS = ("electricity.consumption", "gas.consumption")
COST_CLASSIFIERS = ("electricity.consumption.cost", "gas.consumption.cost")

# The API reports the unit alongside instantaneous and register readings,
# and is not consistent about which one it uses: meter registers come back
# in Wh on some meters and kWh on others. The reported unit is therefore
# always honoured rather than assumed
ENERGY_UNITS = {
    "wh": UnitOfEnergy.WATT_HOUR,
    "kwh": UnitOfEnergy.KILO_WATT_HOUR,
    "mwh": UnitOfEnergy.MEGA_WATT_HOUR,
}
VOLUME_UNITS = {
    "m3": UnitOfVolume.CUBIC_METERS,
    "m³": UnitOfVolume.CUBIC_METERS,
    "ft3": UnitOfVolume.CUBIC_FEET,
    "ft³": UnitOfVolume.CUBIC_FEET,
}
POWER_UNITS = {
    "w": UnitOfPower.WATT,
    "kw": UnitOfPower.KILO_WATT,
}


def normalise_unit(units: str | None) -> str:
    """Return an API unit string in the form used by the lookup tables."""
    return (units or "").strip().lower()


def reading_unit(units: str | None) -> tuple[str | None, str | None]:
    """Map a unit reported by the API to a device class and a HA unit.

    Returns (None, None) for a unit that isn't recognised, so the caller
    can surface the raw value rather than mislabelling it.
    """
    key = normalise_unit(units)
    if key in ENERGY_UNITS:
        return SensorDeviceClass.ENERGY, ENERGY_UNITS[key]
    if key in VOLUME_UNITS:
        return SensorDeviceClass.GAS, VOLUME_UNITS[key]
    return None, None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list = []
    # Usage sensors across every virtual entity, for meter point matching
    all_usage_sensors: list[Usage] = []

    # Get API object from the config flow
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: GlowApiClient = entry_data[DATA_CLIENT]

    # Gather all virtual entities on the account
    virtual_entities = (
        await api_call(client.get_virtual_entities(), "GET to virtualentity") or []
    )

    for virtual_entity in virtual_entities:
        # Meters are scoped to a single virtual entity so that a cost resource is
        # never paired with a usage resource belonging to a different meter
        meters: dict = {}

        # Gather all resources for each virtual entity
        resources = (
            await api_call(
                client.get_resources(virtual_entity.id),
                f"GET to virtualentity/{virtual_entity.id}/resources",
            )
            or []
        )

        # Loop through all resources and create sensors
        for resource in resources:
            if resource.active is False:
                _LOGGER.debug(
                    "Resource %s (%s) is marked inactive by the API; its "
                    "sensors may not receive data",
                    resource.id,
                    resource.classifier,
                )
            if resource.classifier in USAGE_CLASSIFIERS:
                # Let the services act on this meter
                entry_data[DATA_RESOURCES].add(resource.id)
                usage_sensor = Usage(client, resource, virtual_entity)
                entities.append(usage_sensor)
                all_usage_sensors.append(usage_sensor)
                # Save the usage sensor as a meter so that the cost sensor can reference it
                meters[resource.classifier] = usage_sensor

                # Record the meter's own number on the device when the API
                # can report which physical meter sources this resource
                meter_device = await probe_call(
                    client.get_device_for_resource(resource.id),
                    f"device probe for resource {resource.id}",
                )
                if meter_device is not None and meter_device.hardware_id:
                    usage_sensor.serial_number = meter_device.hardware_id

                # Standing and Rate sensors share a single coordinator per resource
                coordinator = TariffCoordinator(hass, entry, client, resource)
                entities.append(Standing(coordinator, resource, virtual_entity))
                entities.append(Rate(coordinator, resource, virtual_entity))

                # The remaining sensors need Glow hardware (IHD/CAD) on the
                # account, so probe each endpoint once and only create the
                # sensor if the account supports it
                entities.extend(
                    await hardware_sensors(hass, entry, client, resource, virtual_entity)
                )

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
                cost_sensor = Cost(client, resource, virtual_entity)
                cost_sensor.meter = meter
                entities.append(cost_sensor)

                # Tariff history is documented against the cost resource.
                # Not every account has it, so probe once at setup
                history = await probe_call(
                    client.get_tariff_list(resource.id),
                    f"tariff list probe for resource {resource.id}",
                )
                if history:
                    entities.append(
                        TariffHistory(client, resource, meter, virtual_entity, history)
                    )
                else:
                    _LOGGER.debug(
                        "No tariff history for resource %s; skipping the "
                        "tariff sensor",
                        resource.id,
                    )

    # Meter points (MPAN/MPRN) are account-wide, so handle them after all
    # virtual entities have been walked
    entities.extend(await meter_point_sensors(client, all_usage_sensors))

    # Get data for all entities on initial startup
    async_add_entities(entities, update_before_add=True)

    return True


async def meter_point_sensors(
    client: GlowApiClient, usage_sensors: list[Usage]
) -> list[SensorEntity]:
    """Create a diagnostic sensor for each meter point on the account.

    Each sensor carries the meter point number (MPAN/MPRN), its verification
    and consent state, and the DCC inventory of the devices behind it. All
    endpoints are probed, so accounts where they fail just get no sensors.
    """
    sensors: list[SensorEntity] = []
    meter_points = (
        await probe_call(client.get_meter_points(), "meter point verification probe")
        or []
    )

    for point in meter_points:
        # Attach to the right meter's device via the API's own mapping of
        # meter point to resources, falling back to the supply type when
        # the account only has one meter of that kind
        resource_ids = (
            await probe_call(
                client.get_meter_point_resources(point.mpxn),
                f"resources probe for meter point {point.mpxn}",
            )
            or []
        )
        meter = next(
            (u for u in usage_sensors if u.resource.id in resource_ids), None
        )
        if meter is None:
            supply = "electricity" if point.kind == "mpan" else "gas"
            candidates = [
                u for u in usage_sensors if supply_type(u.resource) == supply
            ]
            if len(candidates) == 1:
                meter = candidates[0]

        inventory = (
            await probe_call(
                client.get_meter_point_inventory(point.mpxn),
                f"inventory probe for meter point {point.mpxn}",
            )
            or []
        )

        # The inventory can also supply the meter's number if the device
        # lookup couldn't
        if meter is not None and meter.serial_number is None:
            wanted = "ESME" if point.kind == "mpan" else "GSME"
            for entry in inventory:
                if entry.get("DeviceType") == wanted and entry.get("EUI"):
                    meter.serial_number = str(entry["EUI"])
                    break

        sensors.append(MeterPointSensor(client, point, inventory, meter))

    return sensors


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
    minutes = dt_util.now().minute
    return (0 <= minutes <= 5) or (30 <= minutes <= 35)


async def daily_data(
    client: GlowApiClient, resource
) -> tuple[float, datetime] | tuple[None, None]:
    """Get daily usage from the API.

    Returns the total for the day along with the start of that day, which the
    cost sensor exposes as its last_reset.
    """
    # Every time here comes from dt_util so that it follows the timezone
    # configured in Home Assistant. A plain datetime.now() would follow the
    # timezone of the host instead, which differs from it on a container
    # install and would shift the day boundary by the offset between them
    now = dt_util.now()
    # If it's before 01:06, we need to fetch yesterday's data
    # Should only need to be before 00:36 but gas data can be 30 minutes behind electricity data
    if now.time() <= time(1, 5):
        _LOGGER.debug("Fetching yesterday's data")
        now = now - timedelta(days=1)
    # Start of the day the readings cover. The query starts from the same
    # instant that the cost sensor reports as its last_reset, so the total
    # and the boundary it is measured from can never disagree
    day_start = dt_util.start_of_local_day(now)
    t_from = day_start
    # Round to the minute
    t_to = now.replace(second=0, microsecond=0)

    # Tell Hildebrand to pull latest DCC data. The DCC pull covers the whole
    # meter, so the cost resources don't need to trigger it as well
    if not resource.classifier.endswith(".cost"):
        await api_call(
            client.catchup(resource.id), f"catchup for resource {resource.id}"
        )

    _LOGGER.debug(
        "Get readings from %s to %s for %s", t_from, t_to, resource.classifier
    )
    readings = await api_call(
        client.get_readings(resource.id, t_from, t_to, "P1D", "sum"),
        f"readings for resource {resource.id}",
    )
    if not readings:
        _LOGGER.debug("No readings returned for resource id %s", resource.id)
        return None, None

    _LOGGER.debug("Readings for %s has %s entries", resource.classifier, len(readings))
    total = 0.0
    # The API can split the requested day across two buckets around a DST change
    for reading in readings[:2]:
        if reading[1] is not None:
            total += reading[1]
    return total, day_start


async def tariff_data(client: GlowApiClient, resource) -> TariffRates | None:
    """Get tariff data from the API.

    Handled separately from api_call so that a transient failure isn't
    reported as the account having no tariff data.
    """
    try:
        tariff = await client.get_tariff(resource.id)
        _LOGGER.debug("Successful tariff fetch for resource %s", resource.id)
    except GlowAuthError as ex:
        raise ConfigEntryAuthFailed(f"Authentication failed: {ex}") from ex
    except GlowTimeoutError as ex:
        _LOGGER.error("Timeout fetching tariff for resource %s: %s", resource.id, ex)
        return None
    except GlowConnectionError as ex:
        _LOGGER.error(
            "Cannot connect fetching tariff for resource %s: %s", resource.id, ex
        )
        return None
    except GlowApiError as ex:
        _LOGGER.warning(
            "Error fetching tariff for resource %s: %s. "
            "The Glow API may be experiencing issues",
            resource.id,
            ex,
        )
        return None
    if tariff is None:
        _LOGGER.warning(
            "No tariff data returned by the Glow API for the %s meter (id: %s). "
            "If you don't see tariff data for this meter in the Bright app, please "
            "disable the associated rate and standing charge sensors",
            supply_type(resource),
            resource.id,
        )
    return tariff


async def hardware_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: GlowApiClient,
    resource,
    virtual_entity,
) -> list[SensorEntity]:
    """Create the sensors that rely on Glow hardware (IHD/CAD).

    Each endpoint is probed once; accounts without hardware simply don't get
    these sensors and no errors are logged.
    """
    sensors: list[SensorEntity] = []

    # Live power draw, electricity only. Without a CAD the endpoint echoes
    # the latest stored kWh reading instead, so require the unit to be watts
    if resource.classifier == "electricity.consumption":
        current = await probe_call(
            client.get_current(resource.id),
            f"current reading probe for resource {resource.id}",
        )
        power_unit = (
            POWER_UNITS.get(normalise_unit(current.units))
            if current is not None
            else None
        )
        if power_unit is not None:
            coordinator = PowerCoordinator(hass, entry, client, resource)
            # Seed the coordinator so the sensor has a value straight away
            coordinator.async_set_updated_data(current)
            sensors.append(
                Power(coordinator, resource, virtual_entity, power_unit)
            )
        else:
            # An energy unit here means the account has no CAD and the API is
            # echoing the latest stored reading, which is not a power figure
            _LOGGER.debug(
                "No live power data for resource %s (units: %s); "
                "skipping the power sensor",
                resource.id,
                getattr(current, "units", None),
            )

    # Cumulative meter register, only reported when the account has an IHD/CAD
    meter_read = await probe_call(
        client.get_meter_read(resource.id),
        f"meter read probe for resource {resource.id}",
    )
    if meter_read is not None:
        sensors.append(
            MeterReading(client, resource, virtual_entity, meter_read)
        )
    else:
        _LOGGER.debug(
            "No meter register data for resource %s; skipping the meter "
            "reading sensor",
            resource.id,
        )

    # Time of the newest available reading; works for DCC-only accounts too
    last_time = await probe_call(
        client.get_last_time(resource.id),
        f"last-time probe for resource {resource.id}",
    )
    if last_time is not None:
        # How far back the platform holds history. This never moves, so it is
        # fetched once here and carried as an attribute
        first_time = await probe_call(
            client.get_first_time(resource.id),
            f"first-time probe for resource {resource.id}",
        )
        sensors.append(
            LastReading(client, resource, virtual_entity, last_time, first_time)
        )

    return sensors


class Usage(SensorEntity):
    """Sensor object for daily usage."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_name = "Usage (today)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, client: GlowApiClient, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id

        self.client = client
        self.initialised = False
        self.resource = resource
        # The meter's own identifying number, filled in during setup when the
        # API can report it. The device registry merges it into the device
        # shared by all of this meter's sensors
        self.serial_number: str | None = None
        self.virtual_entity = virtual_entity

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
            serial_number=self.serial_number,
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
        value, _ = await daily_data(self.client, self.resource)
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

    def __init__(self, client: GlowApiClient, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id

        self.client = client
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
        value, day_start = await daily_data(self.client, self.resource)
        if value is not None:
            self._attr_native_value = round(value / 100, 2)
            self._attr_last_reset = day_start
            self.initialised = True


class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the tariff sensors."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: GlowApiClient, resource
    ) -> None:
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

        self.client = client
        self.resource = resource

    async def _async_update_data(self):
        """Fetch data from tariff API endpoint."""
        # Always fetch until we have data, then only when updated data might be available
        if self.data is not None and not should_update():
            # Return the previous value so the sensors keep their state
            return self.data

        tariff = await tariff_data(self.client, self.resource)
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
    def _extract(rates: TariffRates) -> Any:
        """Return the raw pence value from the tariff's current rates."""
        raise NotImplementedError

    def _update_value(self) -> None:
        """Read the latest value from the coordinator without writing state."""
        rates = self.coordinator.data
        if rates is None:
            return
        value = self._extract(rates)
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
    def _extract(rates: TariffRates) -> Any:
        """Return the standing charge in pence."""
        return rates.standing_charge


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
    def _extract(rates: TariffRates) -> Any:
        """Return the unit rate in pence."""
        return rates.rate


class PowerCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the live power sensor."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: GlowApiClient, resource
    ) -> None:
        """Initialize the power coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="power",
            update_interval=POWER_SCAN_INTERVAL,
        )

        self.client = client
        self.resource = resource

    async def _async_update_data(self):
        """Fetch the latest instantaneous reading."""
        reading = await api_call(
            self.client.get_current(self.resource.id),
            f"current reading for resource {self.resource.id}",
        )
        if reading is None:
            if self.data is not None:
                # A transient failure shouldn't wipe out a known good value;
                # staleness is handled by the sensor's availability check
                return self.data
            raise UpdateFailed(
                f"No current reading available for resource {self.resource.id}"
            )
        return reading


class Power(CoordinatorEntity, SensorEntity):
    """Sensor for the live power draw, sourced from Glow hardware (CAD)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_has_entity_name = True
    _attr_name = "Power (now)"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Hardware reporting in kW would otherwise be shown a thousand times low
    _attr_suggested_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self, coordinator, resource, virtual_entity, unit: str = UnitOfPower.WATT
    ) -> None:
        """Initialize the power sensor with the unit the API reported."""
        super().__init__(coordinator)

        self._attr_unique_id = resource.id + "-power"
        self._attr_native_unit_of_measurement = unit
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
    def available(self) -> bool:
        """Consider the sensor unavailable once the reading goes stale."""
        reading = self.coordinator.data
        return (
            super().available
            and reading is not None
            and dt_util.utcnow() - reading.timestamp < POWER_STALE_AFTER
        )

    @property
    def native_value(self) -> float | None:
        """Return the latest power reading, in the unit registered at setup."""
        reading = self.coordinator.data
        if reading is None:
            return None
        if normalise_unit(reading.units) != normalise_unit(
            self._attr_native_unit_of_measurement
        ):
            # Showing a value on a different scale would be worse than
            # showing none at all
            _LOGGER.debug(
                "Ignoring a power reading for resource %s reported in %s, "
                "expected %s",
                self.resource.id,
                reading.units,
                self._attr_native_unit_of_measurement,
            )
            return None
        return reading.value


class MeterReading(SensorEntity):
    """Sensor for the cumulative register reading of the meter itself."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:counter"
    _attr_name = "Meter reading"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, client: GlowApiClient, resource, virtual_entity, initial) -> None:
        """Initialize the sensor from the reading probed during setup."""
        self._attr_unique_id = resource.id + "-meterread"

        self.client = client
        self.resource = resource
        self.virtual_entity = virtual_entity
        # Kept so that a later change of unit can be detected rather than
        # silently rescaling the register by a factor of a thousand
        self.units = initial.units

        device_class, unit = reading_unit(initial.units)
        if device_class is None:
            _LOGGER.warning(
                "The Glow API reported the meter register for the %s meter "
                "(id: %s) in an unrecognised unit (%s). The reading is shown "
                "unconverted; please open an issue quoting that unit",
                supply_type(resource),
                resource.id,
                initial.units,
            )
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if device_class == SensorDeviceClass.ENERGY:
            # Registers are reported in Wh by some meters and kWh by others,
            # so display them consistently whichever way they arrive
            self._attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_native_value = initial.value

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    async def async_update(self) -> None:
        """Fetch the latest register reading."""
        reading = await api_call(
            self.client.get_meter_read(self.resource.id),
            f"meter read for resource {self.resource.id}",
        )
        if reading is None:
            return
        if normalise_unit(reading.units) != normalise_unit(self.units):
            # Storing a value on a different scale would corrupt the
            # long term statistics, so wait for the restart that recreates
            # the sensor against the new unit
            _LOGGER.warning(
                "The Glow API changed the meter register unit for resource "
                "%s from %s to %s. Ignoring the new reading; restart Home "
                "Assistant to rebuild the sensor with the new unit",
                self.resource.id,
                self.units,
                reading.units,
            )
            return
        self._attr_native_value = reading.value


class TariffHistory(SensorEntity):
    """Diagnostic sensor showing the tariff currently in effect.

    The state is the tariff's name and the full history, sorted oldest
    first, is exposed through the attributes with rates converted to GBP.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_icon = "mdi:script-text-outline"
    _attr_name = "Tariff"

    def __init__(
        self, client: GlowApiClient, resource, meter, virtual_entity, history
    ) -> None:
        """Initialize the sensor from the history probed during setup."""
        self._attr_unique_id = resource.id + "-tariff-history"

        self.client = client
        self.initialised = False
        self.meter = meter
        self.resource = resource
        self.virtual_entity = virtual_entity
        self._apply(history)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            # Attach to the same device as the meter's other sensors
            identifiers={(DOMAIN, self.meter.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    @staticmethod
    def _pounds(pence) -> float | None:
        """Convert a pence value from the API to GBP."""
        try:
            return round(float(pence) / 100, 4)
        except (TypeError, ValueError):
            return None

    def _apply(self, history) -> None:
        """Set the state and attributes from a sorted tariff history."""
        now = dt_util.utcnow()
        current = None
        for entry in history:
            if entry.effective_from is None or entry.effective_from <= now:
                current = entry
        # If every effective date is in the future, fall back to the newest
        if current is None:
            current = history[-1]

        self._attr_native_value = current.name
        self._attr_extra_state_attributes = {
            "effective_from": current.effective_from,
            "rate": self._pounds(current.rate),
            "standing_charge": self._pounds(current.standing_charge),
            "history": [
                {
                    "name": entry.name,
                    "effective_from": entry.effective_from,
                    "rate": self._pounds(entry.rate),
                    "standing_charge": self._pounds(entry.standing_charge),
                }
                for entry in history
            ],
        }

    async def async_update(self) -> None:
        """Fetch the latest tariff history."""
        # Tariff changes are rare; stick to the shared half-hourly cadence
        if self.initialised and not should_update():
            return
        history = await api_call(
            self.client.get_tariff_list(self.resource.id),
            f"tariff list for resource {self.resource.id}",
        )
        if history:
            self._apply(history)
            self.initialised = True


class MeterPointSensor(SensorEntity):
    """Diagnostic sensor for a meter point (MPAN/MPRN).

    The state is the meter point number. Verification status, consent
    expiry and the DCC inventory of the devices behind the meter point are
    exposed through the attributes.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, client: GlowApiClient, point, inventory: list, meter) -> None:
        """Initialize the sensor from the data probed during setup."""
        self._attr_unique_id = f"meterpoint-{point.mpxn}"
        self._attr_name = f"Meter point ({point.kind.upper()})"
        self._attr_icon = (
            "mdi:meter-electric-outline"
            if point.kind == "mpan"
            else "mdi:meter-gas-outline"
        )
        self._attr_native_value = point.mpxn

        self.client = client
        self.initialised = False
        self.inventory = inventory
        self.meter = meter
        self.point = point
        self._build_attributes()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        if self.meter is not None:
            # Attach to the same device as the meter's other sensors
            return DeviceInfo(
                identifiers={(DOMAIN, self.meter.resource.id)},
                manufacturer="Hildebrand",
                model="Glow (DCC)",
                name=device_name(self.meter.resource, self.meter.virtual_entity),
            )
        return DeviceInfo(
            identifiers={(DOMAIN, f"meterpoint-{self.point.mpxn}")},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=f"Meter point {self.point.mpxn}",
        )

    def _build_attributes(self) -> None:
        """Assemble the attributes from the meter point and its inventory."""
        self._attr_extra_state_attributes = {
            "type": self.point.kind.upper(),
            "verified": self.point.is_verified,
            "consent_valid_until": self.point.valid_until,
            "inventory": [
                {
                    "device_type": entry.get("DeviceType"),
                    "manufacturer": entry.get("DeviceManufacturer"),
                    "model": entry.get("DeviceModel"),
                    "firmware_version": entry.get("DeviceFirmwareVersion"),
                    "status": entry.get("DeviceStatus"),
                    "eui": entry.get("EUI"),
                    "smets_version": entry.get("SmetsVersion"),
                    "date_commissioned": entry.get("DateCommissioned"),
                }
                for entry in self.inventory
            ],
        }

    async def async_update(self) -> None:
        """Refresh the verification and consent state of the meter point."""
        # Consent changes are rare; stick to the shared half-hourly cadence.
        # The inventory is fixed hardware data, fetched once at setup
        if self.initialised and not should_update():
            return
        meter_points = await api_call(
            self.client.get_meter_points(), "meter point verification"
        )
        if meter_points is None:
            return
        for point in meter_points:
            if point.mpxn == self.point.mpxn:
                self.point = point
                self._build_attributes()
                self.initialised = True
                return


class LastReading(SensorEntity):
    """Diagnostic sensor for the time of the newest available reading."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Last reading"

    def __init__(
        self,
        client: GlowApiClient,
        resource,
        virtual_entity,
        initial: datetime,
        first_reading: datetime | None = None,
    ) -> None:
        """Initialize the sensor from the timestamps probed during setup."""
        self._attr_unique_id = resource.id + "-last-time"
        self._attr_native_value = initial
        self._attr_extra_state_attributes = {
            "data_available_from": first_reading,
            "resource_id": resource.id,
            "classifier": resource.classifier,
            "description": resource.description,
        }

        self.client = client
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

    async def async_update(self) -> None:
        """Fetch the time of the most recent reading."""
        last_time = await api_call(
            self.client.get_last_time(self.resource.id),
            f"last-time for resource {self.resource.id}",
        )
        if last_time is not None:
            self._attr_native_value = last_time
