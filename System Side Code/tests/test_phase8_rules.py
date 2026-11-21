"""Unit and integration tests for ARIA Deterministic Rules Engine (Phase 1).

Covers:
1. Three-tier advice mode classification (FULL_CONTEXT, LIMITED_CONTEXT, GENERAL_GUIDANCE).
2. Independent sensor evaluation (each sensor missing individually, multiple missing, all missing).
3. Distinction between missing sensor reading, stale telemetry, and device disconnection.
4. Sensor health classification vs environmental agricultural observations.
5. Configurable monitoring threshold triggers (temperature, humidity, light, MQ-135).
6. Strict agricultural guardrails (no CO2/ppm claims, no soil moisture inferences, unverified HDOP disclaimer).
7. Deterministic guidance fallback generator when AI is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from server.rules import (
    AdviceMode,
    MonitoringThresholds,
    RulesEvaluationResult,
    SensorState,
    Severity,
    evaluate_rules,
    generate_deterministic_fallback_guidance,
)
from server.telemetry import GPSData, TelemetryMessage


class TestPhase8RulesEngine(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
        self.thresholds = MonitoringThresholds()

        # Baseline sample telemetry
        self.full_telemetry = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=2),  # Fresh (< 15s)
            timestamp=self.now - timedelta(seconds=2),
            temperature_c=25.0,
            humidity_pct=55.0,
            light_lux=15000.0,
            mq135_raw=1200,
            gps=GPSData(fix=True, latitude=28.6139, longitude=77.2090, satellites=8),
        )

        # Baseline sample weather
        self.valid_weather = {
            "status": "available",
            "provider": "open-meteo",
            "current": {
                "temperature_2m": 24.5,
                "relative_humidity_2m": 54.0,
                "precipitation": 0.0,
                "wind_speed_10m": 12.0,
            },
            "hourly": {
                "precipitation_probability": [10, 15, 20, 20, 10, 5],
            },
        }

    # =========================================================================
    # 1. Advice Mode Classification Tests
    # =========================================================================

    def test_full_context_mode(self):
        """Fresh telemetry, at least 2 physical sensors, and valid weather -> FULL_CONTEXT."""
        res = evaluate_rules(
            telemetry=self.full_telemetry,
            device_online=True,
            weather=self.valid_weather,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.FULL_CONTEXT)
        self.assertIn("temperature_c", res.available_inputs)
        self.assertIn("humidity_pct", res.available_inputs)
        self.assertIn("weather", res.available_inputs)

    def test_limited_context_when_weather_unavailable(self):
        """Fresh telemetry with sensors but no weather -> LIMITED_CONTEXT."""
        res = evaluate_rules(
            telemetry=self.full_telemetry,
            device_online=True,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.LIMITED_CONTEXT)
        self.assertIn("weather", res.unavailable_inputs)

    def test_limited_context_when_telemetry_is_stale(self):
        """Sensors and weather available, but telemetry is stale (>15s) -> LIMITED_CONTEXT."""
        stale_telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=45),  # 45s old
            temperature_c=25.0,
            humidity_pct=55.0,
        )
        res = evaluate_rules(
            telemetry=stale_telem,
            device_online=True,
            weather=self.valid_weather,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.LIMITED_CONTEXT)
        self.assertTrue(any(o.rule_id == "SYS_TELEMETRY_STALE" for o in res.observations))

    def test_limited_context_with_single_sensor_only(self):
        """Only one physical sensor reading available -> LIMITED_CONTEXT."""
        single_sensor_telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=2),
            temperature_c=22.0,
            humidity_pct=None,
            light_lux=None,
            mq135_raw=None,
        )
        res = evaluate_rules(
            telemetry=single_sensor_telem,
            device_online=True,
            weather=self.valid_weather,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.LIMITED_CONTEXT)

    def test_limited_context_with_weather_only(self):
        """No physical sensors at all, but valid weather exists -> LIMITED_CONTEXT."""
        empty_telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=2),
            temperature_c=None,
            humidity_pct=None,
            light_lux=None,
            mq135_raw=None,
        )
        res = evaluate_rules(
            telemetry=empty_telem,
            device_online=True,
            weather=self.valid_weather,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.LIMITED_CONTEXT)
        self.assertIn("weather", res.available_inputs)

    def test_general_guidance_mode_when_no_sensors_and_no_weather(self):
        """No telemetry and no weather -> GENERAL_GUIDANCE."""
        res = evaluate_rules(
            telemetry=None,
            device_online=False,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(res.advice_mode, AdviceMode.GENERAL_GUIDANCE)
        self.assertEqual(len(res.available_inputs), 0)

    # =========================================================================
    # 2. Distinction: Missing Reading vs Stale vs Device Disconnection
    # =========================================================================

    def test_device_offline_not_marked_as_sensor_fault(self):
        """When rover is offline and no telemetry exists, status is DEVICE_OFFLINE, not faulty."""
        res = evaluate_rules(
            telemetry=None,
            device_online=False,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        dht_health = next(h for h in res.sensor_health if h.sensor == "temperature_c")
        self.assertEqual(dht_health.state, SensorState.DEVICE_OFFLINE)
        self.assertIn("offline", dht_health.detail.lower())

    def test_unresponsive_null_sensor_on_live_telemetry(self):
        """When rover is online and sends a payload with null temperature, it is UNRESPONSIVE_NULL."""
        telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=1),
            temperature_c=None,  # Null sensor reading
            humidity_pct=60.0,
        )
        res = evaluate_rules(
            telemetry=telem,
            device_online=True,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        temp_health = next(h for h in res.sensor_health if h.sensor == "temperature_c")
        hum_health = next(h for h in res.sensor_health if h.sensor == "humidity_pct")
        self.assertEqual(temp_health.state, SensorState.UNRESPONSIVE_NULL)
        self.assertEqual(hum_health.state, SensorState.HEALTHY)

    def test_stale_telemetry_sensor_state(self):
        """When telemetry is older than threshold, valid values are marked STALE."""
        telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=30),  # Stale
            temperature_c=25.0,
            humidity_pct=None,
        )
        res = evaluate_rules(
            telemetry=telem,
            device_online=True,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        temp_health = next(h for h in res.sensor_health if h.sensor == "temperature_c")
        hum_health = next(h for h in res.sensor_health if h.sensor == "humidity_pct")
        self.assertEqual(temp_health.state, SensorState.STALE)
        self.assertEqual(hum_health.state, SensorState.UNRESPONSIVE_NULL)

    # =========================================================================
    # 3. Independent Sensor Evaluation
    # =========================================================================

    def test_each_sensor_missing_individually(self):
        """Verify each sensor can be missing without invalidating other available sensors."""
        sensors = ["temperature_c", "humidity_pct", "light_lux", "mq135_raw"]
        for missing in sensors:
            kwargs = {
                "temperature_c": 25.0,
                "humidity_pct": 50.0,
                "light_lux": 10000.0,
                "mq135_raw": 1200,
            }
            kwargs[missing] = None
            telem = TelemetryMessage(
                schema_version=1,
                device_id="aria-c3",
                source="real",
                received_at=self.now - timedelta(seconds=1),
                **kwargs,
            )
            res = evaluate_rules(
                telemetry=telem,
                device_online=True,
                weather=None,
                thresholds=self.thresholds,
                current_time=self.now,
            )
            self.assertIn(missing, res.unavailable_inputs)
            for active in sensors:
                if active != missing:
                    self.assertIn(active, res.available_inputs)

    def test_all_sensors_missing(self):
        """All sensors null in fresh telemetry is safely evaluated."""
        telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now - timedelta(seconds=1),
            temperature_c=None,
            humidity_pct=None,
            light_lux=None,
            mq135_raw=None,
            gps=None,
        )
        res = evaluate_rules(
            telemetry=telem,
            device_online=True,
            weather=None,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        self.assertEqual(len(res.available_inputs), 0)
        self.assertEqual(res.advice_mode, AdviceMode.GENERAL_GUIDANCE)
        self.assertTrue(any("temperature sensor is unavailable" in lim for lim in res.limitations))

    # =========================================================================
    # 4. Configurable Monitoring Thresholds & Observations
    # =========================================================================

    def test_temperature_thresholds(self):
        """High heat, frost watch, and nominal ambient temperature observations."""
        # High heat (> 35°C)
        t_high = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=38.5)
        res1 = evaluate_rules(t_high, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_TEMP_HIGH" for o in res1.observations))

        # Frost watch (< 4°C)
        t_low = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=2.1)
        res2 = evaluate_rules(t_low, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_TEMP_LOW" for o in res2.observations))

        # Nominal (15-28°C)
        t_nom = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=22.0)
        res3 = evaluate_rules(t_nom, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_TEMP_NOMINAL" for o in res3.observations))

    def test_custom_threshold_override(self):
        """Custom threshold parameters override defaults cleanly."""
        custom_thresholds = MonitoringThresholds(temp_high_c=42.0)
        t = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=38.0)

        # Default trigger (35°C) would flag ENV_TEMP_HIGH
        res_default = evaluate_rules(t, thresholds=MonitoringThresholds(), current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_TEMP_HIGH" for o in res_default.observations))

        # Custom trigger (42°C) flags ENV_TEMP_NOMINAL
        res_custom = evaluate_rules(t, thresholds=custom_thresholds, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_TEMP_NOMINAL" for o in res_custom.observations))

    def test_humidity_thresholds(self):
        """High humidity and low humidity observations."""
        t_wet = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, humidity_pct=92.0)
        res1 = evaluate_rules(t_wet, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_HUM_HIGH" for o in res1.observations))

        t_dry = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, humidity_pct=22.0)
        res2 = evaluate_rules(t_dry, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_HUM_LOW" for o in res2.observations))

    def test_ambient_light_thresholds(self):
        """High solar illuminance and low daylight illuminance."""
        t_bright = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, light_lux=75000.0)
        res1 = evaluate_rules(t_bright, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_LIGHT_HIGH" for o in res1.observations))

        t_dim = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, light_lux=2000.0)
        res2 = evaluate_rules(t_dim, current_time=self.now)
        self.assertTrue(any(o.rule_id == "ENV_LIGHT_LOW" for o in res2.observations))

    # =========================================================================
    # 5. Agricultural Scope & Strict Sensor Guardrails
    # =========================================================================

    def test_mq135_raw_adc_strict_guardrail(self):
        """MQ-135 reading is strictly uncalibrated raw ADC; never CO2, ppm, or calibrated score."""
        t_elevated = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, mq135_raw=3100)
        res = evaluate_rules(t_elevated, current_time=self.now)
        obs = next(o for o in res.observations if o.rule_id == "ENV_MQ135_ELEVATED")

        self.assertIn("raw ADC", obs.explanation)
        self.assertIn("Does NOT represent calibrated air quality index, CO2 ppm", obs.explanation)
        self.assertNotIn("CO2 ppm", obs.title)
        self.assertNotIn("Air Quality Index:", obs.title)

    def test_mq135_baseline_and_elevated_never_imply_calibrated_air_quality(self):
        """MQ-135 wording (both baseline and elevated) must state raw ADC cannot establish air quality or pollution."""
        t_base = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, mq135_raw=150)
        res_base = evaluate_rules(t_base, current_time=self.now)
        obs_base = next(o for o in res_base.observations if o.rule_id == "ENV_MQ135_BASELINE")
        self.assertIn("cannot establish normal air quality or a pollution level without calibration", obs_base.explanation)

        t_elev = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, mq135_raw=3200)
        res_elev = evaluate_rules(t_elev, current_time=self.now)
        obs_elev = next(o for o in res_elev.observations if o.rule_id == "ENV_MQ135_ELEVATED")
        self.assertIn("cannot establish a pollution level without calibration", obs_elev.explanation)

    def test_humidity_does_not_infer_soil_moisture(self):
        """Relative humidity rule explicitly states it reflects air moisture only, not soil moisture."""
        t_hum = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, humidity_pct=88.0)
        res = evaluate_rules(t_hum, current_time=self.now)
        obs = next(o for o in res.observations if o.rule_id == "ENV_HUM_HIGH")
        self.assertIn("Strictly reflects air humidity, NOT soil moisture", obs.explanation)

    def test_temperature_does_not_infer_crop_canopy(self):
        """Temperature rule explicitly notes it reflects ambient air, not crop canopy temperature."""
        t_temp = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=37.0)
        res = evaluate_rules(t_temp, current_time=self.now)
        obs = next(o for o in res.observations if o.rule_id == "ENV_TEMP_HIGH")
        self.assertIn("Reflects ambient air, not crop canopy temperature", obs.explanation)

    def test_gps_hdop_unverified_disclaimer(self):
        """GPS satellite lock records explicit disclaimer that horizontal precision is unverified."""
        telem = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=self.now,
            gps=GPSData(fix=True, latitude=28.6, longitude=77.2, satellites=7),
        )
        res = evaluate_rules(telem, current_time=self.now)
        self.assertTrue(any("HDOP" in lim for lim in res.limitations))

    # =========================================================================
    # 6. Weather Forecast Cross-Checks
    # =========================================================================

    def test_weather_microclimate_temperature_delta(self):
        """Discrepancy between rover ambient temp and regional weather forecast is flagged."""
        telem = TelemetryMessage(schema_version=1, device_id="c3", source="r", received_at=self.now, temperature_c=32.0)
        weather = {"status": "available", "current": {"temperature_2m": 24.0}}  # Delta = 8°C >= 5°C
        res = evaluate_rules(telem, weather=weather, current_time=self.now)
        self.assertTrue(any(o.rule_id == "WTH_TEMP_MICROCLIMATE_DELTA" for o in res.observations))

    def test_weather_active_precipitation_and_high_probability(self):
        """Active rain and elevated probability (>60%) trigger weather warnings."""
        weather = {
            "status": "available",
            "current": {"precipitation": 2.5, "wind_speed_10m": 10.0},
            "hourly": {"precipitation_probability": [75, 80, 65, 40, 20, 10]},
        }
        res = evaluate_rules(self.full_telemetry, weather=weather, current_time=self.now)
        self.assertTrue(any(o.rule_id == "WTH_PRECIPITATION_ACTIVE" for o in res.observations))
        self.assertTrue(any(o.rule_id == "WTH_PRECIP_PROB_HIGH" for o in res.observations))

    # =========================================================================
    # 7. Deterministic Guidance Fallback Generator
    # =========================================================================

    def test_fallback_guidance_generation(self):
        """Fallback generator provides structured observations and general stewardship without AI."""
        res = evaluate_rules(
            telemetry=self.full_telemetry,
            device_online=True,
            weather=self.valid_weather,
            thresholds=self.thresholds,
            current_time=self.now,
        )
        fallback = generate_deterministic_fallback_guidance(res)

        self.assertEqual(fallback["status"], "available")
        self.assertEqual(fallback["provider"], "deterministic_fallback")
        self.assertIn("summary", fallback)
        self.assertIsInstance(fallback["observations"], list)
        self.assertIsInstance(fallback["recommendations"], list)
        self.assertGreaterEqual(len(fallback["recommendations"]), 1)

        # Must not fabricate crop-specific, irrigation, or fertilizer advice
        rec_text = " ".join([r["title"] + " " + r["explanation"] for r in fallback["recommendations"]]).lower()
        self.assertNotIn("irrigate", rec_text)
        self.assertNotIn("fertilize", rec_text)
        self.assertNotIn("pesticide", rec_text)
        self.assertNotIn("wheat", rec_text)
        self.assertNotIn("corn", rec_text)

    def test_fallback_guidance_in_general_guidance_mode(self):
        """Fallback guidance in GENERAL_GUIDANCE mode explicitly disclaims rover measurement absence."""
        res = evaluate_rules(telemetry=None, device_online=False, weather=None, current_time=self.now)
        fallback = generate_deterministic_fallback_guidance(res)

        self.assertIn("No live ARIA physical sensors", fallback["summary"])
        self.assertTrue(any("extension service" in r["explanation"].lower() or "visual" in r["explanation"].lower() for r in fallback["recommendations"]))


if __name__ == "__main__":
    unittest.main()
