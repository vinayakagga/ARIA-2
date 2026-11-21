"""Minimal ARIA MQTT subscriber for Phase 1.

Subscribes to aria/# on the local Mosquitto broker and prints payloads.
This process does not validate the telemetry schema, store data, or send
movement commands. A printed payload is a transport test result, not a
claim that a physical sensor produced the values.
"""

from __future__ import annotations

import logging
import os
import signal
import sys

import paho.mqtt.client as mqtt

TOPIC = "aria/#"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("aria.mqtt_receiver")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.error("environment variable %s=%r is not an integer", name, raw)
        raise SystemExit(2) from None


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("broker rejected connection: %s", reason_code)
        return
    log.info(
        "connected to broker session_present=%s",
        getattr(flags, "session_present", flags),
    )
    result, mid = client.subscribe(TOPIC, qos=0)
    if result != mqtt.MQTT_ERR_SUCCESS:
        log.error("subscribe to %s failed: rc=%s mid=%s", TOPIC, result, mid)
        return
    log.info("subscribe requested for %s (mid=%s)", TOPIC, mid)


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    failures = [code for code in reason_code_list if getattr(code, "is_failure", False)]
    if failures:
        log.error("broker refused subscription mid=%s reasons=%s", mid, failures)
        return
    log.info("subscription active mid=%s granted=%s", mid, list(reason_code_list))


def on_message(client, userdata, message):
    try:
        text = message.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        log.error(
            "malformed UTF-8 on topic=%s bytes=%s error=%s",
            message.topic,
            len(message.payload),
            exc,
        )
        return
    except Exception:
        log.exception(
            "unexpected failure reading message on topic=%s",
            getattr(message, "topic", "?"),
        )
        return
    log.info(
        "message topic=%s qos=%s retain=%s bytes=%s payload=%s",
        message.topic,
        message.qos,
        message.retain,
        len(message.payload),
        text,
    )


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code == 0 or not getattr(reason_code, "is_failure", False):
        log.info("disconnected: %s", reason_code)
        return
    log.error("unexpected disconnect: %s", reason_code)


def build_client() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="aria-pc-receiver",
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    return client


def _enable_console_interrupt() -> None:
    """Restore Ctrl+C handling for this process.

    Windows disables Ctrl+C when a process is started as a new process
    group. This call affects only the receiver process. It does not change
    the machine-wide console setting.
    """
    if os.name != "nt":
        return
    import ctypes

    ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(None, False)


def _ignore_further_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)


def main() -> int:
    _enable_console_interrupt()
    host = os.environ.get("ARIA_MQTT_HOST", DEFAULT_HOST)
    try:
        port = _env_int("ARIA_MQTT_PORT", DEFAULT_PORT)
    except SystemExit:
        return 2

    client = build_client()
    log.info("connecting to mqtt://%s:%s topic=%s", host, port, TOPIC)
    try:
        client.connect(host, port, keepalive=DEFAULT_KEEPALIVE)
    except OSError as exc:
        log.error("could not connect to mqtt://%s:%s: %s", host, port, exc)
        return 1

    try:
        client.loop_forever(retry_first_connection=False)
    except KeyboardInterrupt:
        _ignore_further_interrupts()
        log.info("shutdown requested (KeyboardInterrupt)")
    finally:
        _ignore_further_interrupts()
        try:
            client.disconnect()
        except Exception:
            log.exception("disconnect failed during shutdown")
        try:
            client.loop_stop()
        except Exception:
            log.exception("loop_stop failed during shutdown")
        log.info("receiver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
