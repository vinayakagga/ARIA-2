"""Resilient Weather Service for ARIA Advice Engine (Phase 2).

Integrates with Open-Meteo Forecast API:
- Retrieves location-specific weather using GPS coordinates from ARIA rover telemetry.
- Maintains persistent SQLite memory of the last known valid GPS location.
- If current GPS fix is lost, reuses previously confirmed coordinates up to 24 hours.
- Caches weather forecasts independently for 15 minutes.
- Reuses cached weather within coordinate tolerance (~1 km) to prevent unnecessary requests.
- Never presents expired weather as current.
- Uses Python standard library only (urllib.request, json, threading). No external HTTP frameworks.
- Uses certifi CA bundle for HTTPS certificate verification. Certificate verification is always
  enforced; connections with invalid or expired certificates are rejected.
- Attributions and limitations clearly documented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import logging
import math
import os
import ssl
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from server.database import Database
from server.telemetry import TelemetryMessage

log = logging.getLogger("aria.weather")

OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION_TEXT = "Weather data by Open-Meteo.com"

# Configuration defaults (overridable via environment)
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_CACHE_TTL_S = 900.0  # 15 minutes
DEFAULT_MAX_LOCATION_AGE_S = 86400.0  # 24 hours
DEFAULT_COORD_TOLERANCE_DEG = 0.01  # ~1.1 km at equator


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context with up-to-date CA certificates.

    Prefers the certifi CA bundle (kept current via pip) over the OpenSSL
    built-in bundle, which may contain expired roots in some Python distributions.
    Certificate verification and hostname checking are always enforced.
    """
    try:
        import certifi  # type: ignore[import-not-found]
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # certifi not installed — fall back to the system/OpenSSL default.
        # This may fail on systems with an outdated CA store. Installing
        # certifi (listed in server/requirements.txt) is the recommended fix.
        log.warning(
            "certifi is not installed; using system CA bundle for SSL verification. "
            "If SSL errors occur, run: pip install certifi"
        )
        ctx = ssl.create_default_context()
    return ctx


class LocationSource(str, Enum):
    CURRENT_LOCATION = "CURRENT_LOCATION"
    LAST_KNOWN_LOCATION = "LAST_KNOWN_LOCATION"
    LOCATION_UNAVAILABLE = "LOCATION_UNAVAILABLE"


class CacheStatus(str, Enum):
    FRESH = "fresh"
    CACHED = "cached"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class WeatherCurrent:
    temperature_c: float | None
    humidity_pct: float | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None
    weather_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature_c": self.temperature_c,
            "temperature_2m": self.temperature_c,  # Alias for rules compatibility
            "humidity_pct": self.humidity_pct,
            "relative_humidity_2m": self.humidity_pct,  # Alias for rules compatibility
            "precipitation_mm": self.precipitation_mm,
            "precipitation": self.precipitation_mm,  # Alias for rules compatibility
            "wind_speed_kmh": self.wind_speed_kmh,
            "wind_speed_10m": self.wind_speed_kmh,  # Alias for rules compatibility
            "weather_code": self.weather_code,
        }


@dataclass(frozen=True)
class WeatherForecastSummary:
    precipitation_probability_next_6h_pct: int | None
    precipitation_next_6h_mm: float | None
    maximum_temperature_c: float | None
    maximum_wind_speed_kmh: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "precipitation_probability_next_6h_pct": self.precipitation_probability_next_6h_pct,
            "precipitation_next_6h_mm": self.precipitation_next_6h_mm,
            "maximum_temperature_c": self.maximum_temperature_c,
            "maximum_wind_speed_kmh": self.maximum_wind_speed_kmh,
        }


@dataclass
class WeatherCacheEntry:
    payload: dict[str, Any]
    latitude: float
    longitude: float
    retrieved_at: datetime
    expires_at: datetime
    location_source: LocationSource
    weather_source: str = "open-meteo"

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def matches_location(self, lat: float, lon: float, tolerance_deg: float) -> bool:
        return (abs(self.latitude - lat) <= tolerance_deg and
                abs(self.longitude - lon) <= tolerance_deg)


