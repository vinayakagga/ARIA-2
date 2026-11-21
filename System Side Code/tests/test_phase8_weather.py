"""Unit and integration tests for ARIA Advice Engine Phase 2: Resilient Weather Service."""

from datetime import datetime, timedelta, timezone
import json
import math
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from server.database import Database
from server.telemetry import GPSData, TelemetryMessage
from server.weather import (
    ATTRIBUTION_TEXT,
    CacheStatus,
    LocationSource,
    WeatherCacheEntry,
    WeatherCurrent,
    WeatherForecastSummary,
    WeatherResponse,
    WeatherService,
    is_valid_gps_coordinates,
)


def _make_sample_open_meteo_payload(
    temp: float = 24.5,
    humidity: float = 65.0,
    precipitation: float = 0.0,
    wind_speed: float = 12.4,
    weather_code: int = 1,
) -> dict:
    """Helper to generate realistic Open-Meteo forecast JSON."""
    return {
        "latitude": 13.0827,
        "longitude": 80.2707,
        "generationtime_ms": 0.123,
        "utc_offset_seconds": 0,
        "timezone": "UTC",
        "timezone_abbreviation": "UTC",
        "elevation": 10.0,
        "current_units": {
            "time": "iso8601",
            "interval": "seconds",
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "precipitation": "mm",
            "rain": "mm",
            "wind_speed_10m": "km/h",
            "weather_code": "wmo code",
        },
        "current": {
            "time": "2026-05-18T10:00",
            "interval": 900,
            "temperature_2m": temp,
            "relative_humidity_2m": humidity,
            "precipitation": precipitation,
            "rain": 0.0,
            "wind_speed_10m": wind_speed,
            "weather_code": weather_code,
        },
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "precipitation": "mm",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": [
                f"2026-05-18T{h:02d}:00" for h in range(24)
            ],
            "temperature_2m": [temp + (i * 0.5) for i in range(24)],
            "relative_humidity_2m": [max(30.0, humidity - (i * 1.0)) for i in range(24)],
            "precipitation": [0.0, 0.2, 0.5, 0.1, 0.0, 0.0] + [0.0] * 18,
            "precipitation_probability": [10, 25, 45, 30, 15, 5] + [0] * 18,
            "wind_speed_10m": [wind_speed + (i * 0.2) for i in range(24)],
        },
    }


def _mock_urlopen_response(payload: dict, status: int = 200):
    """Create a mock HTTPResponse context manager for urllib.request.urlopen."""
    body_bytes = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.getcode.return_value = status
    mock_resp.read.return_value = body_bytes
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def _make_telemetry(
    device_id: str = "aria-c3",
    received_at: datetime | None = None,
    temperature_c: float | None = 25.0,
    humidity_pct: float | None = 60.0,
    light_lux: float | None = 500.0,
    mq135_raw: int | None = 120,
    gps: GPSData | None = None,
) -> TelemetryMessage:
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    return TelemetryMessage(
        schema_version=1,
        device_id=device_id,
        source="real",
        received_at=received_at,
        timestamp=received_at,
        temperature_c=temperature_c,
        humidity_pct=humidity_pct,
        light_lux=light_lux,
        mq135_raw=mq135_raw,
        gps=gps,
    )


class TestGPSValidation(unittest.TestCase):
    """Test latitude and longitude sanity and boundary validation."""

    def test_valid_coordinates(self):
        self.assertTrue(is_valid_gps_coordinates(0.0, 0.0))
        self.assertTrue(is_valid_gps_coordinates(13.0827, 80.2707))
        self.assertTrue(is_valid_gps_coordinates(-90.0, -180.0))
        self.assertTrue(is_valid_gps_coordinates(90.0, 180.0))
        self.assertTrue(is_valid_gps_coordinates(45, -73))  # ints

    def test_invalid_coordinates(self):
        self.assertFalse(is_valid_gps_coordinates(90.0001, 0.0))
        self.assertFalse(is_valid_gps_coordinates(-90.0001, 0.0))
        self.assertFalse(is_valid_gps_coordinates(0.0, 180.0001))
        self.assertFalse(is_valid_gps_coordinates(0.0, -180.0001))
        self.assertFalse(is_valid_gps_coordinates(None, 80.0))
        self.assertFalse(is_valid_gps_coordinates(13.0, None))
        self.assertFalse(is_valid_gps_coordinates(True, 80.0))  # booleans are not valid coords
        self.assertFalse(is_valid_gps_coordinates(13.0, False))
        self.assertFalse(is_valid_gps_coordinates("13.08", "80.27"))  # strings
        self.assertFalse(is_valid_gps_coordinates(float("nan"), 80.0))


