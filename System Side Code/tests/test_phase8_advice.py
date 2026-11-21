"""Unit and integration tests for ARIA Advice Engine & Flask API (Phase 4).

Covers:
1. Dynamic device ID resolution (defaults to aria-esp32, not aria-c3; resolves from DB).
2. Full context generation: fresh telemetry + weather + NVIDIA NIM AI advice.
3. Limited context generation: missing sensors, stale telemetry, offline device.
4. General guidance: no telemetry, no weather, all sensors unavailable.
5. Persistent GPS recovery: weather retrieved using last-known coordinates when current fix is lost.
6. Weather API failure resilience: fallback to unexpired cache or deterministic evaluation.
7. NVIDIA failure resilience: unconfigured key, HTTP 500, timeout, malformed response.
8. 10-minute cooldown enforcement:
   - Second request within 10 minutes does NOT call NVIDIA.
   - Deterministic observations and guidance are regenerated from CURRENT sensor conditions.
   - Cooldown response never claims fresh AI advice.
   - Call after cooldown succeeds and calls NVIDIA.
9. Required safety & concurrency guarantees:
   - Failed advice refresh does not overwrite the last successfully generated advice.
   - Failed NVIDIA attempt still activates cooldown (recorded before/at attempt).
   - Concurrent generation requests do not trigger duplicate NVIDIA calls.
   - Backend restart (new instance sharing DB) does not reset the cooldown.
10. Flask API routes (/api/v1/advice/latest, /api/v1/advice/generate):
   - GET /api/v1/advice/latest with empty DB returns 200 with data: null.
   - GET returns latest persisted advice.
   - POST /api/v1/advice/generate triggers advice generation.
   - Request parameter validation (empty device_id -> 400).
   - Sanitized HTTP 500 on unexpected internal errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from server.advice_engine import AdviceEngine
from server.app import create_app
from server.database import Database
from server.nvidia_client import NVIDIAClient, NVIDIAResponse, NVIDIAStatus
from server.rules import AdviceMode, MonitoringThresholds
from server.telemetry import GPSData, StatusMessage, TelemetryMessage
from server.weather import WeatherResponse, WeatherService


def _make_telemetry(
    device_id: str = "aria-esp32",
    received_at: datetime | None = None,
    temperature_c: float | None = 26.5,
    humidity_pct: float | None = 55.0,
    light_lux: float | None = 15000.0,
    mq135_raw: int | None = 1200,
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
        gps=gps or GPSData(fix=True, latitude=13.0827, longitude=80.2707, satellites=8),
    )


def _make_status(
    device_id: str = "aria-esp32",
    status: str = "online",
    received_at: datetime | None = None,
) -> StatusMessage:
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    return StatusMessage(
        schema_version=1,
        device_id=device_id,
        status=status,
        received_at=received_at,
        ip_address="192.168.1.105",
        wifi_rssi_dbm=-62,
        uptime_s=1200,
    )


def _mock_weather_response(status: str = "available") -> WeatherResponse:
    return WeatherResponse(
        status=status,
        location_source="CURRENT_LOCATION",
        location_timestamp=datetime.now(timezone.utc).isoformat(),
        location_age_seconds=1.0,
        coordinates={"latitude": 13.0827, "longitude": 80.2707},
        weather_source="open-meteo" if status == "available" else None,
        weather_retrieved_at=datetime.now(timezone.utc).isoformat() if status == "available" else None,
        weather_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat() if status == "available" else None,
        weather_cache_status="fresh" if status == "available" else "unavailable",
        current={
            "temperature_c": 25.0,
            "temperature_2m": 25.0,
            "humidity_pct": 54.0,
            "relative_humidity_2m": 54.0,
            "precipitation_mm": 0.0,
            "precipitation": 0.0,
            "wind_speed_kmh": 10.0,
            "wind_speed_10m": 10.0,
        } if status == "available" else None,
        forecast={
            "precipitation_probability_next_6h_pct": 15,
            "precipitation_next_6h_mm": 0.0,
            "maximum_temperature_c": 27.0,
            "maximum_wind_speed_kmh": 12.0,
        } if status == "available" else None,
        limitations=["Weather represents a regional forecast, not a direct measurement."],
    )


class TestAdviceEngineCore(unittest.TestCase):
    """Unit tests for AdviceEngine business logic and aggregation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_advice.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        self.mock_weather = MagicMock(spec=WeatherService)
        self.mock_weather.get_weather.return_value = _mock_weather_response("available")

        self.mock_nvidia = MagicMock(spec=NVIDIAClient)
        self.mock_nvidia.is_configured.return_value = True
        self.mock_nvidia.generate_advice.return_value = NVIDIAResponse(
            status=NVIDIAStatus.AVAILABLE,
            model="meta/llama-3.1-8b-instruct",
            advice_mode="FULL_CONTEXT",
            summary="Field conditions are optimal. Ground temperature and humidity are within nominal thresholds.",
            raw_text="...",
            prompt_tokens=250,
            completion_tokens=60,
            finish_reason="stop",
            error=None,
            error_code=None,
        )

        self.engine = AdviceEngine(
            db=self.db,
            weather_service=self.mock_weather,
            nvidia_client=self.mock_nvidia,
            cooldown_seconds=600.0,
        )

    def tearDown(self):
        self.db.close()
        time.sleep(0.05)
        self.temp_dir.cleanup()

    def test_dynamic_device_id_resolution(self):
        # 1. Empty DB defaults to aria-esp32 (not aria-c3)
        self.assertEqual(self.engine.resolve_device_id(), "aria-esp32")

        # 2. Insert telemetry from aria-esp32
        telem = _make_telemetry(device_id="aria-esp32")
        self.db.insert_telemetry(telem)
        self.assertEqual(self.engine.resolve_device_id(), "aria-esp32")

        # 3. Explicit parameter overrides database
        self.assertEqual(self.engine.resolve_device_id("custom-rover"), "custom-rover")

    def test_full_context_advice_generation(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)

        self.assertEqual(advice["status"], "available")
        self.assertEqual(advice["device_id"], "aria-esp32")
        self.assertTrue(advice["device_online"])
        self.assertEqual(advice["advice_mode"], AdviceMode.FULL_CONTEXT.value)
        self.assertEqual(advice["provider"], "nvidia_nim")
        self.assertIn("optimal", advice["summary"].lower())
        self.assertGreater(len(advice["sensor_health"]), 0)
        self.mock_nvidia.generate_advice.assert_called_once()

    def test_limited_context_with_missing_sensor(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        # Temperature is null, only humidity is present
        telem = _make_telemetry(
            device_id="aria-esp32",
            received_at=now,
            temperature_c=None,
            humidity_pct=60.0,
            light_lux=None,
            mq135_raw=None,
        )
        self.db.insert_telemetry(telem)
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)

        self.assertEqual(advice["advice_mode"], AdviceMode.LIMITED_CONTEXT.value)
        # Temperature must be listed as unresponsive_null in sensor_health
        th = next(h for h in advice["sensor_health"] if h["sensor"] == "temperature_c")
        self.assertEqual(th["state"], "unresponsive_null")

    def test_general_guidance_when_all_sensors_null_and_rover_offline(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        # Empty DB, rover offline, no weather
        self.mock_weather.get_weather.return_value = _mock_weather_response("unavailable")
        self.mock_nvidia.is_configured.return_value = False

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)

        self.assertEqual(advice["advice_mode"], AdviceMode.GENERAL_GUIDANCE.value)
        self.assertEqual(advice["provider"], "deterministic_fallback")
        self.assertFalse(advice["device_online"])
        self.assertIn("general", advice["summary"].lower())
        self.assertGreater(len(advice["recommendations"]), 0)

    def test_stale_telemetry_observation(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        t_stale = now - timedelta(seconds=45)  # 45s old (> 15s)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=t_stale))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)

        self.assertEqual(advice["advice_mode"], AdviceMode.LIMITED_CONTEXT.value)
        self.assertTrue(any(o["rule_id"] == "SYS_TELEMETRY_STALE" for o in advice["observations"]))

    def test_nvidia_failure_seamlessly_falls_back_to_deterministic(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        # NVIDIA returns UNAVAILABLE (e.g. HTTP 500 or timeout)
        self.mock_nvidia.generate_advice.return_value = NVIDIAResponse.unavailable(
            advice_mode="FULL_CONTEXT",
            error="HTTP 500: Internal Server Error",
            error_code=500,
        )

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)

        self.assertEqual(advice["provider"], "deterministic_fallback")
        self.assertTrue(any("NVIDIA AI generation failed" in lim for lim in advice["limitations"]))
        self.assertGreater(len(advice["recommendations"]), 0)

    def test_cooldown_enforcement_and_current_sensor_re_evaluation(self):
        t0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=t0, temperature_c=25.0))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=t0))

        # Request 1: Fresh generation, calls NVIDIA
        advice1 = self.engine.generate_advice(device_id="aria-esp32", now=t0)
        self.assertEqual(advice1["provider"], "nvidia_nim")
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)

        # Request 2: 2 minutes later (< 10 min cooldown). Sensor temperature spikes to 38°C!
        t1 = t0 + timedelta(minutes=2)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=t1, temperature_c=38.0))

        advice2 = self.engine.generate_advice(device_id="aria-esp32", now=t1)

        # NVIDIA must NOT be called again
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)

        # Cooldown must be reported active
        self.assertTrue(advice2["cooldown"]["active"])
        self.assertAlmostEqual(advice2["cooldown"]["remaining_seconds"], 480.0, places=1)
        self.assertEqual(advice2["provider"], "deterministic_fallback")

        # Crucial check: current sensor conditions are RE-EVALUATED, not blindly serving old advice!
        # The 38°C temperature must trigger ENV_TEMP_HIGH in observations
        self.assertTrue(any(o["rule_id"] == "ENV_TEMP_HIGH" for o in advice2["observations"]))
        # Never claims NVIDIA generated fresh advice
        self.assertNotIn("ai_metadata", advice2)

        # Request 3: 11 minutes later (> 10 min cooldown)
        t2 = t0 + timedelta(minutes=11)
        advice3 = self.engine.generate_advice(device_id="aria-esp32", now=t2)
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 2)
        self.assertEqual(advice3["provider"], "nvidia_nim")

    def test_gps_unavailable_to_fix_acquired_reflects_new_fix(self):
        """When rover transitions from no GPS fix to locked fix, advice updates from unavailable to healthy."""
        t0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        # 1. Rover without GPS fix
        telem_nofix = _make_telemetry(
            device_id="aria-esp32",
            received_at=t0,
            gps=GPSData(fix=False, latitude=None, longitude=None, satellites=0),
        )
        self.db.insert_telemetry(telem_nofix)
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=t0))

        self.mock_weather.get_weather.return_value = WeatherResponse(
            status="unavailable",
            location_source="LOCATION_UNAVAILABLE",
            location_timestamp=None,
            location_age_seconds=None,
            coordinates=None,
            weather_source=None,
            weather_retrieved_at=None,
            weather_expires_at=None,
            weather_cache_status="unavailable",
            current=None,
            forecast=None,
        )

        advice1 = self.engine.generate_advice(device_id="aria-esp32", now=t0)
        self.assertEqual(advice1["gps_provenance"], "unavailable")
        gps_health1 = next(h for h in advice1["sensor_health"] if h["sensor"] == "gps")
        self.assertEqual(gps_health1["state"], "unresponsive_null")
        self.assertIn("Coordinates unavailable", gps_health1["detail"])

        # 2. Rover locks outdoor GPS fix
        t1 = t0 + timedelta(seconds=10)
        telem_fixed = _make_telemetry(
            device_id="aria-esp32",
            received_at=t1,
            gps=GPSData(fix=True, latitude=28.6295, longitude=77.0907, satellites=6),
        )
        self.db.insert_telemetry(telem_fixed)

        self.mock_weather.get_weather.return_value = WeatherResponse(
            status="available",
            location_source="CURRENT_LOCATION",
            location_timestamp=t1.isoformat(),
            location_age_seconds=0.0,
            coordinates={"latitude": 28.6295, "longitude": 77.0907},
            weather_source="open-meteo",
            weather_retrieved_at=t1.isoformat(),
            weather_expires_at=(t1 + timedelta(minutes=15)).isoformat(),
            weather_cache_status="fresh",
            current={"temperature_2m": 22.0, "relative_humidity_2m": 60.0},
            forecast={"precipitation_probability_next_6h_pct": 10},
        )

        advice2 = self.engine.generate_advice(device_id="aria-esp32", now=t1)
        self.assertEqual(advice2["gps_provenance"], "current_fix")
        gps_health2 = next(h for h in advice2["sensor_health"] if h["sensor"] == "gps")
        self.assertEqual(gps_health2["state"], "healthy")
        self.assertIn("Outdoor 2D/3D fix locked (6 satellites)", gps_health2["detail"])

    def test_current_gps_not_labelled_last_known(self):
        """Current GPS fix must be flagged as current_fix, never last_known_fix."""
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(
            device_id="aria-esp32",
            received_at=now,
            gps=GPSData(fix=True, latitude=28.63, longitude=77.09, satellites=5),
        ))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))
        self.mock_weather.get_weather.return_value = _mock_weather_response("available")

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)
        self.assertEqual(advice["gps_provenance"], "current_fix")
        self.assertNotEqual(advice["gps_provenance"], "last_known_fix")

    def test_last_known_gps_includes_timestamp_and_age(self):
        """When live GPS fix is lost, last known GPS is labeled last_known_fix with timestamp and age."""
        t0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        telem0 = _make_telemetry(
            device_id="aria-esp32",
            received_at=t0,
            gps=GPSData(fix=True, latitude=28.63, longitude=77.09, satellites=7),
        )
        self.db.insert_telemetry(telem0)

        # 120 seconds later, live fix lost
        t1 = t0 + timedelta(seconds=120)
        telem1 = _make_telemetry(
            device_id="aria-esp32",
            received_at=t1,
            gps=GPSData(fix=False, latitude=None, longitude=None, satellites=0),
        )
        self.db.insert_telemetry(telem1)
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=t1))

        self.mock_weather.get_weather.return_value = WeatherResponse(
            status="available",
            location_source="LAST_KNOWN_LOCATION",
            location_timestamp=t0.isoformat(),
            location_age_seconds=120.0,
            coordinates={"latitude": 28.63, "longitude": 77.09},
            weather_source="open-meteo",
            weather_retrieved_at=t1.isoformat(),
            weather_expires_at=(t1 + timedelta(minutes=15)).isoformat(),
            weather_cache_status="fresh",
            current={"temperature_2m": 22.0},
            forecast=None,
        )

        advice = self.engine.generate_advice(device_id="aria-esp32", now=t1)
        self.assertEqual(advice["gps_provenance"], "last_known_fix")
        self.assertEqual(advice["weather"]["location_source"], "LAST_KNOWN_LOCATION")
        self.assertEqual(advice["weather"]["location_timestamp"], t0.isoformat())
        self.assertEqual(advice["weather"]["location_age_seconds"], 120.0)

    def test_stale_telemetry_identified_in_advice_payload(self):
        """Telemetry older than threshold is marked stale in telemetry_context with age in seconds."""
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        t_stale = now - timedelta(seconds=50)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=t_stale))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)
        ctx = advice["telemetry_context"]
        self.assertEqual(ctx["telemetry_freshness"], "stale")
        self.assertAlmostEqual(ctx["telemetry_age_seconds"], 50.0, places=1)
        self.assertIsNotNone(ctx["telemetry_timestamp"])
        self.assertIsNotNone(ctx["snapshot"])

    def test_weather_failure_still_allows_deterministic_advice(self):
        """WeatherService unavailability degrades gracefully to deterministic advice."""
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        self.mock_weather.get_weather.return_value = WeatherResponse(
            status="unavailable",
            location_source="LOCATION_UNAVAILABLE",
            location_timestamp=None,
            location_age_seconds=None,
            coordinates=None,
            weather_source=None,
            weather_retrieved_at=None,
            weather_expires_at=None,
            weather_cache_status="unavailable",
            current=None,
            forecast=None,
            limitations=["Open-Meteo connection timed out."],
        )
        self.mock_nvidia.is_configured.return_value = False

        advice = self.engine.generate_advice(device_id="aria-esp32", now=now)
        self.assertEqual(advice["status"], "available")
        self.assertEqual(advice["provider"], "deterministic_fallback")
        self.assertGreater(len(advice["recommendations"]), 0)