@dataclass
class WeatherResponse:
    status: str  # "available" | "unavailable"
    location_source: str
    location_timestamp: str | None
    location_age_seconds: float | None
    coordinates: dict[str, float] | None
    weather_source: str | None
    weather_retrieved_at: str | None
    weather_expires_at: str | None
    weather_cache_status: str
    current: dict[str, Any] | None
    forecast: dict[str, Any] | None
    attribution: str = ATTRIBUTION_TEXT
    limitations: list[str] = field(default_factory=list)
    hourly_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        res = {
            "status": self.status,
            "location_source": self.location_source,
            "location_timestamp": self.location_timestamp,
            "location_age_seconds": self.location_age_seconds,
            "coordinates": self.coordinates,
            "weather_source": self.weather_source,
            "weather_retrieved_at": self.weather_retrieved_at,
            "weather_expires_at": self.weather_expires_at,
            "weather_cache_status": self.weather_cache_status,
            "current": self.current,
            "forecast": self.forecast,
            "attribution": self.attribution,
            "limitations": self.limitations,
        }
        if self.hourly_summary:
            res["hourly"] = self.hourly_summary
        return res


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def is_valid_gps_coordinates(lat: Any, lon: Any) -> bool:
    """Validate latitude and longitude ranges."""
    if lat is None or lon is None:
        return False
    if isinstance(lat, bool) or isinstance(lon, bool):
        return False
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    if math.isnan(lat) or math.isnan(lon):
        return False
    return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0


