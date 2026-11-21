"""Phase 4 verification tests for ARIA Flask REST API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from server.app import create_app
from server.database import Database
from server.telemetry import GPSData, StatusMessage, TelemetryMessage


class TestPhase4FlaskAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api.db"
        self.db = Database(self.db_path)
        self.db.initialize()
        # Disable MQTT for API tests: no broker required, no worker threads.
        self.app = create_app(db=self.db, start_mqtt=False)
        self.client = self.app.test_client()

    def tearDown(self):
        # Stop MQTT client (no-op when start_mqtt=False, but safe to call).
        self.app.stop_mqtt()
        self.db.close()
        time.sleep(0.1)
        self.temp_dir.cleanup()

    def test_health_check_success(self):
        resp = self.client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["database"], "connected")
        self.assertIn("timestamp", data)

    def test_health_check_degraded_when_db_fails(self):
        mock_db = MagicMock()
        mock_db.count_telemetry.side_effect = RuntimeError("DB connection lost")
        # Disable MQTT so no worker thread is created for this transient app.
        app = create_app(db=mock_db, start_mqtt=False)
        client = app.test_client()

        resp = client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["database"], "disconnected")
        self.assertIn("DB connection lost", data["error"])

    def test_telemetry_latest_empty_database(self):
        resp = self.client.get("/api/v1/telemetry/latest")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIsNone(data["data"])
        self.assertIn("no telemetry recorded yet", data["message"])

    def test_telemetry_latest_success(self):
        t_recv = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        t_dev = datetime(2026, 11, 15, 1, 59, 59, tzinfo=timezone.utc)
        gps = GPSData(fix=True, latitude=28.6139, longitude=77.2090, satellites=7, altitude_m=210.0, speed_mps=0.5)
        msg = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=t_recv,
            timestamp=t_dev,
            temperature_c=27.5,
            humidity_pct=60.0,
            light_lux=320.0,
            mq135_raw=1200,
            gps=gps,
        )
        self.db.insert_telemetry(msg)

        resp = self.client.get("/api/v1/telemetry/latest")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        telem = data["data"]
        self.assertEqual(telem["device_id"], "aria-c3")
        self.assertEqual(telem["source"], "simulated")
        self.assertEqual(telem["temperature_c"], 27.5)
        self.assertEqual(telem["humidity_pct"], 60.0)
        self.assertEqual(telem["light_lux"], 320.0)
        self.assertEqual(telem["mq135_raw"], 1200)
        self.assertNotIn("distance_cm", telem)
        self.assertTrue(telem["gps"]["fix"])
        self.assertAlmostEqual(telem["gps"]["latitude"], 28.6139)
        self.assertAlmostEqual(telem["gps"]["longitude"], 77.2090)

    def test_telemetry_latest_filter_by_device_id(self):
        t_recv = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        self.db.insert_telemetry(TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=t_recv,
            temperature_c=25.0,
        ))
        self.db.insert_telemetry(TelemetryMessage(
            schema_version=1,
            device_id="aria-devkit",
            source="real",
            received_at=t_recv + timedelta(seconds=1),
            temperature_c=30.0,
        ))

        # Query aria-c3
        resp1 = self.client.get("/api/v1/telemetry/latest?device_id=aria-c3")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.get_json()["data"]["device_id"], "aria-c3")
        self.assertEqual(resp1.get_json()["data"]["temperature_c"], 25.0)

        # Query aria-devkit
        resp2 = self.client.get("/api/v1/telemetry/latest?device_id=aria-devkit")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()["data"]["device_id"], "aria-devkit")
        self.assertEqual(resp2.get_json()["data"]["temperature_c"], 30.0)

        # Query non-existent device
        resp3 = self.client.get("/api/v1/telemetry/latest?device_id=unknown-device")
        self.assertEqual(resp3.status_code, 404)
        self.assertEqual(resp3.get_json()["status"], "error")
        self.assertIn("no telemetry found for device: unknown-device", resp3.get_json()["message"])

        # Query empty device_id
        resp4 = self.client.get("/api/v1/telemetry/latest?device_id=   ")
        self.assertEqual(resp4.status_code, 400)
        self.assertEqual(resp4.get_json()["status"], "error")

    def test_telemetry_history_empty_database(self):
        resp = self.client.get("/api/v1/telemetry/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["data"], [])

    def test_telemetry_history_success_and_ordering(self):
        base = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            self.db.insert_telemetry(TelemetryMessage(
                schema_version=1,
                device_id="aria-c3",
                source="simulated",
                received_at=base + timedelta(seconds=i),
                temperature_c=20.0 + i,
            ))

        resp = self.client.get("/api/v1/telemetry/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["count"], 5)
        # Verify descending order (most recent first)
        self.assertEqual(data["data"][0]["temperature_c"], 24.0)
        self.assertEqual(data["data"][-1]["temperature_c"], 20.0)

    def test_telemetry_history_limit_parameter(self):
        base = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            self.db.insert_telemetry(TelemetryMessage(
                schema_version=1,
                device_id="aria-c3",
                source="simulated",
                received_at=base + timedelta(seconds=i),
                temperature_c=20.0 + i,
            ))

        # Valid limit=3
        resp1 = self.client.get("/api/v1/telemetry/history?limit=3")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.get_json()["count"], 3)
        self.assertEqual(len(resp1.get_json()["data"]), 3)

        # Invalid limit string
        resp2 = self.client.get("/api/v1/telemetry/history?limit=notanumber")
        self.assertEqual(resp2.status_code, 400)
        self.assertEqual(resp2.get_json()["status"], "error")

        # Limit zero
        resp3 = self.client.get("/api/v1/telemetry/history?limit=0")
        self.assertEqual(resp3.status_code, 400)
        self.assertEqual(resp3.get_json()["status"], "error")

        # Limit exceeds 1000
        resp4 = self.client.get("/api/v1/telemetry/history?limit=1001")
        self.assertEqual(resp4.status_code, 400)
        self.assertEqual(resp4.get_json()["status"], "error")

    def test_telemetry_history_since_parameter(self):
        base = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            self.db.insert_telemetry(TelemetryMessage(
                schema_version=1,
                device_id="aria-c3",
                source="simulated",
                received_at=base + timedelta(seconds=i * 10),
                temperature_c=20.0 + i,
            ))

        # Query since 2026-11-15T02:00:20Z (should include records 2, 3, 4)
        since_str = (base + timedelta(seconds=20)).isoformat()
        resp1 = self.client.get(f"/api/v1/telemetry/history?since={since_str}")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.get_json()["count"], 3)

        # Invalid since string
        resp2 = self.client.get("/api/v1/telemetry/history?since=invalid-date")
        self.assertEqual(resp2.status_code, 400)
        self.assertEqual(resp2.get_json()["status"], "error")

    def test_status_latest_empty_database(self):
        resp = self.client.get("/api/v1/status/latest")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIsNone(data["data"])
        self.assertIn("no status recorded yet", data["message"])

    def test_status_latest_success(self):
        t_recv = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        self.db.insert_status(StatusMessage(
            schema_version=1,
            device_id="aria-c3",
            status="online",
            received_at=t_recv,
            ip_address="192.168.1.110",
            wifi_rssi_dbm=-60,
            uptime_s=250,
        ))

        resp = self.client.get("/api/v1/status/latest")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")
        st = data["data"]
        self.assertEqual(st["device_id"], "aria-c3")
        self.assertEqual(st["status"], "online")
        self.assertEqual(st["ip_address"], "192.168.1.110")
        self.assertEqual(st["wifi_rssi_dbm"], -60)
        self.assertEqual(st["uptime_s"], 250)

        # Query non-existent device
        resp_missing = self.client.get("/api/v1/status/latest?device_id=ghost")
        self.assertEqual(resp_missing.status_code, 404)
        self.assertEqual(resp_missing.get_json()["status"], "error")

    def test_method_not_allowed(self):
        resp = self.client.post("/api/v1/telemetry/latest", json={"command": "stop"})
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp.get_json()["status"], "error")
        self.assertEqual(resp.get_json()["message"], "Method not allowed")

    def test_not_found_endpoint(self):
        resp = self.client.get("/api/v1/nonexistent")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["status"], "error")


if __name__ == "__main__":
    unittest.main()
