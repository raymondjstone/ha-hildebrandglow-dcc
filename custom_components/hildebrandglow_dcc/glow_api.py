"""Asynchronous client for the Hildebrand Glowmarkt API.

Implements the small subset of the Glowmarkt platform API that this
integration uses, replacing the pyglowmarkt dependency. The API is
documented at
https://docs.glowmarkt.com/GlowmarktAPIDataRetrievalDocumentationIndividualUserForBright.pdf
and https://api.glowmarkt.com/api-docs/v0-1/resourcesys/.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Application ID published in the Glowmarkt API documentation for Bright users
APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
BASE_URL = "https://api.glowmarkt.com/api/v0-1/"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Tokens expire after 7 days; re-authenticate well before that rather than
# waiting for requests to start failing with a 401
TOKEN_EXPIRY_MARGIN = timedelta(hours=12)
TOKEN_LIFETIME_FALLBACK = timedelta(days=7)


class GlowError(Exception):
    """Base error raised by the Glow API client."""


class GlowAuthError(GlowError):
    """The API rejected the credentials."""


class GlowConnectionError(GlowError):
    """The API could not be reached."""


class GlowTimeoutError(GlowConnectionError):
    """The API did not respond in time."""


class GlowApiError(GlowError):
    """The API returned an unexpected response.

    The HTTP status is kept so that callers can tell an endpoint that this
    account does not support from one that merely failed this time.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the message along with the status that produced it."""
        super().__init__(message)
        self.status = status


@dataclass
class VirtualEntity:
    """A Glowmarkt virtual entity, e.g. a home with smart meters."""

    id: str
    name: str | None


@dataclass
class Resource:
    """A Glowmarkt resource: a single data stream belonging to a meter."""

    id: str
    classifier: str
    name: str
    base_unit: str
    description: str | None = None
    active: bool | None = None


@dataclass
class TariffRates:
    """Current tariff rates in pence, as reported by the API."""

    rate: float | None
    standing_charge: float | None


@dataclass
class TariffHistoryEntry:
    """One tariff from the resource's tariff history, rates in pence."""

    name: str | None
    effective_from: datetime | None
    rate: float | None
    standing_charge: float | None


@dataclass
class CurrentReading:
    """A single instantaneous reading with the unit reported by the API."""

    timestamp: datetime
    value: float
    units: str | None


@dataclass
class GlowDevice:
    """A physical device (meter, CAD/IHD gateway) known to the platform."""

    id: str
    hardware_id: str | None
    description: str | None
    hardware_ids: dict | None = None


@dataclass
class MeterPoint:
    """A meter point (MPAN/MPRN) with its verification and consent state."""

    mpxn: str
    kind: str
    is_verified: bool | None
    valid_until: datetime | None


def _utc_string(when: datetime) -> str:
    """Convert a datetime to the naive UTC ISO string the API expects.

    Naive datetimes are assumed to be in the local timezone, matching the
    values the sensors build from datetime.now().
    """
    return when.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_single_reading(payload: Any) -> CurrentReading | None:
    """Parse a response holding a single [timestamp, value] data entry.

    Used by the current and meterread endpoints, which both return
    {"units": ..., "data": [[ts, value]]}.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or []
    entry = data[0] if data else None
    if not isinstance(entry, (list, tuple)) or len(entry) < 2 or entry[1] is None:
        return None
    return CurrentReading(
        timestamp=datetime.fromtimestamp(entry[0], tz=timezone.utc).astimezone(),
        value=entry[1],
        units=payload.get("units"),
    )


def _parse_device(elt: Any) -> GlowDevice | None:
    """Build a GlowDevice from an API device document."""
    if not isinstance(elt, dict) or "deviceId" not in elt:
        return None
    hardware_ids = elt.get("hardwareIds")
    return GlowDevice(
        id=elt["deviceId"],
        hardware_id=elt.get("hardwareId"),
        description=elt.get("description"),
        hardware_ids=hardware_ids if isinstance(hardware_ids, dict) else None,
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO date string from the API into an aware local datetime."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone()


def _as_float(value: Any) -> float | None:
    """Coerce an API value to float; tiered tariffs report rates as strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_tariff(entry: dict) -> TariffRates | None:
    """Extract the current rates from a tariff document.

    The live API returns a currentRates convenience object, while the
    documented schema shows a plan/planDetail structure; accept either.
    """
    current = entry.get("currentRates")
    if isinstance(current, dict):
        rate = _as_float(current.get("rate"))
        standing = _as_float(current.get("standingCharge"))
        if rate is not None or standing is not None:
            return TariffRates(rate=rate, standing_charge=standing)

    rate = None
    standing = None
    for plan in entry.get("plan") or []:
        for detail in plan.get("planDetail") or []:
            if not isinstance(detail, dict):
                continue
            if rate is None and "rate" in detail:
                rate = _as_float(detail["rate"])
            if standing is None and "standing" in detail:
                standing = _as_float(detail["standing"])
    if rate is None and standing is None:
        return None
    return TariffRates(rate=rate, standing_charge=standing)


