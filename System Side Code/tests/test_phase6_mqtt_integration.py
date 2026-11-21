"""Phase 6 verification tests for Flask application and MQTT ingestion integration.

Verifies:
1. Valid telemetry received through MQTT is stored in SQLite.
2. Valid status received through MQTT is stored in SQLite.
3. Invalid telemetry is quarantined and NOT stored in SQLite.
4. Flask application startup initializes and starts the MQTT integration.
5. Shutdown stops the MQTT client and worker thread cleanly.
6. Guard prevents starting multiple MQTT clients or worker threads.

Uses in-memory SQLite databases and mocks for the MQTT broker.
Does not require a real broker running, and avoids WAL file locking on Windows.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

from server.app import create_app
from server.database import Database
from server.mqtt_client import ARIAClient


import uuid as _uuid


def _make_in_memory_db() -> Database:
    """Create a named shared-cache in-memory SQLite database for testing.

    Using a named shared-cache URI (``file:<name>?mode=memory&cache=shared``)
    instead of plain ``':memory:'`` means every thread that opens the same URI
    shares the *same* in-memory database.  This is essential because the MQTT
    worker thread must be able to INSERT rows that the test's main thread can
    then query.

    The unique name per call ensures test isolation.
    """
    name = _uuid.uuid4().hex
    uri = f"file:{name}?mode=memory&cache=shared"
    db = Database(uri)
    db.initialize()
    return db


class TestPhase6MQTTIntegration(unittest.TestCase):
    """Integration tests for MQTT ↔ Flask ↔ SQLite pipeline (Phase 6)."""

    def setUp(self):
        self.db = _make_in_memory_db()
        self._app = None  # Set by each test so tearDown can call stop_mqtt safely.

    def tearDown(self):
        # Always stop MQTT before closing the database so the worker thread
        # releases any in-flight database references before gc.
        if self._app is not None:
            try:
                self._app.stop_mqtt()
            except Exception:
                pass
            self._app = None
        self.db.close()

    # ------------------------------------------------------------------
    # Helper: build a patched app and store it in self._app
    # ------------------------------------------------------------------
    def _create_app_with_mock(self, mock_client_cls) -> "Flask":
        mock_paho = MagicMock()
        mock_paho.connect.return_value = 0
        mock_paho.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
        mock_client_cls.return_value = mock_paho

        app = create_app(db=self.db, start_mqtt=True)
        self._app = app  # Register with tearDown.
        return app, mock_paho

    # ------------------------------------------------------------------
    # Test 1 — valid telemetry persisted
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_valid_telemetry_stored_in_sqlite(self, mock_client_cls):
        """Valid telemetry received through MQTT is stored in SQLite."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)
        client = app.mqtt_client

        self.assertIsNotNone(client)
        self.assertTrue(client.is_running)

        telem_payload = {
            "schema_version": 1,
            "device_id": "aria-rover-p6",
            "source": "simulated",
            "timestamp": "2026-11-15T03:00:00Z",
            "temperature_c": 24.2,
            "humidity_pct": 55.4,
            "light_lux": 420.0,
            "mq135_raw": 1100,
            "gps": {
                "fix": False,
                "latitude": None,
                "longitude": None,
            },
        }
        msg = mqtt.MQTTMessage(topic=b"aria/v1/telemetry")
        msg.payload = json.dumps(telem_payload).encode("utf-8")

        client._handle_message(mock_paho, None, msg)
        time.sleep(0.4)  # Allow worker thread to drain queue.

        record = self.db.get_latest_telemetry("aria-rover-p6")
        self.assertIsNotNone(record, "Telemetry message was not persisted to SQLite")
        self.assertEqual(record.device_id, "aria-rover-p6")
        self.assertEqual(record.source, "simulated")
        self.assertAlmostEqual(record.temperature_c, 24.2)
        self.assertAlmostEqual(record.humidity_pct, 55.4)
        self.assertAlmostEqual(record.light_lux, 420.0)
        self.assertEqual(record.mq135_raw, 1100)
        self.assertFalse(record.gps.fix)

    # ------------------------------------------------------------------
    # Test 2 — valid status persisted
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_valid_status_stored_in_sqlite(self, mock_client_cls):
        """Valid status received through MQTT is stored in SQLite."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)
        client = app.mqtt_client

        status_payload = {
            "schema_version": 1,
            "device_id": "aria-rover-p6",
            "status": "online",
            "ip_address": "192.168.1.150",
            "wifi_rssi_dbm": -68,
            "uptime_s": 7200,
            "timestamp": "2026-11-15T03:00:00Z",
        }
        msg = mqtt.MQTTMessage(topic=b"aria/v1/status")
        msg.payload = json.dumps(status_payload).encode("utf-8")

        client._handle_message(mock_paho, None, msg)
        time.sleep(0.4)

        record = self.db.get_latest_status("aria-rover-p6")
        self.assertIsNotNone(record, "Device status was not persisted to SQLite")
        self.assertEqual(record.device_id, "aria-rover-p6")
        self.assertEqual(record.status, "online")
        self.assertEqual(record.ip_address, "192.168.1.150")
        self.assertEqual(record.wifi_rssi_dbm, -68)
        self.assertEqual(record.uptime_s, 7200)

    # ------------------------------------------------------------------
    # Test 3 — invalid telemetry quarantined, not stored
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_invalid_telemetry_quarantined_and_not_stored(self, mock_client_cls):
        """Invalid telemetry is quarantined and must NOT be stored in SQLite."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)
        client = app.mqtt_client

        quarantined_events: list[tuple] = []
        original_quarantine = client._on_quarantine

        def spy_quarantine(topic, raw, errors):
            quarantined_events.append((topic, raw, errors))
            if original_quarantine:
                original_quarantine(topic, raw, errors)

        client.set_quarantine_handler(spy_quarantine)

        bad_payload = {
            "schema_version": 999,
            "device_id": "aria-bad-device",
            "source": "simulated",
            "temperature_c": "not_a_float",  # deliberately invalid
        }
        msg = mqtt.MQTTMessage(topic=b"aria/v1/telemetry")
        msg.payload = json.dumps(bad_payload).encode("utf-8")

        client._handle_message(mock_paho, None, msg)
        time.sleep(0.4)

        record = self.db.get_latest_telemetry("aria-bad-device")
        self.assertIsNone(record, "Invalid telemetry must NOT be stored in SQLite")

        self.assertEqual(len(quarantined_events), 1)
        topic, raw, errors = quarantined_events[0]
        self.assertEqual(topic, "aria/v1/telemetry")
        self.assertTrue(any("Unsupported schema_version 999" in err for err in errors))
        self.assertTrue(any("temperature_c must be a number" in err for err in errors))

    # ------------------------------------------------------------------
    # Test 4 — startup initializes MQTT
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_app_startup_starts_mqtt_integration(self, mock_client_cls):
        """Flask application startup initializes and starts the MQTT client."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)

        self.assertIsNotNone(app.mqtt_client)
        self.assertTrue(app.mqtt_client.is_running)
        mock_paho.connect.assert_called_once()
        mock_paho.loop_start.assert_called_once()

    # ------------------------------------------------------------------
    # Test 5 — shutdown stops client and worker
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_clean_shutdown_stops_client_and_worker(self, mock_client_cls):
        """Clean shutdown stops the MQTT network loop and worker thread."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)
        client = app.mqtt_client

        self.assertTrue(client.is_running)
        self.assertIsNotNone(client._worker_thread)
        self.assertTrue(client._worker_thread.is_alive())

        app.stop_mqtt()
        self._app = None  # tearDown must not call stop_mqtt a second time.

        self.assertFalse(client.is_running)
        self.assertIsNone(client._worker_thread)
        mock_paho.disconnect.assert_called_once()
        mock_paho.loop_stop.assert_called_once()

    # ------------------------------------------------------------------
    # Test 6 — guard against double-start
    # ------------------------------------------------------------------
    @patch("paho.mqtt.client.Client")
    def test_client_not_started_multiple_times(self, mock_client_cls):
        """Guard prevents starting multiple MQTT clients or duplicate loops."""
        app, mock_paho = self._create_app_with_mock(mock_client_cls)
        client = app.mqtt_client

        # Second call to start() on an already-running client must be a no-op.
        client.start()

        self.assertEqual(mock_paho.connect.call_count, 1)
        self.assertEqual(mock_paho.loop_start.call_count, 1)


if __name__ == "__main__":
    unittest.main()
