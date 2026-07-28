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
    """The API returned an unexpected response."""


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


@dataclass
class TariffRates:
    """Current tariff rates in pence, as reported by the API."""

    rate: float | None
    standing_charge: float | None


def _utc_string(when: datetime) -> str:
    """Convert a datetime to the naive UTC ISO string the API expects.

    Naive datetimes are assumed to be in the local timezone, matching the
    values the sensors build from datetime.now().
    """
    return when.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_tariff(entry: dict) -> TariffRates | None:
    """Extract the current rates from a tariff document.

    The live API returns a currentRates convenience object, while the
    documented schema shows a plan/planDetail structure; accept either.
    """
    current = entry.get("currentRates")
    if isinstance(current, dict):
        rate = current.get("rate")
        standing = current.get("standingCharge")
        if rate is not None or standing is not None:
            return TariffRates(rate=rate, standing_charge=standing)

    rate = None
    standing = None
    for plan in entry.get("plan") or []:
        for detail in plan.get("planDetail") or []:
            if not isinstance(detail, dict):
                continue
            if rate is None and "rate" in detail:
                rate = detail["rate"]
            if standing is None and "standing" in detail:
                standing = detail["standing"]
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

    async def _ensure_token(self) -> None:
        """Authenticate if there is no token or it is close to expiry."""
        async with self._auth_lock:
            if (
                self._token is None
                or datetime.now(timezone.utc)
                >= self._token_expiry - TOKEN_EXPIRY_MARGIN
            ):
                await self._authenticate()

    async def _get(self, path: str, params: dict | None = None) -> aiohttp.ClientResponse:
        """Perform a GET request with the current auth headers."""
        return await self._session.get(
            f"{BASE_URL}{path}",
            headers={
                "Content-Type": "application/json",
                "applicationId": APPLICATION_ID,
                "token": self._token,
            },
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    async def _request(self, path: str, params: dict | None = None) -> Any:
        """Perform an authenticated GET request, re-authenticating on a 401."""
        await self._ensure_token()
        try:
            resp = await self._get(path, params)
            if resp.status == 401:
                # The token was revoked before its expiry time; retry once
                _LOGGER.debug("Token rejected by the API, re-authenticating")
                async with self._auth_lock:
                    await self._authenticate()
                resp = await self._get(path, params)
            if resp.status != 200:
                raise GlowApiError(f"GET {path} returned status {resp.status}")
            return await resp.json()
        except TimeoutError as ex:
            raise GlowTimeoutError(f"Timeout during GET {path}") from ex
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