class GlowApiClient:
    """Minimal async client for the Glowmarkt API."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        """Initialize the client with a shared aiohttp session."""
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_expiry = datetime.min.replace(tzinfo=timezone.utc)
        self._auth_lock = asyncio.Lock()

    async def authenticate(self) -> None:
        """Authenticate with the API, raising GlowAuthError on bad credentials."""
        async with self._auth_lock:
            await self._authenticate()

    async def _authenticate(self) -> None:
        """Fetch a new JWT token. Caller must hold the auth lock."""
        try:
            resp = await self._session.post(
                f"{BASE_URL}auth",
                headers={
                    "Content-Type": "application/json",
                    "applicationId": APPLICATION_ID,
                },
                json={"username": self._username, "password": self._password},
                timeout=REQUEST_TIMEOUT,
            )
        except TimeoutError as ex:
            raise GlowTimeoutError("Timeout during authentication") from ex
        except aiohttp.ClientError as ex:
            raise GlowConnectionError(f"Cannot connect: {ex}") from ex

        if resp.status in (401, 403):
            raise GlowAuthError("Authentication failed")
        if resp.status != 200:
            raise GlowApiError(f"Authentication returned status {resp.status}")

        data = await resp.json()
        if not data.get("valid") or "token" not in data:
            raise GlowAuthError("Expected an authentication token")

        self._token = data["token"]
        exp = data.get("exp")
        if exp:
            self._token_expiry = datetime.fromtimestamp(exp, tz=timezone.utc)
        else:
            self._token_expiry = (
                datetime.now(timezone.utc) + TOKEN_LIFETIME_FALLBACK
            )
        _LOGGER.debug(
            "Successful POST to %sauth, token valid until %s",
            BASE_URL,
            self._token_expiry,
        )

    async def _refresh_token(self) -> bool:
        """Try to exchange the current token for a fresh one.

        Uses GET auth/newToken so the password doesn't need to be re-sent
        for routine renewals. Returns False if the token is missing, already
        expired, or the exchange fails for any reason; the caller then falls
        back to a full password authentication. Caller must hold the auth
        lock.
        """
        if self._token is None or datetime.now(timezone.utc) >= self._token_expiry:
            return False
        try:
            resp = await self._session.get(
                f"{BASE_URL}auth/newToken",
                headers={
                    "Content-Type": "application/json",
                    "applicationId": APPLICATION_ID,
                    "token": self._token,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status != 200:
                _LOGGER.debug("Token refresh returned status %s", resp.status)
                return False
            data = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as ex:
            _LOGGER.debug("Token refresh failed: %s", ex)
            return False

        if not data.get("valid") or "token" not in data:
            return False

        self._token = data["token"]
        exp = data.get("exp")
        if exp:
            self._token_expiry = datetime.fromtimestamp(exp, tz=timezone.utc)
        else:
            self._token_expiry = (
                datetime.now(timezone.utc) + TOKEN_LIFETIME_FALLBACK
            )
        _LOGGER.debug(
            "Refreshed the API token, now valid until %s", self._token_expiry
        )
        return True

    async def _ensure_token(self) -> None:
        """Authenticate if there is no token or it is close to expiry.

        A token nearing expiry is renewed via a token exchange where
        possible, with full password authentication as the fallback.
        """
        async with self._auth_lock:
            if (
                self._token is None
                or datetime.now(timezone.utc)
                >= self._token_expiry - TOKEN_EXPIRY_MARGIN
            ):
                if not await self._refresh_token():
                    await self._authenticate()

    async def _send(
        self, method: str, path: str, params: dict | None = None
    ) -> aiohttp.ClientResponse:
        """Perform a request with the current auth headers."""
        return await self._session.request(
            method,
            f"{BASE_URL}{path}",
            headers={
                "Content-Type": "application/json",
                "applicationId": APPLICATION_ID,
                "token": self._token,
            },
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    async def _request(
        self, path: str, params: dict | None = None, method: str = "GET"
    ) -> Any:
        """Perform an authenticated request, re-authenticating on a 401."""
        await self._ensure_token()
        try:
            resp = await self._send(method, path, params)
            if resp.status == 401:
                # The token was revoked before its expiry time; retry once
                _LOGGER.debug("Token rejected by the API, re-authenticating")
                async with self._auth_lock:
                    await self._authenticate()
                resp = await self._send(method, path, params)
            if resp.status != 200:
                raise GlowApiError(
                    f"{method} {path} returned status {resp.status}",
                    status=resp.status,
                )
            return await resp.json()
        except TimeoutError as ex:
            raise GlowTimeoutError(f"Timeout during {method} {path}") from ex
        except aiohttp.ClientError as ex:
            raise GlowConnectionError(f"Cannot connect: {ex}") from ex

    async def get_virtual_entities(self) -> list[VirtualEntity]:
        """Return all virtual entities on the account."""
        payload = await self._request("virtualentity")
        return [
            VirtualEntity(id=elt["veId"], name=elt.get("name"))
            for elt in payload
        ]

    async def get_resources(self, ve_id: str) -> list[Resource]:
        """Return the full resource definitions for a virtual entity."""
        payload = await self._request(f"virtualentity/{ve_id}/resources")
        return [
            Resource(
                id=elt["resourceId"],
                classifier=elt.get("classifier", ""),
                name=elt.get("name", ""),
                base_unit=elt.get("baseUnit", ""),
                description=elt.get("description"),
                active=elt.get("active"),
            )
            for elt in payload.get("resources", [])
        ]

    async def get_readings(
        self,
        resource_id: str,
        t_from: datetime,
        t_to: datetime,
        period: str,
        func: str = "sum",
    ) -> list[tuple[datetime, float]]:
        """Return readings for a resource as (local datetime, value) pairs.

        Requests nulls for missing data so a genuine zero reading can be
        distinguished from no data; null entries are filtered out.
        """
        params = {
            "from": _utc_string(t_from),
            "to": _utc_string(t_to),
            "period": period,
            # Data is requested in UTC and converted locally, so no offset
            "offset": "0",
            "function": func,
            "nulls": "1",
        }
        payload = await self._request(f"resource/{resource_id}/readings", params)
        return [
            (
                datetime.fromtimestamp(entry[0], tz=timezone.utc).astimezone(),
                entry[1],
            )
            for entry in payload.get("data", [])
            if entry[1] is not None
        ]

    async def catchup(self, resource_id: str) -> Any:
        """Ask the platform to pull the latest readings from the DCC."""
        return await self._request(f"resource/{resource_id}/catchup")

    async def get_tariff(self, resource_id: str) -> TariffRates | None:
        """Return the current tariff rates, or None if the API has none."""
        payload = await self._request(f"resource/{resource_id}/tariff")
        data = payload.get("data") or []
        if not data:
            return None
        # Use the last entry, which is the tariff currently in effect
        return _parse_tariff(data[-1])

    async def get_tariff_list(self, resource_id: str) -> list[TariffHistoryEntry]:
        """Return the tariff history of a cost resource, oldest first.

        The API documents the response as unsorted, so entries are sorted by
        effective date here. Dynamic and time-of-use tariffs may have no
        single flat rate, in which case the rate is None.
        """
        payload = await self._request(f"resource/{resource_id}/tariff-list")
        entries = []
        data = payload.get("data") or [] if isinstance(payload, dict) else []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            rates = _parse_tariff(entry)
            effective = entry.get("effectiveDate") or entry.get("from")
            when = None
            if isinstance(effective, str):
                try:
                    # Dates come back naive, e.g. "2018-12-12 00:00:00"; the
                    # platform stores everything in UTC
                    when = datetime.fromisoformat(effective).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    _LOGGER.debug("Could not parse tariff date %s", effective)
            entries.append(
                TariffHistoryEntry(
                    name=entry.get("displayName") or entry.get("name"),
                    effective_from=when,
                    rate=rates.rate if rates else None,
                    standing_charge=rates.standing_charge if rates else None,
                )
            )
        entries.sort(
            key=lambda e: e.effective_from
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return entries

    async def get_current(self, resource_id: str) -> CurrentReading | None:
        """Return the latest instantaneous reading of a resource.

        With Glow hardware (IHD/CAD) the electricity value is the live power
        draw in W, updated every few seconds. Without hardware the API echoes
        the latest stored reading instead, with units of kWh.
        """
        payload = await self._request(f"resource/{resource_id}/current")
        return _parse_single_reading(payload)

    async def get_meter_read(self, resource_id: str) -> CurrentReading | None:
        """Return the cumulative register reading of the meter.

        Only available for accounts with a Glow IHD/CAD. The unit varies by
        meter, so callers must honour the units field rather than assuming
        the kWh shown in the API documentation.
        """
        payload = await self._request(f"resource/{resource_id}/meterread")
        return _parse_single_reading(payload)

    async def _get_timestamp(self, path: str, key: str) -> datetime | None:
        """Return a local datetime from an endpoint reporting one timestamp."""
        payload = await self._request(path)
        data = payload.get("data") if isinstance(payload, dict) else None
        ts = data.get(key) if isinstance(data, dict) else None
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()

    async def get_last_time(self, resource_id: str) -> datetime | None:
        """Return the local time of the most recent available reading."""
        return await self._get_timestamp(
            f"resource/{resource_id}/last-time", "lastTs"
        )

    async def get_first_time(self, resource_id: str) -> datetime | None:
        """Return the local time of the oldest available reading.

        Indicates how far back the platform holds history for the meter.
        """
        return await self._get_timestamp(
            f"resource/{resource_id}/first-time", "firstTs"
        )

    async def clear_cache(self, resource_id: str) -> Any:
        """Drop the platform's cached data for a resource.

        Useful when the platform is serving stale readings; the next request
        is then answered from the underlying data.
        """
        return await self._request(
            f"resource/{resource_id}/cache", method="DELETE"
        )

    async def get_devices(self) -> list[GlowDevice]:
        """Return the physical devices registered to the account."""
        payload = await self._request("device")
        return [
            device
            for elt in payload or []
            if (device := _parse_device(elt)) is not None
        ]

    async def get_device_for_resource(self, resource_id: str) -> GlowDevice | None:
        """Return the physical device that sources a resource.

        For DCC data this is the smart meter itself, whose hardware ID holds
        the meter's identifying number.
        """
        payload = await self._request(f"device/resource/{resource_id}")
        return _parse_device(payload)

    async def get_meter_points(self) -> list[MeterPoint]:
        """Return the account's meter points (MPAN/MPRN) with their status."""
        payload = await self._request("user/verification/status")
        points = payload.get("meterPointVerification") if isinstance(payload, dict) else None
        result = []
        for elt in points or []:
            if not isinstance(elt, dict) or elt.get("mpxn") is None:
                continue
            result.append(
                MeterPoint(
                    mpxn=str(elt["mpxn"]),
                    kind=elt.get("mpxnKey", "mpxn"),
                    is_verified=elt.get("isVerified"),
                    valid_until=_parse_iso_datetime(elt.get("isValidUntil")),
                )
            )
        return result

    async def get_meter_point_inventory(self, mpxn: str) -> list[dict]:
        """Return the DCC inventory of a meter point.

        Each entry describes one device behind the meter point (ESME, GSME,
        comms hub, ...) with its manufacturer, model, firmware and EUI.
        """
        payload = await self._request(f"device/meter-point/{mpxn}/inventory")
        if not isinstance(payload, dict) or not payload.get("valid", True):
            return []
        return [
            entry for entry in payload.get("inventory") or [] if isinstance(entry, dict)
        ]

    async def get_meter_point_resources(self, mpxn: str) -> list[str]:
        """Return the IDs of the resources associated with a meter point."""
        payload = await self._request(f"device/meter-point/{mpxn}/resources")
        resources = payload.get("resources") if isinstance(payload, dict) else None
        return [
            elt["resourceId"]
            for elt in resources or []
            if isinstance(elt, dict) and "resourceId" in elt
        ]

    async def get_device_last_seen(self, device_id: str) -> datetime | None:
        """Return when a gateway device last sent a packet to the platform.

        Returns None for devices that don't report packet timestamps, such
        as the meters themselves on DCC-only accounts.
        """
        payload = await self._request(f"device/{device_id}/status")
        if not isinstance(payload, dict) or not payload.get("found"):
            return None
        ts = payload.get("value")
        if not ts:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