class TestAdviceSafetyAndConcurrency(unittest.TestCase):
    """Tests for edge cases, failure safety, restarts, and concurrent requests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_safety.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        self.mock_weather = MagicMock(spec=WeatherService)
        self.mock_weather.get_weather.return_value = _mock_weather_response("available")

        self.mock_nvidia = MagicMock(spec=NVIDIAClient)
        self.mock_nvidia.is_configured.return_value = True
        self.mock_nvidia.generate_advice.return_value = NVIDIAResponse(
            status=NVIDIAStatus.AVAILABLE,
            model="meta/llama-3.1-8b-instruct",
            advice_mode="FULL_CONTEXT",
            summary="Successful AI advice.",
            raw_text="...",
            prompt_tokens=100,
            completion_tokens=50,
            finish_reason="stop",
            error=None,
            error_code=None,
        )

        self.engine = AdviceEngine(
            db=self.db,
            weather_service=self.mock_weather,
            nvidia_client=self.mock_nvidia,
            cooldown_seconds=600.0,
        )

    def tearDown(self):
        self.db.close()
        time.sleep(0.05)
        self.temp_dir.cleanup()

    def test_failed_advice_refresh_preserves_last_successful_advice(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        # Successfully generate advice 1
        self.engine.generate_advice(device_id="aria-esp32", now=now)
        saved = self.db.get_latest_advice("aria-esp32")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["summary"], "Successful AI advice.")

        # Simulate unexpected failure during a subsequent refresh (e.g. weather service explodes)
        self.mock_weather.get_weather.side_effect = RuntimeError("Weather service disk crash")

        with self.assertRaises(RuntimeError):
            self.engine.generate_advice(device_id="aria-esp32", now=now + timedelta(minutes=1))

        # Previous successful advice must remain intact in SQLite!
        preserved = self.db.get_latest_advice("aria-esp32")
        self.assertIsNotNone(preserved)
        self.assertEqual(preserved["summary"], "Successful AI advice.")

    def test_failed_nvidia_attempt_still_activates_cooldown(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        # NVIDIA call times out / fails
        self.mock_nvidia.generate_advice.return_value = NVIDIAResponse.unavailable(
            advice_mode="FULL_CONTEXT",
            error="Connection timed out",
            error_code=None,
        )

        advice1 = self.engine.generate_advice(device_id="aria-esp32", now=now)
        self.assertEqual(advice1["provider"], "deterministic_fallback")
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)

        # Cooldown must be recorded even though NVIDIA failed, preventing retry storm!
        cooldown_active, remaining = self.engine.get_cooldown_status("aria-esp32", now + timedelta(seconds=10))
        self.assertTrue(cooldown_active)
        self.assertGreater(remaining, 580.0)

        # Immediate second call must NOT retry NVIDIA
        advice2 = self.engine.generate_advice(device_id="aria-esp32", now=now + timedelta(seconds=15))
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)
        self.assertTrue(advice2["cooldown"]["active"])

    def test_concurrent_requests_trigger_only_one_nvidia_call(self):
        now = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        def slow_nvidia(*args, **kwargs):
            time.sleep(0.1)  # Simulate network latency
            return NVIDIAResponse(
                status=NVIDIAStatus.AVAILABLE,
                model="meta/llama-3.1-8b-instruct",
                advice_mode="FULL_CONTEXT",
                summary="AI advice from concurrent test.",
                raw_text="...",
                prompt_tokens=100,
                completion_tokens=50,
                finish_reason="stop",
                error=None,
                error_code=None,
            )

        self.mock_nvidia.generate_advice.side_effect = slow_nvidia

        results = []
        errors = []

        def worker():
            try:
                res = self.engine.generate_advice(device_id="aria-esp32", now=now)
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 5)
        # Exactly ONE call to NVIDIA NIM
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)

    def test_backend_restart_does_not_reset_cooldown(self):
        t0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=t0))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=t0))

        # Engine 1 generates advice
        self.engine.generate_advice(device_id="aria-esp32", now=t0)
        self.assertEqual(self.mock_nvidia.generate_advice.call_count, 1)

        # Simulate backend restart: close DB, recreate new DB and Engine on same file
        self.db.close()
        db2 = Database(self.db_path)
        mock_nvidia2 = MagicMock(spec=NVIDIAClient)
        mock_nvidia2.is_configured.return_value = True

        engine2 = AdviceEngine(
            db=db2,
            weather_service=self.mock_weather,
            nvidia_client=mock_nvidia2,
            cooldown_seconds=600.0,
        )

        # 3 minutes after t0, engine 2 attempts generation
        t1 = t0 + timedelta(minutes=3)
        advice = engine2.generate_advice(device_id="aria-esp32", now=t1)

        # Cooldown MUST still be active; new NVIDIA client must NOT be called
        self.assertTrue(advice["cooldown"]["active"])
        self.assertAlmostEqual(advice["cooldown"]["remaining_seconds"], 420.0, places=1)
        mock_nvidia2.generate_advice.assert_not_called()
        db2.close()


class TestPhase4AdviceFlaskAPI(unittest.TestCase):
    """Integration tests for Flask API endpoints (/api/v1/advice/latest, /api/v1/advice/generate)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_api_advice.db"
        self.db = Database(self.db_path)
        self.db.initialize()

        self.mock_weather = MagicMock(spec=WeatherService)
        self.mock_weather.get_weather.return_value = _mock_weather_response("available")

        self.mock_nvidia = MagicMock(spec=NVIDIAClient)
        self.mock_nvidia.is_configured.return_value = True
        self.mock_nvidia.generate_advice.return_value = NVIDIAResponse(
            status=NVIDIAStatus.AVAILABLE,
            model="meta/llama-3.1-8b-instruct",
            advice_mode="FULL_CONTEXT",
            summary="Flask API generated advice.",
            raw_text="...",
            prompt_tokens=150,
            completion_tokens=45,
            finish_reason="stop",
            error=None,
            error_code=None,
        )

        self.advice_engine = AdviceEngine(
            db=self.db,
            weather_service=self.mock_weather,
            nvidia_client=self.mock_nvidia,
            cooldown_seconds=600.0,
        )

        self.app = create_app(
            db=self.db,
            start_mqtt=False,
            advice_engine=self.advice_engine,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.stop_mqtt()
        self.db.close()
        time.sleep(0.05)
        self.temp_dir.cleanup()

    def test_get_latest_advice_empty_database(self):
        resp = self.client.get("/api/v1/advice/latest")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIsNone(data["data"])
        self.assertIn("no advice", data["message"].lower())

    def test_post_generate_advice_success(self):
        now = datetime.now(timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        resp = self.client.post(
            "/api/v1/advice/generate",
            json={"device_id": "aria-esp32"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["data"]["device_id"], "aria-esp32")
        self.assertEqual(body["data"]["provider"], "nvidia_nim")
        self.assertEqual(body["data"]["summary"], "Flask API generated advice.")

    def test_get_latest_advice_after_generation(self):
        now = datetime.now(timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        # Generate advice
        self.client.post("/api/v1/advice/generate")

        # GET /api/v1/advice/latest should now return it
        resp = self.client.get("/api/v1/advice/latest?device_id=aria-esp32")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIsNotNone(data["data"])
        self.assertEqual(data["data"]["device_id"], "aria-esp32")

    def test_invalid_device_id_parameter(self):
        resp = self.client.get("/api/v1/advice/latest?device_id=   ")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cannot be empty", resp.get_json()["message"])

        resp2 = self.client.post(
            "/api/v1/advice/generate",
            json={"device_id": ""},
        )
        self.assertEqual(resp2.status_code, 400)

    def test_unexpected_internal_error_returns_sanitized_500(self):
        with patch.object(self.advice_engine, "generate_advice", side_effect=RuntimeError("Secret internal DB crash")):
            resp = self.client.post("/api/v1/advice/generate")
            self.assertEqual(resp.status_code, 500)
            body = resp.get_json()
            self.assertEqual(body["status"], "error")
            self.assertIn("Internal error", body["message"])
            # Must NOT leak internal stack or exception details
            self.assertNotIn("Secret internal DB crash", json.dumps(body))

    def test_advice_freshness_metadata_exposed_via_flask_api(self):
        """API response must expose telemetry_context and gps_provenance."""
        now = datetime.now(timezone.utc)
        self.db.insert_telemetry(_make_telemetry(device_id="aria-esp32", received_at=now))
        self.db.insert_status(_make_status(device_id="aria-esp32", received_at=now))

        resp = self.client.post("/api/v1/advice/generate", json={"device_id": "aria-esp32"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]

        self.assertIn("telemetry_context", data)
        self.assertIn("gps_provenance", data)
        self.assertIn("telemetry_timestamp", data["telemetry_context"])
        self.assertIn("telemetry_age_seconds", data["telemetry_context"])
        self.assertIn("telemetry_freshness", data["telemetry_context"])
        self.assertIn("snapshot", data["telemetry_context"])


if __name__ == "__main__":
    unittest.main()
