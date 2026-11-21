"""ARIA Flask REST API (Phase 4).

Provides read-only JSON endpoints for ARIA telemetry and device status.
Reuses the database persistence layer (server/database.py).
Strictly independent of MQTT network transport.
"""

from __future__ import annotations

import atexit
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import socket
from typing import Any

from flask import Flask, jsonify, render_template, request

from server.advice_engine import AdviceEngine
from server.database import Database, MAX_QUERY_LIMIT
from server.mqtt_client import ARIAClient
from server.telemetry import StatusMessage, TelemetryMessage, VALID_COMMANDS

log = logging.getLogger("aria.api")

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    """Load key-value pairs from .env if present into os.environ without overriding."""
    if ENV_FILE_PATH.is_file():
        try:
            with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_env_file()


def _check_broker_connected(timeout: float = 0.3) -> bool:
    host = os.environ.get("ARIA_MQTT_HOST", "127.0.0.1")
    port = int(os.environ.get("ARIA_MQTT_PORT", "1883"))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _telemetry_to_dict(msg: TelemetryMessage) -> dict[str, Any]:
    return {
        "schema_version": msg.schema_version,
        "device_id": msg.device_id,
        "source": msg.source,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
        "device_timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        "temperature_c": msg.temperature_c,
        "humidity_pct": msg.humidity_pct,
        "light_lux": msg.light_lux,
        "mq135_raw": msg.mq135_raw,
        "gps": {
            "fix": msg.gps.fix,
            "latitude": msg.gps.latitude,
            "longitude": msg.gps.longitude,
            "satellites": msg.gps.satellites,
            "altitude_m": msg.gps.altitude_m,
            "speed_mps": msg.gps.speed_mps,
        } if msg.gps else None,
    }


def _status_to_dict(msg: StatusMessage) -> dict[str, Any]:
    return {
        "schema_version": msg.schema_version,
        "device_id": msg.device_id,
        "status": msg.status,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
        "device_timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
        "ip_address": msg.ip_address,
        "wifi_rssi_dbm": msg.wifi_rssi_dbm,
        "uptime_s": msg.uptime_s,
    }