class TestPersistentGPSMemory(unittest.TestCase):
    """Test SQLite storage, recovery, and staleness of last confirmed GPS coordinates."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_save_and_retrieve_last_known_gps(self):
        now = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.db.save_last_known_gps(
            device_id="aria-c3",
            latitude=13.0827,
            longitude=80.2707,
            last_fix_at=now,
            updated_at=now,
        )

        record = self.db.get_last_known_gps("aria-c3")
        self.assertIsNotNone(record)
        self.assertEqual(record["device_id"], "aria-c3")
        self.assertAlmostEqual(record["latitude"], 13.0827, places=4)
        self.assertAlmostEqual(record["longitude"], 80.2707, places=4)
        self.assertEqual(record["last_fix_at"], now)

    def test_survives_database_restart(self):
        now = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.db.save_last_known_gps("aria-c3", 12.9716, 77.5946, now, now)
        self.db.close()

        # Reopen same database file
        db2 = Database(self.tmp.name)
        record = db2.get_last_known_gps("aria-c3")
        self.assertIsNotNone(record)
        self.assertAlmostEqual(record["latitude"], 12.9716, places=4)
        db2.close()

    def test_separate_devices_stored_independently(self):
        t1 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 18, 11, 0, 0, tzinfo=timezone.utc)
        self.db.save_last_known_gps("rover-1", 10.0, 20.0, t1, t1)
        self.db.save_last_known_gps("rover-2", 30.0, 40.0, t2, t2)

        r1 = self.db.get_last_known_gps("rover-1")
        r2 = self.db.get_last_known_gps("rover-2")
        self.assertEqual(r1["latitude"], 10.0)
        self.assertEqual(r2["latitude"], 30.0)

    def test_insert_telemetry_automatically_persists_valid_gps(self):
        now = datetime(2026, 5, 18, 12, 30, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=15.1234, longitude=75.5678, satellites=7),
        )
        self.db.insert_telemetry(telem)

        stored = self.db.get_last_known_gps("aria-c3")
        self.assertIsNotNone(stored)
        self.assertAlmostEqual(stored["latitude"], 15.1234, places=4)
        self.assertAlmostEqual(stored["longitude"], 75.5678, places=4)

    def test_resolve_location_prefers_current_fix(self):
        service = WeatherService(db=self.db)
        now = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=13.08, longitude=80.27, satellites=6),
        )

        source, lat, lon, fix_at, age_s = service.resolve_location(telem, now=now)
        self.assertEqual(source, LocationSource.CURRENT_LOCATION)
        self.assertAlmostEqual(lat, 13.08, places=2)
        self.assertAlmostEqual(lon, 80.27, places=2)
        self.assertEqual(fix_at, now)
        self.assertAlmostEqual(age_s, 0.0, places=1)

    def test_resolve_location_falls_back_to_last_known_on_fix_loss(self):
        service = WeatherService(db=self.db)
        t_fix = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.db.save_last_known_gps("aria-c3", 13.0827, 80.2707, t_fix, t_fix)

        # Rover loses GPS fix 30 minutes later
        t_now = t_fix + timedelta(minutes=30)
        telem_no_fix = _make_telemetry(
            received_at=t_now,
            gps=GPSData(fix=False, latitude=None, longitude=None),
        )

        source, lat, lon, fix_at, age_s = service.resolve_location(telem_no_fix, now=t_now)
        self.assertEqual(source, LocationSource.LAST_KNOWN_LOCATION)
        self.assertAlmostEqual(lat, 13.0827, places=4)
        self.assertAlmostEqual(lon, 80.2707, places=4)
        self.assertEqual(fix_at, t_fix)
        self.assertAlmostEqual(age_s, 1800.0, places=1)

    def test_resolve_location_marks_unavailable_when_stale_over_24h(self):
        service = WeatherService(db=self.db, max_location_age_s=86400.0)
        t_fix = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.db.save_last_known_gps("aria-c3", 13.0827, 80.2707, t_fix, t_fix)

        # 25 hours later
        t_stale = t_fix + timedelta(hours=25)
        source, lat, lon, fix_at, age_s = service.resolve_location(None, device_id="aria-c3", now=t_stale)
        self.assertEqual(source, LocationSource.LOCATION_UNAVAILABLE)
        self.assertIsNotNone(lat)  # Coords still returned for reference
        self.assertGreater(age_s, 86400.0)


class TestOpenMeteoFetchAndParsing(unittest.TestCase):
    """Test REST API interaction, payload parsing, and HTTP error handling."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.service = WeatherService(db=self.db)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    @patch("urllib.request.urlopen")
    def test_fetch_success_and_parsing(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=28.5, humidity=70.0, precipitation=1.2, wind_speed=15.0, weather_code=61)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            temperature_c=28.0,
            humidity_pct=68.0,
            light_lux=600.0,
            mq135_raw=125,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "available")
        self.assertEqual(resp.location_source, LocationSource.CURRENT_LOCATION.value)
        self.assertEqual(resp.weather_cache_status, CacheStatus.FRESH.value)
        self.assertEqual(resp.weather_source, "open-meteo")
        self.assertEqual(resp.attribution, ATTRIBUTION_TEXT)

        # Check current conditions
        self.assertIsNotNone(resp.current)
        self.assertEqual(resp.current["temperature_c"], 28.5)
        self.assertEqual(resp.current["temperature_2m"], 28.5)
        self.assertEqual(resp.current["humidity_pct"], 70.0)
        self.assertEqual(resp.current["precipitation_mm"], 1.2)
        self.assertEqual(resp.current["wind_speed_kmh"], 15.0)
        self.assertEqual(resp.current["weather_code"], 61)

        # Check forecast summary (first 6 hours of sample)
        self.assertIsNotNone(resp.forecast)
        self.assertEqual(resp.forecast["precipitation_probability_next_6h_pct"], 45)
        self.assertEqual(resp.forecast["precipitation_next_6h_mm"], 0.8)  # 0.0 + 0.2 + 0.5 + 0.1 + 0.0 + 0.0
        self.assertEqual(resp.forecast["maximum_wind_speed_kmh"], 16.0)

        # Check request URL
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn("https://api.open-meteo.com/v1/forecast", req.full_url)
        self.assertIn("latitude=13.0827", req.full_url)
        self.assertIn("longitude=80.2707", req.full_url)
        self.assertEqual(req.headers.get("User-agent"), "ARIA-Agricultural-Rover/1.0")

    @patch("urllib.request.urlopen")
    def test_http_429_rate_limit_handled(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.open-meteo.com/v1/forecast",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=None,
        )

        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "unavailable")
        self.assertEqual(resp.weather_cache_status, CacheStatus.UNAVAILABLE.value)
        self.assertIsNone(resp.current)
        self.assertTrue(any("429" in lim for lim in resp.limitations))

    @patch("urllib.request.urlopen")
    def test_network_timeout_handled(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("Request timed out")

        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "unavailable")
        self.assertTrue(any("timed out" in lim.lower() for lim in resp.limitations))

    @patch("urllib.request.urlopen")
    def test_malformed_json_handled(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.read.return_value = b"{broken json"
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        mock_urlopen.return_value = mock_resp

        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "unavailable")


