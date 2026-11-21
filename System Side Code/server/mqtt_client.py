"""ARIA MQTT Transport Client (Phase 2 foundation).

Encapsulates Paho MQTT transport, connection lifecycle, and topic routing.
Decouples network transport from payload validation and business logic.
Keeps MQTT network callbacks strictly non-blocking.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import queue
import threading
from typing import Callable

import paho.mqtt.client as mqtt

from server.telemetry import (
    StatusMessage,
    TelemetryMessage,
    VALID_COMMANDS,
    validate_status_payload,
    validate_telemetry_payload,
)

log = logging.getLogger("aria.mqtt_client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 60
DEFAULT_SUBSCRIPTIONS = (("aria/v1/#", 1),)

TelemetryHandler = Callable[[TelemetryMessage], None]
StatusHandler = Callable[[StatusMessage], None]
QuarantineHandler = Callable[[str, bytes, list[str]], None]


class ARIAClient:
    """Decoupled MQTT client for ARIA."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: str = "aria-backend-client",
        keepalive: int = DEFAULT_KEEPALIVE,
        on_telemetry: TelemetryHandler | None = None,
        on_status: StatusHandler | None = None,
        on_quarantine: QuarantineHandler | None = None,
    ) -> None:
        self.host = host or os.environ.get("ARIA_MQTT_HOST", DEFAULT_HOST)
        self.port = port or int(os.environ.get("ARIA_MQTT_PORT", str(DEFAULT_PORT)))
        self.client_id = client_id
        self.keepalive = keepalive

        self._on_telemetry = on_telemetry
        self._on_status = on_status
        self._on_quarantine = on_quarantine

        self._queue: queue.Queue[tuple[str, bytes, datetime]] = queue.Queue(maxsize=1000)
        self._running = False
        self._worker_thread: threading.Thread | None = None

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
        )
        self._client.on_connect = self._handle_connect
        self._client.on_subscribe = self._handle_subscribe
        self._client.on_message = self._handle_message
        self._client.on_disconnect = self._handle_disconnect

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected()

    def set_telemetry_handler(self, handler: TelemetryHandler) -> None:
        self._on_telemetry = handler

    def set_status_handler(self, handler: StatusHandler) -> None:
        self._on_status = handler

    def set_quarantine_handler(self, handler: QuarantineHandler) -> None:
        self._on_quarantine = handler

    def publish_command(self, command: str, timeout: float = 1.0) -> tuple[bool, str | None]:
        """Publish a validated motor command to aria/v1/command.

        Guarantees:
        - Only commands in VALID_COMMANDS ('forward', 'reverse', 'left', 'right', 'stop') are accepted.
        - Fixed topic: 'aria/v1/command'. Arbitrary topics or payloads cannot be injected.
        - Fixed QoS: 1.
        - Fixed Retain: False (strictly never retain motor commands).
        - Verifies actual delivery (PUBACK for QoS 1).

        Returns:
            (True, None) on verified delivery.
            (False, error_message) on failure or timeout.
        """
        if command not in VALID_COMMANDS:
            return False, f"Invalid command '{command}'. Allowed: {sorted(VALID_COMMANDS)}"

        if not self._running:
            return False, "MQTT client is not running"

        if not self.is_connected:
            return False, "MQTT broker is not connected"

        payload = json.dumps({"command": command})
        try:
            info = self._client.publish("aria/v1/command", payload=payload, qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                log.error("MQTT publish failed with rc=%s for command=%s", info.rc, command)
                return False, f"MQTT publish failed with error code {info.rc}"

            # Wait for publication confirmation (PUBACK for QoS 1)
            try:
                info.wait_for_publish(timeout=timeout)
            except (ValueError, RuntimeError) as exc:
                return False, f"MQTT wait_for_publish failed: {exc}"

            if not info.is_published():
                return False, "MQTT publish acknowledgment timeout"

            log.info("published motor command '%s' mid=%s", command, getattr(info, "mid", None))
            return True, None
        except Exception as exc:
            log.exception("exception publishing command '%s'", command)
            return False, str(exc)

    def _handle_connect(self, client: mqtt.Client, userdata: any, flags: any, reason_code: any, properties: any) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection failed with reason code: %s", reason_code)
            return
        log.info(
            "connected to MQTT broker at %s:%s session_present=%s",
            self.host,
            self.port,
            getattr(flags, "session_present", flags),
        )
        for topic, qos in DEFAULT_SUBSCRIPTIONS:
            result, mid = client.subscribe(topic, qos=qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                log.error("subscription request failed for topic=%s rc=%s", topic, result)
            else:
                log.info("subscription requested: topic=%s qos=%s mid=%s", topic, qos, mid)

    def _handle_subscribe(self, client: mqtt.Client, userdata: any, mid: int, reason_code_list: any, properties: any) -> None:
        failures = [rc for rc in reason_code_list if getattr(rc, "is_failure", False)]
        if failures:
            log.error("subscription rejected mid=%s reasons=%s", mid, failures)
        else:
            log.info("subscription confirmed mid=%s granted=%s", mid, list(reason_code_list))

    def _handle_message(self, client: mqtt.Client, userdata: any, message: mqtt.MQTTMessage) -> None:
        # Non-blocking callback: immediately queue incoming message and return
        now = datetime.now(timezone.utc)
        try:
            self._queue.put_nowait((message.topic, message.payload, now))
        except queue.Full:
            log.error("message queue full; dropping message from topic=%s", message.topic)

    def _handle_disconnect(self, client: mqtt.Client, userdata: any, disconnect_flags: any, reason_code: any, properties: any) -> None:
        if reason_code == 0 or not getattr(reason_code, "is_failure", False):
            log.info("client disconnected normally (%s)", reason_code)
        else:
            log.warning("unexpected client disconnect (%s)", reason_code)

    def _dispatch_worker(self) -> None:
        """Worker thread that drains incoming MQTT messages and dispatches validation."""
        while self._running:
            try:
                topic, raw_payload, received_at = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self._process_message(topic, raw_payload, received_at)
            except Exception:
                log.exception("unhandled exception processing message from topic=%s", topic)
            finally:
                self._queue.task_done()

    def _process_message(self, topic: str, raw_payload: bytes, received_at: datetime) -> None:
        if topic == "aria/v1/telemetry" or topic.startswith("aria/telemetry"):
            res = validate_telemetry_payload(raw_payload, received_at=received_at)
            if res.is_valid and res.data:
                if self._on_telemetry:
                    self._on_telemetry(res.data)
            else:
                if self._on_quarantine:
                    self._on_quarantine(topic, raw_payload, res.errors)
        elif topic == "aria/v1/status" or topic.startswith("aria/status"):
            res = validate_status_payload(raw_payload, received_at=received_at)
            if res.is_valid and res.data:
                if self._on_status:
                    self._on_status(res.data)
            else:
                if self._on_quarantine:
                    self._on_quarantine(topic, raw_payload, res.errors)
        else:
            log.debug("unhandled topic message received: topic=%s bytes=%s", topic, len(raw_payload))

    def start(self) -> None:
        """Start the worker thread and connect the MQTT network loop."""
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._dispatch_worker, name="aria-mqtt-dispatcher", daemon=True)
        self._worker_thread.start()

        log.info("connecting to MQTT broker at %s:%s", self.host, self.port)
        try:
            self._client.connect(self.host, self.port, keepalive=self.keepalive)
            self._client.loop_start()
        except Exception:
            self._running = False
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=2.0)
                self._worker_thread = None
            raise

    def stop(self) -> None:
        """Stop network loop and drain worker thread cleanly."""
        if not self._running:
            return
        self._running = False
        try:
            self._client.disconnect()
        except Exception:
            log.exception("error during disconnect")
        try:
            self._client.loop_stop()
        except Exception:
            log.exception("error stopping network loop")

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
            self._worker_thread = None

        for sock_name in ("_sockpairR", "_sockpairW"):
            s = getattr(self._client, sock_name, None)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

        log.info("MQTT client stopped")
