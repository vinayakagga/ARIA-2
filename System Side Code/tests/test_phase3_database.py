"""Phase 3 verification tests for ARIA SQLite persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from server.database import MAX_QUERY_LIMIT, Database
from server.mqtt_client import ARIAClient
from server.telemetry import GPSData, StatusMessage, TelemetryMessage

MOSQUITTO_PUB = r"C:\Program Files\mosquitto\mosquitto_pub.exe"


class TestPhase3Database(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_aria.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        time.sleep(0.05)
        self.temp_dir.cleanup()

    def test_schema_initialization_and_idempotence(self):
        """Verify schema tables, indexes and idempotency."""
        self.db.initialize()
        # Call initialize second time to verify idempotency
        self.db.initialize()

        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        self.assertIn("schema_migrations", tables)
        self.assertIn("telemetry", tables)
        self.assertIn("device_status", tables)

        cur.execute("SELECT version FROM schema_migrations")
        versions = [row[0] for row in cur.fetchall()]
        self.assertEqual(versions, [1])

    def test_insert_and_query_full_telemetry(self):
        """Verify insertion and retrieval of full telemetry record."""
        t_device = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        t_recv = datetime(2026, 11, 15, 2, 0, 1, 123456, tzinfo=timezone.utc)
        gps = GPSData(
            fix=True,
            latitude=28.6139,
            longitude=77.2090,
            satellites=9,
            altitude_m=215.4,
            speed_mps=0.3,
        )
        msg = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=t_recv,
            timestamp=t_device,
            temperature_c=28.5,
            humidity_pct=61.2,
            light_lux=350.0,
            mq135_raw=1230,
            gps=gps,
            raw_payload='{"test":"payload"}',
        )

        row_id = self.db.insert_telemetry(msg)
        self.assertGreater(row_id, 0)

        latest = self.db.get_latest_telemetry("aria-c3")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.device_id, "aria-c3")
        self.assertEqual(latest.source, "simulated")
        self.assertEqual(latest.received_at, t_recv)
        self.assertEqual(latest.timestamp, t_device)
        self.assertAlmostEqual(latest.temperature_c, 28.5)
        self.assertAlmostEqual(latest.humidity_pct, 61.2)
        self.assertAlmostEqual(latest.light_lux, 350.0)
        self.assertEqual(latest.mq135_raw, 1230)
        self.assertIsNotNone(latest.gps)
        self.assertTrue(latest.gps.fix)
        self.assertAlmostEqual(latest.gps.latitude, 28.6139)
        self.assertAlmostEqual(latest.gps.longitude, 77.2090)
        self.assertEqual(latest.gps.satellites, 9)
        self.assertAlmostEqual(latest.gps.altitude_m, 215.4)
        self.assertAlmostEqual(latest.gps.speed_mps, 0.3)
        self.assertEqual(latest.raw_payload, '{"test":"payload"}')

    def test_insert_and_query_null_sensor_fields(self):
        """Verify null/unavailable sensor fields remain None."""
        t_recv = datetime(2026, 11, 15, 2, 5, 0, tzinfo=timezone.utc)
        gps = GPSData(fix=False, latitude=None, longitude=None)
        msg = TelemetryMessage(
            schema_version=1,
            device_id="aria-devkit",
            source="real",
            received_at=t_recv,
            timestamp=None,  # device has no RTC
            temperature_c=None,
            humidity_pct=None,
            light_lux=None,
            mq135_raw=None,
            gps=gps,
            raw_payload="{}",
        )
        self.db.insert_telemetry(msg)

        latest = self.db.get_latest_telemetry("aria-devkit")
        self.assertIsNotNone(latest)
        self.assertIsNone(latest.timestamp)
        self.assertIsNone(latest.temperature_c)
        self.assertIsNone(latest.humidity_pct)
        self.assertIsNone(latest.light_lux)
        self.assertIsNone(latest.mq135_raw)
        self.assertIsNotNone(latest.gps)
        self.assertFalse(latest.gps.fix)
        self.assertIsNone(latest.gps.latitude)
        self.assertIsNone(latest.gps.longitude)

    def test_separate_received_at_and_device_timestamp(self):
        """Verify server received_at and device timestamp are tracked independently."""
        t_device = datetime(2026, 11, 15, 1, 0, 0, tzinfo=timezone.utc)
        t_recv = datetime(2026, 11, 15, 2, 30, 0, tzinfo=timezone.utc)

        msg = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=t_recv,
            timestamp=t_device,
        )
        self.db.insert_telemetry(msg)

        latest = self.db.get_latest_telemetry("aria-c3")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.timestamp, t_device)
        self.assertEqual(latest.received_at, t_recv)
        self.assertNotEqual(latest.timestamp, latest.received_at)

    def test_duplicate_and_out_of_order_timestamps(self):
        """Verify deterministic ordering when device timestamps are duplicate or out-of-order."""
        base_recv = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)

        # 1. Message arrived first, device time 10:05
        m1 = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=base_recv,
            timestamp=datetime(2026, 11, 15, 10, 5, 0, tzinfo=timezone.utc),
            temperature_c=20.0,
        )
        # 2. Message arrived second, but carries older device time 10:01 (out of order)
        m2 = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=base_recv + timedelta(seconds=1),
            timestamp=datetime(2026, 11, 15, 10, 1, 0, tzinfo=timezone.utc),
            temperature_c=21.0,
        )
        # 3. Message arrived third, carries duplicate device time 10:05
        m3 = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=base_recv + timedelta(seconds=2),
            timestamp=datetime(2026, 11, 15, 10, 5, 0, tzinfo=timezone.utc),
            temperature_c=22.0,
        )

        self.db.insert_telemetry(m1)
        self.db.insert_telemetry(m2)
        self.db.insert_telemetry(m3)

        self.assertEqual(self.db.count_telemetry("aria-c3"), 3)

        # Ordered by server received_at DESC: m3, then m2, then m1
        history = self.db.get_telemetry_history("aria-c3", limit=10)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].temperature_c, 22.0)  # m3
        self.assertEqual(history[1].temperature_c, 21.0)  # m2
        self.assertEqual(history[2].temperature_c, 20.0)  # m1

    def test_bounded_history_queries(self):
        """Verify bounded query limits and filters."""
        base = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(25):
            msg = TelemetryMessage(
                schema_version=1,
                device_id="aria-c3",
                source="simulated",
                received_at=base + timedelta(seconds=i),
                temperature_c=20.0 + i,
            )
            self.db.insert_telemetry(msg)

        # Default / bounded query limit
        limited = self.db.get_telemetry_history("aria-c3", limit=5)
        self.assertEqual(len(limited), 5)
        # Most recent first
        self.assertEqual(limited[0].temperature_c, 44.0)

        # Cap limit at MAX_QUERY_LIMIT
        all_recs = self.db.get_telemetry_history("aria-c3", limit=5000)
        self.assertEqual(len(all_recs), 25)

        # Filter by since
        since_time = base + timedelta(seconds=20)
        since_recs = self.db.get_telemetry_history("aria-c3", since=since_time)
        self.assertEqual(len(since_recs), 5)  # indices 20, 21, 22, 23, 24

    def test_status_insert_and_query(self):
        """Verify status table insertion and retrieval."""
        t_recv1 = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        s1 = StatusMessage(
            schema_version=1,
            device_id="aria-c3",
            status="online",
            received_at=t_recv1,
            ip_address="192.168.1.100",
            wifi_rssi_dbm=-62,
            uptime_s=100,
        )
        self.db.insert_status(s1)

        t_recv2 = datetime(2026, 11, 15, 3, 0, 0, tzinfo=timezone.utc)
        s2 = StatusMessage(
            schema_version=1,
            device_id="aria-c3",
            status="offline",
            received_at=t_recv2,
            timestamp=None,
        )
        self.db.insert_status(s2)

        latest = self.db.get_latest_status("aria-c3")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "offline")
        self.assertEqual(latest.received_at, t_recv2)

    def test_database_restart_persistence(self):
        """Verify records persist across database connection close and restart."""
        t_recv = datetime(2026, 11, 15, 2, 0, 0, tzinfo=timezone.utc)
        msg = TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="simulated",
            received_at=t_recv,
            temperature_c=26.8,
        )
        self.db.insert_telemetry(msg)

        # Close database connection
        self.db.close()

        # Reopen with new Database instance on same file path
        restarted_db = Database(self.db_path)
        try:
            latest = restarted_db.get_latest_telemetry("aria-c3")
            self.assertIsNotNone(latest)
            self.assertEqual(latest.device_id, "aria-c3")
            self.assertAlmostEqual(latest.temperature_c, 26.8)
            self.assertEqual(latest.received_at, t_recv)
        finally:
            restarted_db.close()

    def test_end_to_end_mqtt_to_database(self):
        """Verify MQTT client automatically writes validated messages to database."""
        client = ARIAClient(
            host="127.0.0.1",
            port=1883,
            client_id="aria-db-integration-test",
            on_telemetry=self.db.insert_telemetry,
            on_status=self.db.insert_status,
        )

        client.start()
        time.sleep(1.0)
        self.assertTrue(client.is_connected)

        try:
            telem_payload = {
                "schema_version": 1,
                "device_id": "aria-test-db",
                "source": "simulated",
                "temperature_c": 27.3,
                "humidity_pct": 58.0,
            }
            subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-t", "aria/v1/telemetry", "-m", json.dumps(telem_payload)],
                check=True,
                timeout=5,
            )

            status_payload = {
                "schema_version": 1,
                "device_id": "aria-test-db",
                "status": "online",
                "uptime_s": 500,
            }
            subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-t", "aria/v1/status", "-m", json.dumps(status_payload)],
                check=True,
                timeout=5,
            )

            # Wait for dispatch and insert
            time.sleep(1.0)

            latest_telem = self.db.get_latest_telemetry("aria-test-db")
            self.assertIsNotNone(latest_telem)
            self.assertAlmostEqual(latest_telem.temperature_c, 27.3)
            self.assertAlmostEqual(latest_telem.humidity_pct, 58.0)

            latest_status = self.db.get_latest_status("aria-test-db")
            self.assertIsNotNone(latest_status)
            self.assertEqual(latest_status.status, "online")
            self.assertEqual(latest_status.uptime_s, 500)

        finally:
            client.stop()


if __name__ == "__main__":
    unittest.main()