def create_app(
    db: Database | None = None,
    mqtt_client: ARIAClient | None = None,
    start_mqtt: bool | None = None,
    advice_engine: AdviceEngine | None = None,
) -> Flask:
    """Application factory for the ARIA REST API and Dashboard.

    Integrates MQTT transport with SQLite database persistence.
    """
    app = Flask(
        __name__,
        template_folder=str(DASHBOARD_DIR / "templates"),
        static_folder=str(DASHBOARD_DIR / "static"),
    )
    database = db if db is not None else Database()
    app.advice_engine = advice_engine if advice_engine is not None else AdviceEngine(db=database)

    # Determine whether to start MQTT client
    if start_mqtt is None:
        start_mqtt = os.environ.get("ARIA_START_MQTT", "true").strip().lower() not in ("false", "0", "no")

    # Setup ARIAClient instance
    if mqtt_client is None:
        host = os.environ.get("ARIA_MQTT_HOST", "127.0.0.1")
        port = int(os.environ.get("ARIA_MQTT_PORT", "1883"))
        import uuid
        client_id = os.environ.get("ARIA_MQTT_CLIENT_ID") or f"aria-backend-{uuid.uuid4().hex[:8]}"
        client = ARIAClient(host=host, port=port, client_id=client_id)
    else:
        client = mqtt_client

    # Define handlers connecting MQTT events to SQLite persistence
    def on_telemetry(msg: TelemetryMessage) -> None:
        try:
            row_id = database.insert_telemetry(msg)
            log.info("persisted telemetry id=%s device_id=%s source=%s", row_id, msg.device_id, msg.source)
        except Exception as exc:
            log.error("failed to persist telemetry from device %s: %s", msg.device_id, exc)
            raise

    def on_status(msg: StatusMessage) -> None:
        try:
            row_id = database.insert_status(msg)
            log.info("persisted status id=%s device_id=%s status=%s", row_id, msg.device_id, msg.status)
        except Exception as exc:
            log.error("failed to persist status from device %s: %s", msg.device_id, exc)
            raise

    def on_quarantine(topic: str, raw_payload: bytes, errors: list[str]) -> None:
        log.warning(
            "quarantined payload on topic=%s (%d bytes): %s",
            topic,
            len(raw_payload),
            "; ".join(errors),
        )

    client.set_telemetry_handler(on_telemetry)
    client.set_status_handler(on_status)
    client.set_quarantine_handler(on_quarantine)

    app.mqtt_client = client
    database._associated_client = client

    def stop_mqtt() -> None:
        if getattr(app, "mqtt_client", None) is not None:
            try:
                app.mqtt_client.stop()
            except Exception:
                log.exception("error stopping MQTT client")
            app.mqtt_client = None

    app.stop_mqtt = stop_mqtt
    atexit.register(stop_mqtt)

    # Start MQTT client unless in Werkzeug reloader master process
    is_reloader_master = (
        os.environ.get("WERKZEUG_RUN_MAIN") is None
        and app.debug
    )
    if start_mqtt and not is_reloader_master:
        if not client.is_running:
            try:
                client.start()
            except Exception as exc:
                log.error("failed to connect MQTT client during application startup: %s", exc)
                raise

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify({"status": "error", "message": str(error.description or "Bad request")}), 400

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"status": "error", "message": str(error.description or "Resource not found")}), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({"status": "error", "message": "Internal server error"}), 500

    @app.route("/", methods=["GET"])
    def index():
        """Render the ARIA Web Dashboard."""
        return render_template("index.html")

    @app.route("/api/v1/health", methods=["GET"])
    def health_check():
        """Health check endpoint confirming API, DB, and broker connectivity."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            database.count_telemetry()
            db_status = "connected"
        except Exception as exc:
            log.error("health check DB failure: %s", exc)
            return jsonify({
                "status": "degraded",
                "timestamp": now,
                "database": "disconnected",
                "error": str(exc),
            }), 503

        if getattr(app, "mqtt_client", None) is not None and app.mqtt_client.is_connected:
            broker_status = "connected"
        else:
            broker_status = "connected" if _check_broker_connected() else "disconnected"

        return jsonify({
            "status": "ok",
            "timestamp": now,
            "database": db_status,
            "broker": broker_status,
        }), 200

    @app.route("/api/v1/telemetry/latest", methods=["GET"])
    def get_latest_telemetry():
        """Return the most recent telemetry record, optionally filtered by device_id."""
        device_id = request.args.get("device_id")
        if device_id is not None:
            device_id = device_id.strip()
            if not device_id:
                return jsonify({"status": "error", "message": "device_id cannot be empty"}), 400

        msg = database.get_latest_telemetry(device_id=device_id)
        if msg is None:
            if device_id:
                return jsonify({
                    "status": "error",
                    "message": f"no telemetry found for device: {device_id}",
                }), 404
            return jsonify({
                "status": "success",
                "data": None,
                "message": "no telemetry recorded yet",
            }), 200

        return jsonify({
            "status": "success",
            "data": _telemetry_to_dict(msg),
        }), 200

    @app.route("/api/v1/telemetry/history", methods=["GET"])
    def get_telemetry_history():
        """Return bounded historical telemetry records ordered by received_at DESC."""
        # 1. Validate device_id
        device_id = request.args.get("device_id")
        if device_id is not None:
            device_id = device_id.strip()
            if not device_id:
                return jsonify({"status": "error", "message": "device_id cannot be empty"}), 400

        # 2. Validate limit
        raw_limit = request.args.get("limit")
        limit = 100
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                return jsonify({"status": "error", "message": "limit must be an integer"}), 400
            if limit < 1 or limit > MAX_QUERY_LIMIT:
                return jsonify({
                    "status": "error",
                    "message": f"limit must be between 1 and {MAX_QUERY_LIMIT}",
                }), 400

        # 3. Validate since (ISO-8601 string)
        raw_since = request.args.get("since")
        since: datetime | None = None
        if raw_since is not None:
            s = raw_since.strip()
            if s:
                # Handle space replacement from unencoded '+' in URL query parameter (e.g. ' 00:00')
                if " " in s and "+" not in s and "-" not in s[10:]:
                    parts = s.rsplit(" ", 1)
                    if len(parts) == 2 and ":" in parts[1]:
                        s = f"{parts[0]}+{parts[1]}"
                if s.endswith("Z") or s.endswith("z"):
                    s = s[:-1] + "+00:00"
                try:
                    since = datetime.fromisoformat(s)
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    return jsonify({
                        "status": "error",
                        "message": f"invalid ISO-8601 timestamp for 'since': {exc}",
                    }), 400

        history = database.get_telemetry_history(device_id=device_id, limit=limit, since=since)
        return jsonify({
            "status": "success",
            "count": len(history),
            "limit": limit,
            "data": [_telemetry_to_dict(m) for m in history],
        }), 200

    @app.route("/api/v1/status/latest", methods=["GET"])
    def get_latest_status():
        """Return latest status record, optionally filtered by device_id."""
        device_id = request.args.get("device_id")
        if device_id is not None:
            device_id = device_id.strip()
            if not device_id:
                return jsonify({"status": "error", "message": "device_id cannot be empty"}), 400

        msg = database.get_latest_status(device_id=device_id)
        if msg is None:
            if device_id:
                return jsonify({
                    "status": "error",
                    "message": f"no status found for device: {device_id}",
                }), 404
            return jsonify({
                "status": "success",
                "data": None,
                "message": "no status recorded yet",
            }), 200

        return jsonify({
            "status": "success",
            "data": _status_to_dict(msg),
        }), 200

    @app.route("/api/v1/command", methods=["POST"])
    def post_command():
        """Publish a motor control command to aria/v1/command.

        Accepts only JSON: {"command": "<forward|reverse|left|right|stop>"}.
        Rejects all invalid commands, extra paths, and non-JSON payloads.
        Publishes with QoS 1 and retain=False.
        """
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Content-Type must be application/json",
            }), 400

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({
                "status": "error",
                "message": "Invalid JSON body; expected an object",
            }), 400

        cmd = data.get("command")
        if cmd is None:
            return jsonify({
                "status": "error",
                "message": "Missing required field 'command'",
            }), 400

        if not isinstance(cmd, str):
            return jsonify({
                "status": "error",
                "message": "'command' must be a string",
            }), 400

        cmd = cmd.strip().lower()
        if cmd not in VALID_COMMANDS:
            return jsonify({
                "status": "error",
                "message": f"Invalid command '{cmd}'. Must be one of {sorted(VALID_COMMANDS)}",
            }), 400

        client: ARIAClient | None = getattr(app, "mqtt_client", None)
        if client is None or not client.is_running:
            return jsonify({
                "status": "error",
                "message": "MQTT client is not running on backend",
            }), 503

        if not client.is_connected:
            return jsonify({
                "status": "error",
                "message": "MQTT broker is disconnected; cannot transmit command",
            }), 503

        success, err = client.publish_command(cmd)
        if not success:
            log.error("Failed to publish command '%s': %s", cmd, err)
            return jsonify({
                "status": "error",
                "message": f"Command publish failed: {err}",
            }), 503

        return jsonify({
            "status": "success",
            "command": cmd,
        }), 200

    @app.route("/api/v1/advice/latest", methods=["GET"])
    @app.route("/api/v1/advice", methods=["GET"])
    def get_latest_advice():
        """Retrieve the most recently generated advice."""
        device_id = request.args.get("device_id")
        if device_id is not None:
            device_id = device_id.strip()
            if not device_id:
                return jsonify({"status": "error", "message": "device_id cannot be empty"}), 400

        try:
            advice = app.advice_engine.get_latest_advice(device_id=device_id)
            if advice is None:
                return jsonify({
                    "status": "success",
                    "data": None,
                    "message": "no advice generated yet",
                }), 200

            return jsonify({
                "status": "success",
                "data": advice,
            }), 200
        except Exception as exc:
            log.exception("failed to retrieve latest advice: %s", exc)
            return jsonify({
                "status": "error",
                "message": "Internal error retrieving advice",
            }), 500

    @app.route("/api/v1/advice/generate", methods=["POST"])
    @app.route("/api/v1/advice", methods=["POST"])
    def generate_advice():
        """Manually trigger agricultural advice generation.

        Enforces a 10-minute cooldown on external NVIDIA AI generation.
        Returns unified structured advice response.
        """
        device_id = None
        if request.is_json and request.data:
            body = request.get_json(silent=True)
            if body is not None:
                if not isinstance(body, dict):
                    return jsonify({"status": "error", "message": "Invalid JSON body; expected an object"}), 400
                device_id = body.get("device_id")

        if device_id is None:
            device_id = request.args.get("device_id")

        if device_id is not None:
            if not isinstance(device_id, str):
                return jsonify({"status": "error", "message": "device_id must be a string"}), 400
            device_id = device_id.strip()
            if not device_id:
                return jsonify({"status": "error", "message": "device_id cannot be empty"}), 400

        force_weather = request.args.get("force_weather", "false").lower() in ("true", "1")

        try:
            advice = app.advice_engine.generate_advice(
                device_id=device_id,
                force_refresh_weather=force_weather,
            )
            return jsonify({
                "status": "success",
                "data": advice,
            }), 200
        except Exception as exc:
            log.exception("unexpected error generating advice: %s", exc)
            return jsonify({
                "status": "error",
                "message": "Internal error generating advice",
            }), 500

    return app


if __name__ == "__main__":
    host = os.environ.get("ARIA_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("ARIA_HTTP_PORT", "5000"))
    app = create_app()
    log.info("Starting ARIA Flask API on %s:%s", host, port)
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        app.stop_mqtt()
