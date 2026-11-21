"""Phase 1 verification tests for ARIA MQTT receiver."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

PYTHON_EXE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".venv", "Scripts", "python.exe"))
RECEIVER_SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server", "mqtt_receiver.py"))
MOSQUITTO_PUB = r"C:\Program Files\mosquitto\mosquitto_pub.exe"


class TestPhase1MQTTReceiver(unittest.TestCase):
    def test_environment_audit(self):
        """Phase 0: Verify Python version and virtual environment."""
        self.assertTrue(os.path.isfile(PYTHON_EXE), f"Python executable missing: {PYTHON_EXE}")
        ver = subprocess.check_output([PYTHON_EXE, "--version"], text=True).strip()
        self.assertTrue(ver.startswith("Python 3.12"), f"Expected Python 3.12.x, got: {ver}")
        self.assertTrue(os.path.isfile(MOSQUITTO_PUB), f"Mosquitto CLI missing: {MOSQUITTO_PUB}")

    def test_receiver_connection_failure(self):
        """Verify receiver handles connection failure gracefully."""
        env = os.environ.copy()
        env["ARIA_MQTT_PORT"] = "19999"  # non-existent port
        proc = subprocess.Popen(
            [PYTHON_EXE, RECEIVER_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        stdout, _ = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not connect to mqtt://", stdout)

    def test_receiver_receives_cli_publication_and_handles_malformed(self):
        """Phase 1 acceptance test:

        1. Start receiver.
        2. Publish valid telemetry message with mosquitto_pub.
        3. Publish status message with mosquitto_pub.
        4. Publish invalid non-UTF-8 payload with mosquitto_pub.
        5. Stop receiver cleanly and verify output log.
        """
        proc = subprocess.Popen(
            [PYTHON_EXE, RECEIVER_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        time.sleep(1.0)  # Wait for connection and subscription

        try:
            # 1. Publish valid telemetry message
            telemetry_payload = '{"schema_version":1,"device_id":"aria-c3","source":"simulated","temperature_c":28.4}'
            pub1 = subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-p", "1883", "-t", "aria/v1/telemetry", "-m", telemetry_payload],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(pub1.returncode, 0, f"mosquitto_pub failed: {pub1.stderr}")

            # 2. Publish status message
            status_payload = '{"device_id":"aria-c3","status":"online"}'
            pub2 = subprocess.run(
                [MOSQUITTO_PUB, "-h", "127.0.0.1", "-p", "1883", "-t", "aria/v1/status", "-m", status_payload],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(pub2.returncode, 0, f"mosquitto_pub failed: {pub2.stderr}")

            # 3. Publish malformed non-UTF-8 message
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                tf.write(b"\xff\xfe\x80\x81invalid-utf8")
                temp_filename = tf.name

            try:
                pub3 = subprocess.run(
                    [MOSQUITTO_PUB, "-h", "127.0.0.1", "-p", "1883", "-t", "aria/v1/telemetry", "-f", temp_filename],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(pub3.returncode, 0, f"mosquitto_pub failed: {pub3.stderr}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)

            time.sleep(1.0)  # Allow receiver to process messages
        finally:
            proc.terminate()
            stdout, _ = proc.communicate(timeout=5)

        self.assertIn("subscription active", stdout)
        self.assertIn("aria/v1/telemetry", stdout)
        self.assertIn("schema_version", stdout)
        self.assertIn("aria/v1/status", stdout)
        self.assertIn("malformed UTF-8", stdout)

    def test_clean_ctrl_c_shutdown(self):
        """Verify clean shutdown when CTRL_C_EVENT is delivered."""
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [PYTHON_EXE, RECEIVER_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
        )

        time.sleep(1.0)  # Wait for startup and subscription
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_C_EVENT)
        else:
            proc.send_signal(signal.SIGINT)

        stdout, _ = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, f"Process exited with non-zero code {proc.returncode}: {stdout}")
        self.assertIn("shutdown requested (KeyboardInterrupt)", stdout)
        self.assertIn("receiver stopped", stdout)


if __name__ == "__main__":
    unittest.main()
