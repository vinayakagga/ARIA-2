"""Phase 7 verification tests for manual MQTT motor control.

Verifies:
1. Valid directional commands ('forward', 'reverse', 'left', 'right', 'stop') are accepted via POST /api/v1/command.
2. Payload published to MQTT topic 'aria/v1/command' is strictly {"command": "<cmd>"}.
3. Published with QoS 1 and retain=False.
4. Publication confirmation is verified (is_published check).
5. Invalid commands, non-string commands, and non-JSON payloads are rejected with HTTP 400.
6. MQTT broker disconnection or client stopped states return HTTP 503.
7. Publication errors or timeouts return HTTP 503 with informative error message.
8. STOP command is always accepted and verified.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

from server.app import create_app
from server.database import Database
from server.mqtt_client import ARIAClient


class TestPhase7MotorControl(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.initialize()
        self.mock_paho = MagicMock()
        self.mock_paho.connect.return_value = 0
        self.mock_paho.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
        self.mock_paho.is_connected.return_value = True

        # Default successful publish setup
        self.mock_info = MagicMock()
        self.mock_info.rc = mqtt.MQTT_ERR_SUCCESS
        self.mock_info.is_published.return_value = True
        self.mock_info.wait_for_publish.return_value = True
        self.mock_info.mid = 42
        self.mock_paho.publish.return_value = self.mock_info

        # Create client with mock
        with patch("paho.mqtt.client.Client", return_value=self.mock_paho):
            self.mqtt_client = ARIAClient()
            self.mqtt_client.start()

        self.app = create_app(db=self.db, mqtt_client=self.mqtt_client, start_mqtt=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.stop_mqtt()
        self.db.close()

    def test_valid_motor_commands(self):
        """All five valid commands ('forward', 'reverse', 'left', 'right', 'stop') succeed."""
        for cmd in ["forward", "reverse", "left", "right", "stop"]:
            resp = self.client.post("/api/v1/command", json={"command": cmd})
            self.assertEqual(resp.status_code, 200, f"Command '{cmd}' failed: {resp.get_data(as_text=True)}")
            data = resp.get_json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["command"], cmd)

            # Verify MQTT publish arguments
            self.mock_paho.publish.assert_called_with(
                "aria/v1/command",
                payload=json.dumps({"command": cmd}),
                qos=1,
                retain=False,
            )

    def test_case_insensitive_and_whitespace_strip(self):
        """Commands are case-insensitive and leading/trailing whitespace is stripped."""
        resp = self.client.post("/api/v1/command", json={"command": "  FORWARD  "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["command"], "forward")

    def test_invalid_command_strings_rejected(self):
        """Unrecognized commands are rejected with HTTP 400."""
        invalid_cmds = ["backward", "turbo", "drift", "spin", "fwd", "", "123"]
        for bad_cmd in invalid_cmds:
            resp = self.client.post("/api/v1/command", json={"command": bad_cmd})
            self.assertEqual(resp.status_code, 400, f"Bad command '{bad_cmd}' was not rejected")
            data = resp.get_json()
            self.assertEqual(data["status"], "error")
            self.assertIn("Invalid command", data["message"])

    def test_missing_or_non_string_command_field(self):
        """Missing 'command' or non-string types return HTTP 400."""
        # Missing field
        resp = self.client.post("/api/v1/command", json={"action": "forward"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Missing required field 'command'", resp.get_json()["message"])

        # Integer
        resp = self.client.post("/api/v1/command", json={"command": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("'command' must be a string", resp.get_json()["message"])

        # Boolean
        resp = self.client.post("/api/v1/command", json={"command": True})
        self.assertEqual(resp.status_code, 400)

        # None
        resp = self.client.post("/api/v1/command", json={"command": None})
        self.assertEqual(resp.status_code, 400)

    def test_non_json_body_rejected(self):
        """Non-JSON bodies and missing Content-Type headers return HTTP 400."""
        resp = self.client.post("/api/v1/command", data="command=forward", content_type="text/plain")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Content-Type must be application/json", resp.get_json()["message"])

    def test_broker_disconnected_returns_503(self):
        """When MQTT broker is disconnected, returns HTTP 503."""
        self.mock_paho.is_connected.return_value = False
        resp = self.client.post("/api/v1/command", json={"command": "stop"})
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "error")
        self.assertIn("broker is disconnected", data["message"])

    def test_mqtt_client_not_running_returns_503(self):
        """When backend MQTT client is stopped, returns HTTP 503."""
        self.mqtt_client.stop()
        resp = self.client.post("/api/v1/command", json={"command": "stop"})
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "error")
        self.assertIn("not running", data["message"])

    def test_mqtt_publish_error_returns_503(self):
        """When Paho publish() returns an error code, returns HTTP 503."""
        fail_info = MagicMock()
        fail_info.rc = mqtt.MQTT_ERR_NO_CONN
        self.mock_paho.publish.return_value = fail_info

        resp = self.client.post("/api/v1/command", json={"command": "forward"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("failed", resp.get_json()["message"].lower())

    def test_mqtt_publish_timeout_returns_503(self):
        """When publish acknowledgment times out, returns HTTP 503."""
        timeout_info = MagicMock()
        timeout_info.rc = mqtt.MQTT_ERR_SUCCESS
        timeout_info.is_published.return_value = False  # Not acknowledged
        self.mock_paho.publish.return_value = timeout_info

        resp = self.client.post("/api/v1/command", json={"command": "left"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("timeout", resp.get_json()["message"].lower())

    def test_stop_command_always_available(self):
        """STOP command succeeds and publishes with QoS 1, retain=False."""
        resp = self.client.post("/api/v1/command", json={"command": "stop"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["command"], "stop")
        self.mock_paho.publish.assert_called_with(
            "aria/v1/command",
            payload='{"command": "stop"}',
            qos=1,
            retain=False,
        )


if __name__ == "__main__":
    unittest.main()