class WeatherService:
    """Resilient Open-Meteo Weather Service with persistent GPS memory and forecast caching."""

    def __init__(
        self,
        db: Database | None = None,
        timeout_s: float | None = None,
        cache_ttl_s: float | None = None,
        max_location_age_s: float | None = None,
        coord_tolerance_deg: float | None = None,
    ) -> None:
        self.db = db if db is not None else Database()
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else float(os.environ.get("WEATHER_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_S)))
        )
        self.cache_ttl_s = (
            cache_ttl_s
            if cache_ttl_s is not None
            else float(os.environ.get("WEATHER_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_S)))
        )
        self.max_location_age_s = (
            max_location_age_s
            if max_location_age_s is not None
            else float(os.environ.get("GPS_MAX_LOCATION_AGE_SECONDS", str(DEFAULT_MAX_LOCATION_AGE_S)))
        )
        self.coord_tolerance_deg = (
            coord_tolerance_deg
            if coord_tolerance_deg is not None
            else float(os.environ.get("WEATHER_COORDINATE_TOLERANCE_DEG", str(DEFAULT_COORD_TOLERANCE_DEG)))
        )

        self._lock = threading.Lock()
        self._cache: WeatherCacheEntry | None = None

    def resolve_location(
        self,
        telemetry: TelemetryMessage | None,
        device_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[LocationSource, float | None, float | None, datetime | None, float | None]:
        """Determine effective GPS coordinates.

        Returns:
            (LocationSource, latitude, longitude, fix_timestamp, age_seconds)
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        dev_id = device_id or (telemetry.device_id if telemetry else "aria-c3")

        # 1. Check live telemetry for valid GPS fix
        gps = getattr(telemetry, "gps", None) if telemetry else None
        if gps and gps.fix and is_valid_gps_coordinates(gps.latitude, gps.longitude):
            lat = float(gps.latitude)
            lon = float(gps.longitude)
            fix_at = telemetry.received_at if telemetry and telemetry.received_at else now
            if fix_at.tzinfo is None:
                fix_at = fix_at.replace(tzinfo=timezone.utc)

            # Update persistent SQLite storage immediately
            try:
                self.db.save_last_known_gps(
                    device_id=dev_id,
                    latitude=lat,
                    longitude=lon,
                    last_fix_at=fix_at,
                    updated_at=now,
                )
            except Exception:
                log.exception("failed to persist current GPS location for %s", dev_id)

            age_s = max(0.0, (now - fix_at).total_seconds())
            return LocationSource.CURRENT_LOCATION, lat, lon, fix_at, age_s

        # 2. GPS fix is not available currently. Fall back to persistent SQLite storage.
        try:
            stored = self.db.get_last_known_gps(dev_id)
        except Exception:
            log.exception("failed to query last known GPS from database for %s", dev_id)
            stored = None

        if stored and is_valid_gps_coordinates(stored.get("latitude"), stored.get("longitude")):
            stored_lat = float(stored["latitude"])
            stored_lon = float(stored["longitude"])
            stored_fix_at = stored["last_fix_at"]
            if stored_fix_at.tzinfo is None:
                stored_fix_at = stored_fix_at.replace(tzinfo=timezone.utc)
            age_s = max(0.0, (now - stored_fix_at).total_seconds())

            if age_s <= self.max_location_age_s:
                # Valid last-known location within 24h
                return LocationSource.LAST_KNOWN_LOCATION, stored_lat, stored_lon, stored_fix_at, age_s
            else:
                # Stale last-known location (> 24h)
                return LocationSource.LOCATION_UNAVAILABLE, stored_lat, stored_lon, stored_fix_at, age_s

        # 3. No stored coordinates exist at all
        return LocationSource.LOCATION_UNAVAILABLE, None, None, None, None

    def _fetch_open_meteo(self, lat: float, lon: float) -> dict[str, Any]:
        """Fetch weather data from Open-Meteo REST API using standard urllib."""
        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": "temperature_2m,relative_humidity_2m,precipitation,rain,wind_speed_10m,weather_code",
            "hourly": "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability,wind_speed_10m",
            "forecast_days": "1",
            "timezone": "UTC",
        }
        url = f"{OPEN_METEO_BASE_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ARIA-Agricultural-Rover/1.0",
                "Accept": "application/json",
            },
        )

        ssl_ctx = _build_ssl_context()
        with urllib.request.urlopen(req, timeout=self.timeout_s, context=ssl_ctx) as response:
            status_code = response.getcode()
            if status_code != 200:
                raise urllib.error.HTTPError(url, status_code, f"Open-Meteo returned status {status_code}", response.headers, None)
            raw_body = response.read().decode("utf-8")

        data = json.loads(raw_body)
        if not isinstance(data, dict):
            raise ValueError("Open-Meteo response root must be a JSON object")
        return data

    def _parse_weather_payload(self, raw_data: dict[str, Any]) -> tuple[WeatherCurrent, WeatherForecastSummary, dict[str, Any]]:
        """Validate and parse raw Open-Meteo response."""
        curr = raw_data.get("current")
        if not isinstance(curr, dict):
            curr = {}

        # Safe extraction of current conditions
        temp_c = curr.get("temperature_2m")
        if temp_c is not None:
            try:
                temp_c = float(temp_c)
            except (ValueError, TypeError):
                temp_c = None

        hum_pct = curr.get("relative_humidity_2m")
        if hum_pct is not None:
            try:
                hum_pct = float(hum_pct)
            except (ValueError, TypeError):
                hum_pct = None

        precip_mm = curr.get("precipitation")
        if precip_mm is not None:
            try:
                precip_mm = float(precip_mm)
            except (ValueError, TypeError):
                precip_mm = None

        wind_kmh = curr.get("wind_speed_10m")
        if wind_kmh is not None:
            try:
                wind_kmh = float(wind_kmh)
            except (ValueError, TypeError):
                wind_kmh = None

        weather_code = curr.get("weather_code")
        if weather_code is not None:
            try:
                weather_code = int(weather_code)
            except (ValueError, TypeError):
                weather_code = None

        current_obj = WeatherCurrent(
            temperature_c=temp_c,
            humidity_pct=hum_pct,
            precipitation_mm=precip_mm,
            wind_speed_kmh=wind_kmh,
            weather_code=weather_code,
        )

        # Parse forecast summary from hourly next 6 hours
        hourly = raw_data.get("hourly")
        if not isinstance(hourly, dict):
            hourly = {}

        probs = [p for p in hourly.get("precipitation_probability", []) if isinstance(p, (int, float))]
        next_probs = probs[:6] if probs else []
        max_prob = int(max(next_probs)) if next_probs else None

        precips = [p for p in hourly.get("precipitation", []) if isinstance(p, (int, float))]
        next_precips = precips[:6] if precips else []
        sum_precip = round(float(sum(next_precips)), 2) if next_precips else None

        temps = [t for t in hourly.get("temperature_2m", []) if isinstance(t, (int, float))]
        next_temps = temps[:6] if temps else []
        max_temp = float(max(next_temps)) if next_temps else None

        winds = [w for w in hourly.get("wind_speed_10m", []) if isinstance(w, (int, float))]
        next_winds = winds[:6] if winds else []
        max_wind = float(max(next_winds)) if next_winds else None

        forecast_obj = WeatherForecastSummary(
            precipitation_probability_next_6h_pct=max_prob,
            precipitation_next_6h_mm=sum_precip,
            maximum_temperature_c=max_temp,
            maximum_wind_speed_kmh=max_wind,
        )

        hourly_summary = {
            "precipitation_probability": next_probs,
            "precipitation": next_precips,
            "temperature_2m": next_temps,
            "wind_speed_10m": next_winds,
        }

        return current_obj, forecast_obj, hourly_summary

    def get_weather(
        self,
        telemetry: TelemetryMessage | None,
        device_id: str | None = None,
        force_refresh: bool = False,
        now: datetime | None = None,
    ) -> WeatherResponse:
        """Retrieve localized weather context with GPS memory and 15-minute caching."""
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        loc_source, lat, lon, fix_at, age_s = self.resolve_location(telemetry, device_id=device_id, now=now)

        base_limitations = [
            "Weather represents a regional forecast, not a direct measurement at the rover.",
            "GPS horizontal precision is unverified because HDOP is not included in telemetry.",
        ]

        # 1. Location is completely unavailable or older than 24h
        if loc_source == LocationSource.LOCATION_UNAVAILABLE:
            with self._lock:
                cache = self._cache
                # Check if we have a valid, unexpired weather cache from prior query
                if cache is not None and not cache.is_expired(now):
                    log.info("reusing unexpired weather cache despite location unavailable")
                    curr_obj, fcast_obj, hourly_sum = self._parse_weather_payload(cache.payload)
                    return WeatherResponse(
                        status="available",
                        location_source=LocationSource.LAST_KNOWN_LOCATION.value,
                        location_timestamp=_iso(cache.retrieved_at),
                        location_age_seconds=max(0.0, (now - cache.retrieved_at).total_seconds()),
                        coordinates={"latitude": cache.latitude, "longitude": cache.longitude},
                        weather_source=cache.weather_source,
                        weather_retrieved_at=_iso(cache.retrieved_at),
                        weather_expires_at=_iso(cache.expires_at),
                        weather_cache_status=CacheStatus.CACHED.value,
                        current=curr_obj.to_dict(),
                        forecast=fcast_obj.to_dict(),
                        hourly_summary=hourly_sum,
                        limitations=base_limitations + [
                            "GPS location is unavailable. Reusing previously cached localized weather forecast."
                        ],
                    )

            # No cache or cache is expired
            lims = list(base_limitations)
            if age_s is not None and age_s > self.max_location_age_s:
                lims.append(f"Last confirmed GPS location is stale ({age_s/3600:.1f} hours old, exceeds 24h limit).")
            else:
                lims.append("No valid current or sufficiently recent last-known GPS location is available.")

            return WeatherResponse(
                status="unavailable",
                location_source=LocationSource.LOCATION_UNAVAILABLE.value,
                location_timestamp=_iso(fix_at),
                location_age_seconds=age_s,
                coordinates={"latitude": lat, "longitude": lon} if (lat is not None and lon is not None) else None,
                weather_source=None,
                weather_retrieved_at=None,
                weather_expires_at=None,
                weather_cache_status=CacheStatus.UNAVAILABLE.value,
                current=None,
                forecast=None,
                limitations=lims,
            )

        # 2. Location is available (CURRENT_LOCATION or LAST_KNOWN_LOCATION)
        assert lat is not None and lon is not None

        # Check in-memory weather cache
        with self._lock:
            cache = self._cache
            if cache is not None:
                matches = cache.matches_location(lat, lon, self.coord_tolerance_deg)
                expired = cache.is_expired(now)

                if matches and not expired and not force_refresh:
                    # Valid cache hit
                    curr_obj, fcast_obj, hourly_sum = self._parse_weather_payload(cache.payload)
                    lims = list(base_limitations)
                    if loc_source == LocationSource.LAST_KNOWN_LOCATION:
                        lims.append(f"Using last known GPS location recorded {age_s:.0f}s ago. Current live GPS fix is unavailable.")

                    return WeatherResponse(
                        status="available",
                        location_source=loc_source.value,
                        location_timestamp=_iso(fix_at),
                        location_age_seconds=age_s,
                        coordinates={"latitude": lat, "longitude": lon},
                        weather_source=cache.weather_source,
                        weather_retrieved_at=_iso(cache.retrieved_at),
                        weather_expires_at=_iso(cache.expires_at),
                        weather_cache_status=CacheStatus.CACHED.value,
                        current=curr_obj.to_dict(),
                        forecast=fcast_obj.to_dict(),
                        hourly_summary=hourly_sum,
                        limitations=lims,
                    )

        # 3. Cache miss, expired, rover moved outside tolerance, or force_refresh requested.
        # Fetch fresh data from Open-Meteo.
        try:
            raw_payload = self._fetch_open_meteo(lat, lon)
            expires_at = now + timedelta(seconds=self.cache_ttl_s)

            with self._lock:
                self._cache = WeatherCacheEntry(
                    payload=raw_payload,
                    latitude=lat,
                    longitude=lon,
                    retrieved_at=now,
                    expires_at=expires_at,
                    location_source=loc_source,
                )

            curr_obj, fcast_obj, hourly_sum = self._parse_weather_payload(raw_payload)
            lims = list(base_limitations)
            if loc_source == LocationSource.LAST_KNOWN_LOCATION:
                lims.append(f"Using last known GPS location recorded {age_s:.0f}s ago. Current live GPS fix is unavailable.")

            return WeatherResponse(
                status="available",
                location_source=loc_source.value,
                location_timestamp=_iso(fix_at),
                location_age_seconds=age_s,
                coordinates={"latitude": lat, "longitude": lon},
                weather_source="open-meteo",
                weather_retrieved_at=_iso(now),
                weather_expires_at=_iso(expires_at),
                weather_cache_status=CacheStatus.FRESH.value,
                current=curr_obj.to_dict(),
                forecast=fcast_obj.to_dict(),
                hourly_summary=hourly_sum,
                limitations=lims,
            )

        except Exception as exc:
            log.warning("Open-Meteo weather request failed for lat=%.4f lon=%.4f: %s", lat, lon, exc)

            # On network/API failure, fallback to existing cache if unexpired and matching
            with self._lock:
                cache = self._cache
                if cache is not None and cache.matches_location(lat, lon, self.coord_tolerance_deg) and not cache.is_expired(now):
                    log.info("reusing unexpired weather cache following API failure")
                    curr_obj, fcast_obj, hourly_sum = self._parse_weather_payload(cache.payload)
                    lims = list(base_limitations) + [
                        f"Open-Meteo refresh failed ({exc}); displaying unexpired cached forecast."
                    ]
                    if loc_source == LocationSource.LAST_KNOWN_LOCATION:
                        lims.append(f"Using last known GPS location recorded {age_s:.0f}s ago.")

                    return WeatherResponse(
                        status="available",
                        location_source=loc_source.value,
                        location_timestamp=_iso(fix_at),
                        location_age_seconds=age_s,
                        coordinates={"latitude": lat, "longitude": lon},
                        weather_source=cache.weather_source,
                        weather_retrieved_at=_iso(cache.retrieved_at),
                        weather_expires_at=_iso(cache.expires_at),
                        weather_cache_status=CacheStatus.CACHED.value,
                        current=curr_obj.to_dict(),
                        forecast=fcast_obj.to_dict(),
                        hourly_summary=hourly_sum,
                        limitations=lims,
                    )

            # No usable cache exists
            lims = list(base_limitations) + [f"Weather retrieval failed: {exc}"]
            if loc_source == LocationSource.LAST_KNOWN_LOCATION:
                lims.append(f"Using last known GPS location recorded {age_s:.0f}s ago.")

            return WeatherResponse(
                status="unavailable",
                location_source=loc_source.value,
                location_timestamp=_iso(fix_at),
                location_age_seconds=age_s,
                coordinates={"latitude": lat, "longitude": lon},
                weather_source=None,
                weather_retrieved_at=None,
                weather_expires_at=None,
                weather_cache_status=CacheStatus.UNAVAILABLE.value,
                current=None,
                forecast=None,
                limitations=lims,
            )
