"""Phase 2 verification tests for ARIA MQTT contract and backend foundation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
import unittest

from server.mqtt_client import ARIAClient
from server.telemetry import (
    GPSData,
    StatusMessage,
    TelemetryMessage,
    validate_status_payload,
    validate_telemetry_payload,
)

MOSQUITTO_PUB = r"C:\Program Files\mosquitto\mosquitto_pub.exe"


class TestPhase2TelemetryContract(unittest.TestCase):
    def test_valid_full_telemetry(self):
        payload = {
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "timestamp": "2026-11-15T02:00:00Z",
            "temperature_c": 28.4,
            "humidity_pct": 62.0,
            "light_lux": 340.0,
            "mq135_raw": 1250,
            "distance_cm": 85.0,
            "gps": {
                "fix": True,
                "latitude": 28.6139,
                "longitude": 77.2090,
                "satellites": 8,
                "altitude_m": 216.5,
                "speed_mps": 0.4,
            },
        }
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertTrue(res.is_valid, f"Validation failed: {res.errors}")
        self.assertIsNotNone(res.data)
        data = res.data
        self.assertEqual(data.schema_version, 1)
        self.assertEqual(data.device_id, "aria-c3")
        self.assertEqual(data.source, "simulated")
        self.assertEqual(data.temperature_c, 28.4)
        self.assertEqual(data.humidity_pct, 62.0)
        self.assertEqual(data.light_lux, 340.0)
        self.assertEqual(data.mq135_raw, 1250)
        self.assertIsNotNone(data.gps)
        self.assertTrue(data.gps.fix)
        self.assertAlmostEqual(data.gps.latitude, 28.6139)
        self.assertAlmostEqual(data.gps.longitude, 77.2090)
        self.assertEqual(data.gps.satellites, 8)
        self.assertAlmostEqual(data.gps.altitude_m, 216.5)
        self.assertAlmostEqual(data.gps.speed_mps, 0.4)

    def test_valid_telemetry_with_null_and_missing_sensors(self):
        """Sensors may be disconnected or unread; null or omitted values must be valid."""
        payload = {
            "schema_version": 1,
            "device_id": "aria-devkit",
            "source": "real",
            "temperature_c": None,
            "humidity_pct": None,
            "light_lux": 100.0,
            # mq135_raw completely omitted
            "gps": {
                "fix": False,
                "latitude": None,
                "longitude": None,
            },
        }
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertTrue(res.is_valid, f"Validation failed: {res.errors}")
        data = res.data
        self.assertIsNone(data.temperature_c)
        self.assertIsNone(data.humidity_pct)
        self.assertEqual(data.light_lux, 100.0)
        self.assertIsNone(data.mq135_raw)
        self.assertFalse(data.gps.fix)
        self.assertIsNone(data.gps.latitude)
        self.assertIsNone(data.gps.longitude)

    def test_malformed_json_syntax(self):
        raw = '{"schema_version": 1, "device_id": "aria-c3", unquoted}'
        res = validate_telemetry_payload(raw)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Invalid JSON" in err for err in res.errors))

    def test_missing_required_fields(self):
        cases = [
            ({}, "Missing required field: 'schema_version'"),
            ({"schema_version": 1}, "Missing required field: 'device_id'"),
            ({"schema_version": 1, "device_id": "aria-c3"}, "Missing required field: 'source'"),
        ]
        for payload, expected_error in cases:
            res = validate_telemetry_payload(json.dumps(payload))
            self.assertFalse(res.is_valid)
            self.assertTrue(any(expected_error in err for err in res.errors), f"Expected {expected_error} in {res.errors}")

    def test_unsupported_schema_version(self):
        payload = {"schema_version": 99, "device_id": "aria-c3", "source": "simulated"}
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Unsupported schema_version 99" in err for err in res.errors))

    def test_invalid_source_value(self):
        payload = {"schema_version": 1, "device_id": "aria-c3", "source": "cloud"}
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Invalid 'source' value" in err for err in res.errors))

    def test_wrong_field_types(self):
        payload = {
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "temperature_c": "28.4",  # String instead of number
            "mq135_raw": True,        # Bool instead of int
            "gps": "no fix",          # String instead of dict
        }
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertFalse(res.is_valid)
        self.assertTrue(any("temperature_c must be a number" in err for err in res.errors))
        self.assertTrue(any("mq135_raw must be an integer" in err for err in res.errors))
        self.assertTrue(any("gps must be an object" in err for err in res.errors))

    def test_invalid_gps_combinations(self):
        # Case 1: fix is true, but latitude is null
        res1 = validate_telemetry_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "gps": {"fix": True, "latitude": None, "longitude": 77.2},
        }))
        self.assertFalse(res1.is_valid)
        self.assertTrue(any("gps.latitude and gps.longitude are required numbers when gps.fix is true" in e for e in res1.errors))

        # Case 2: fix is false, but latitude is supplied
        res2 = validate_telemetry_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "gps": {"fix": False, "latitude": 28.5, "longitude": None},
        }))
        self.assertFalse(res2.is_valid)
        self.assertTrue(any("gps.latitude and gps.longitude must be null when gps.fix is false" in e for e in res2.errors))

        # Case 3: latitude out of range
        res3 = validate_telemetry_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "gps": {"fix": True, "latitude": 95.0, "longitude": 77.2},
        }))
        self.assertFalse(res3.is_valid)
        self.assertTrue(any("gps.latitude (95.0) exceeds maximum" in e for e in res3.errors))

    def test_sensor_bounds_checking(self):
        cases = [
            ({"temperature_c": 120.0}, "temperature_c (120.0) exceeds maximum"),
            ({"temperature_c": -50.0}, "temperature_c (-50.0) is below minimum"),
            ({"humidity_pct": 105.0}, "humidity_pct (105.0) exceeds maximum"),
            ({"light_lux": -5.0}, "light_lux (-5.0) is below minimum"),
            ({"mq135_raw": 5000}, "mq135_raw (5000) exceeds maximum"),
        ]
        for field_dict, expected_err in cases:
            payload = {"schema_version": 1, "device_id": "aria-c3", "source": "simulated", **field_dict}
            res = validate_telemetry_payload(json.dumps(payload))
            self.assertFalse(res.is_valid)
            self.assertTrue(any(expected_err in e for e in res.errors), f"Expected '{expected_err}' in {res.errors}")

    def test_legacy_distance_cm_safely_ignored(self):
        """HC-SR04 removed: legacy distance_cm in incoming payload is safely ignored."""
        payload = {
            "schema_version": 1,
            "device_id": "aria-c3",
            "source": "simulated",
            "distance_cm": 85.0,
        }
        res = validate_telemetry_payload(json.dumps(payload))
        self.assertTrue(res.is_valid)
        self.assertFalse(hasattr(res.data, "distance_cm"))

    def test_status_contract_validation(self):
        # Valid online status
        res1 = validate_status_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "status": "online",
            "ip_address": "192.168.1.50",
            "wifi_rssi_dbm": -55,
            "uptime_s": 1200,
            "timestamp": "2026-11-15T02:00:00Z",
        }))
        self.assertTrue(res1.is_valid, f"Status validation failed: {res1.errors}")
        self.assertEqual(res1.data.status, "online")
        self.assertEqual(res1.data.ip_address, "192.168.1.50")
        self.assertEqual(res1.data.wifi_rssi_dbm, -55)

        # Valid LWT offline status
        res2 = validate_status_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "status": "offline",
            "timestamp": None,
        }))
        self.assertTrue(res2.is_valid, f"LWT validation failed: {res2.errors}")
        self.assertEqual(res2.data.status, "offline")

        # Invalid status enum
        res3 = validate_status_payload(json.dumps({
            "schema_version": 1,
            "device_id": "aria-c3",
            "status": "rebooting",
        }))
        self.assertFalse(res3.is_valid)
        self.assertTrue(any("Invalid 'status' value" in e for e in res3.errors))


class TestPhase2DecoupledClient(unittest.TestCase):
    def test_client_integration_with_broker(self):
        """Test ARIAClient transport with live local broker.

        Validates that valid payloads reach on_telemetry/on_status,
        and invalid payloads are dispatched to on_quarantine.
        """
        received_telemetry: list[TelemetryMessage] = []
        received_status: list[StatusMessage] = []
        quarantined: list[tuple[str, bytes, list[str]]] = []

        client = ARIAClient(
            host="127.0.0.1",
            port=1883,
            client_id="aria-test-harness",
            on_telemetry=lambda msg: received_telemetry.append(msg),
            on_status=lambda msg: received_status.append(msg),
            on_quarantine=lambda topic, raw, errors: quarantined.append((topic, raw, errors)),
        )

        client.start()
        time.sleep(1.0)
        self.assertTrue(client.is_connected, "Client failed to connect to local broker")

        try:
            # 1. Publish valid telemetry via CLI
            valid_telem = {
                "schema_version": 1,
                "device_id": "aria-c3",
                "source": "simulated",
                "temperature_c": 25.5,
                "humidity_pct": 50.0,
            }
            subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-t", "aria/v1/telemetry", "-m", json.dumps(valid_telem)],
                check=True,
                timeout=5,
            )

            # 2. Publish invalid telemetry via CLI (wrong type for temperature)
            invalid_telem = {
                "schema_version": 1,
                "device_id": "aria-c3",
                "source": "simulated",
                "temperature_c": "invalid_temp",
            }
            subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-t", "aria/v1/telemetry", "-m", json.dumps(invalid_telem)],
                check=True,
                timeout=5,
            )

            # 3. Publish valid status via CLI
            valid_status = {
                "schema_version": 1,
                "device_id": "aria-c3",
                "status": "online",
                "uptime_s": 42,
            }
            subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-t", "aria/v1/status", "-m", json.dumps(valid_status)],
                check=True,
                timeout=5,
            )

            # Allow queue dispatcher to drain
            time.sleep(1.0)

            # Assertions
            matching_telem = [m for m in received_telemetry if m.temperature_c == 25.5]
            self.assertEqual(len(matching_telem), 1, f"Expected 1 matching telemetry message, got {len(matching_telem)} (total received: {len(received_telemetry)})")
            self.assertEqual(matching_telem[0].device_id, "aria-c3")
            self.assertEqual(matching_telem[0].temperature_c, 25.5)

            self.assertEqual(len(quarantined), 1, f"Expected 1 quarantined message, got {len(quarantined)}")
            self.assertEqual(quarantined[0][0], "aria/v1/telemetry")
            self.assertTrue(any("temperature_c must be a number" in err for err in quarantined[0][2]))

            matching_status = [s for s in received_status if s.uptime_s == 42]
            self.assertEqual(len(matching_status), 1, f"Expected 1 matching status message, got {len(matching_status)} (total: {len(received_status)})")
            self.assertEqual(matching_status[0].status, "online")
            self.assertEqual(matching_status[0].uptime_s, 42)

        finally:
            client.stop()


if __name__ == "__main__":
    unittest.main()