class TestWeatherCache(unittest.TestCase):
    """Test 15-minute weather forecast caching and coordinate matching tolerance."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.service = WeatherService(db=self.db, cache_ttl_s=900.0, coord_tolerance_deg=0.01)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    @patch("urllib.request.urlopen")
    def test_cache_hit_within_ttl_and_tolerance(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=25.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        # Call 1: Fresh fetch
        resp1 = self.service.get_weather(telem, now=t0)
        self.assertEqual(resp1.weather_cache_status, CacheStatus.FRESH.value)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Call 2: 5 minutes later, slight drift (0.002 deg ~ 200m), should hit cache
        t1 = t0 + timedelta(minutes=5)
        telem_drift = _make_telemetry(
            received_at=t1,
            gps=GPSData(fix=True, latitude=13.0840, longitude=80.2720),
        )
        resp2 = self.service.get_weather(telem_drift, now=t1)
        self.assertEqual(resp2.weather_cache_status, CacheStatus.CACHED.value)
        self.assertEqual(mock_urlopen.call_count, 1)  # No second HTTP call

    @patch("urllib.request.urlopen")
    def test_cache_miss_on_large_location_change(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=25.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem1 = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )
        self.service.get_weather(telem1, now=t0)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Rover moved 10 km away (0.09 deg) 2 minutes later
        t1 = t0 + timedelta(minutes=2)
        telem2 = _make_telemetry(
            received_at=t1,
            gps=GPSData(fix=True, latitude=13.1800, longitude=80.2707),
        )
        resp2 = self.service.get_weather(telem2, now=t1)
        self.assertEqual(resp2.weather_cache_status, CacheStatus.FRESH.value)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_cache_miss_after_ttl_expires(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=25.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )
        self.service.get_weather(telem, now=t0)
        self.assertEqual(mock_urlopen.call_count, 1)

        # 16 minutes later (> 15 min TTL)
        t_expired = t0 + timedelta(minutes=16)
        resp2 = self.service.get_weather(telem, now=t_expired)
        self.assertEqual(resp2.weather_cache_status, CacheStatus.FRESH.value)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_force_refresh_bypasses_valid_cache(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=25.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )
        self.service.get_weather(telem, now=t0)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Force refresh 1 minute later
        t1 = t0 + timedelta(minutes=1)
        resp = self.service.get_weather(telem, force_refresh=True, now=t1)
        self.assertEqual(resp.weather_cache_status, CacheStatus.FRESH.value)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_fallback_to_unexpired_cache_on_api_failure(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=26.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )
        self.service.get_weather(telem, now=t0)

        # Now API goes down with 500 error, but cache is 10 minutes old (< 15 min TTL)
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.open-meteo.com/v1/forecast",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )

        t1 = t0 + timedelta(minutes=10)
        # Using force_refresh=True to trigger the fetch attempt
        resp = self.service.get_weather(telem, force_refresh=True, now=t1)
        self.assertEqual(resp.status, "available")
        self.assertEqual(resp.weather_cache_status, CacheStatus.CACHED.value)
        self.assertEqual(resp.current["temperature_c"], 26.0)
        self.assertTrue(any("failed" in lim.lower() for lim in resp.limitations))

    @patch("urllib.request.urlopen")
    def test_reuse_cached_weather_on_gps_fix_loss(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=27.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        t0 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem_fix = _make_telemetry(
            received_at=t0,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )
        self.service.get_weather(telem_fix, now=t0)

        # 5 minutes later, rover loses GPS fix completely
        t1 = t0 + timedelta(minutes=5)
        telem_no_fix = _make_telemetry(
            received_at=t1,
            gps=GPSData(fix=False, latitude=None, longitude=None),
        )

        resp = self.service.get_weather(telem_no_fix, now=t1)
        self.assertEqual(resp.status, "available")
        self.assertEqual(resp.location_source, LocationSource.LAST_KNOWN_LOCATION.value)
        self.assertEqual(resp.weather_cache_status, CacheStatus.CACHED.value)
        self.assertEqual(resp.current["temperature_c"], 27.0)
        self.assertTrue(any("last known gps location" in lim.lower() for lim in resp.limitations))


class TestFaultToleranceAndEdgeCases(unittest.TestCase):
    """Test resilience when physical sensors are missing, rover offline, or coordinates invalid."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(self.tmp.name)
        self.service = WeatherService(db=self.db)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    @patch("urllib.request.urlopen")
    def test_weather_retrieved_even_when_all_physical_sensors_null(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=22.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        # All sensor readings are None, but GPS is valid
        telem = _make_telemetry(
            received_at=now,
            temperature_c=None,
            humidity_pct=None,
            light_lux=None,
            mq135_raw=None,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "available")
        self.assertEqual(resp.current["temperature_c"], 22.0)

    def test_graceful_response_when_no_gps_ever_and_empty_db(self):
        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=False, latitude=None, longitude=None),
        )

        resp = self.service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "unavailable")
        self.assertEqual(resp.location_source, LocationSource.LOCATION_UNAVAILABLE.value)
        self.assertIsNone(resp.current)
        self.assertIsNone(resp.forecast)
        self.assertTrue(any("No valid current or sufficiently recent" in lim for lim in resp.limitations))

    def test_none_telemetry_gracefully_handled(self):
        # Rover offline, no telemetry passed
        resp = self.service.get_weather(None, device_id="aria-c3")
        self.assertEqual(resp.status, "unavailable")
        self.assertEqual(resp.location_source, LocationSource.LOCATION_UNAVAILABLE.value)

    @patch("urllib.request.urlopen")
    def test_database_error_does_not_crash_weather_service(self, mock_urlopen):
        sample = _make_sample_open_meteo_payload(temp=25.0)
        mock_urlopen.return_value = _mock_urlopen_response(sample)

        # Mock DB failure
        bad_db = MagicMock()
        bad_db.save_last_known_gps.side_effect = RuntimeError("DB write failed")
        bad_db.get_last_known_gps.side_effect = RuntimeError("DB read failed")

        service = WeatherService(db=bad_db)
        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        telem = _make_telemetry(
            received_at=now,
            gps=GPSData(fix=True, latitude=13.0827, longitude=80.2707),
        )

        # Should not raise exception
        resp = service.get_weather(telem, now=now)
        self.assertEqual(resp.status, "available")
        self.assertEqual(resp.current["temperature_c"], 25.0)

    def test_tls_verification_strictly_enabled(self):
        """TLS certificate verification and hostname checking must be strictly enabled."""
        import ssl
        from server.weather import _build_ssl_context
        ctx = _build_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
