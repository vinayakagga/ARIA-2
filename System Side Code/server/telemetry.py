"""Telemetry and Status payload parsing and validation for ARIA.

Phase 2 foundation: Decouples payload validation from MQTT transport.
Validates incoming JSON payloads against docs/MQTT_CONTRACT.md.
Distinguishes malformed payloads (quarantined) from valid payloads
with unavailable/null sensor fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Generic, TypeVar

log = logging.getLogger("aria.telemetry")

SUPPORTED_SCHEMA_VERSIONS = {1}
VALID_SOURCES = {"simulated", "real"}
VALID_STATUSES = {"online", "offline", "degraded", "error"}
VALID_COMMANDS = {"forward", "reverse", "left", "right", "stop"}
DEVICE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

T = TypeVar("T")


@dataclass(frozen=True)
class ValidationResult(Generic[T]):
    is_valid: bool
    data: T | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GPSData:
    fix: bool
    latitude: float | None = None
    longitude: float | None = None
    satellites: int | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None


@dataclass(frozen=True)
class TelemetryMessage:
    schema_version: int
    device_id: str
    source: str
    received_at: datetime
    timestamp: datetime | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    light_lux: float | None = None
    mq135_raw: int | None = None
    gps: GPSData | None = None
    raw_payload: str = ""


@dataclass(frozen=True)
class StatusMessage:
    schema_version: int
    device_id: str
    status: str
    received_at: datetime
    ip_address: str | None = None
    wifi_rssi_dbm: int | None = None
    uptime_s: int | None = None
    timestamp: datetime | None = None
    raw_payload: str = ""


def _parse_iso_timestamp(val: Any) -> datetime | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"timestamp must be a string, got {type(val).__name__}")
    s = val.strip()
    if not s:
        return None
    # Support 'Z' as UTC
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Default naive timestamps to UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_float_or_null(val: Any, name: str, min_val: float | None = None, max_val: float | None = None) -> float | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a number or null, got {type(val).__name__}")
    f = float(val)
    if min_val is not None and f < min_val:
        raise ValueError(f"{name} ({f}) is below minimum allowed value ({min_val})")
    if max_val is not None and f > max_val:
        raise ValueError(f"{name} ({f}) exceeds maximum allowed value ({max_val})")
    return f


def _check_int_or_null(val: Any, name: str, min_val: int | None = None, max_val: int | None = None) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{name} must be an integer or null, got {type(val).__name__}")
    if min_val is not None and val < min_val:
        raise ValueError(f"{name} ({val}) is below minimum allowed value ({min_val})")
    if max_val is not None and val > max_val:
        raise ValueError(f"{name} ({val}) exceeds maximum allowed value ({max_val})")
    return val


def _validate_gps(raw_gps: Any) -> GPSData | None:
    if raw_gps is None:
        return None
    if not isinstance(raw_gps, dict):
        raise ValueError(f"gps must be an object or null, got {type(raw_gps).__name__}")

    fix_val = raw_gps.get("fix")
    if not isinstance(fix_val, bool):
        raise ValueError(f"gps.fix must be a boolean, got {type(fix_val).__name__}")

    lat_val = raw_gps.get("latitude")
    lon_val = raw_gps.get("longitude")

    if fix_val:
        # When fix is true, latitude and longitude must be valid floats
        if lat_val is None or lon_val is None:
            raise ValueError("gps.latitude and gps.longitude are required numbers when gps.fix is true")
        lat = _check_float_or_null(lat_val, "gps.latitude", -90.0, 90.0)
        lon = _check_float_or_null(lon_val, "gps.longitude", -180.0, 180.0)
    else:
        # When fix is false, coordinates must be null
        if lat_val is not None or lon_val is not None:
            raise ValueError("gps.latitude and gps.longitude must be null when gps.fix is false")
        lat = None
        lon = None

    sats = _check_int_or_null(raw_gps.get("satellites"), "gps.satellites", min_val=0)
    alt = _check_float_or_null(raw_gps.get("altitude_m"), "gps.altitude_m")
    spd = _check_float_or_null(raw_gps.get("speed_mps"), "gps.speed_mps", min_val=0.0)

    return GPSData(
        fix=fix_val,
        latitude=lat,
        longitude=lon,
        satellites=sats,
        altitude_m=alt,
        speed_mps=spd,
    )


def validate_telemetry_payload(
    raw_payload: str | bytes,
    received_at: datetime | None = None,
) -> ValidationResult[TelemetryMessage]:
    """Validate a raw payload against the ARIA Telemetry contract.

    Returns ValidationResult containing the parsed TelemetryMessage if valid,
    or a list of validation errors if invalid/quarantined.
    """
    if received_at is None:
        received_at = datetime.now(timezone.utc)

    # 1. Decode UTF-8 string
    if isinstance(raw_payload, bytes):
        try:
            text = raw_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            log.warning("quarantined payload: malformed UTF-8: %s", exc)
            return ValidationResult(is_valid=False, errors=[f"Malformed UTF-8 encoding: {exc}"])
    elif isinstance(raw_payload, str):
        text = raw_payload
    else:
        return ValidationResult(is_valid=False, errors=[f"Expected bytes or str, got {type(raw_payload).__name__}"])

    # 2. Parse JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("quarantined payload: invalid JSON: %s", exc)
        return ValidationResult(is_valid=False, errors=[f"Invalid JSON: {exc}"])

    if not isinstance(data, dict):
        return ValidationResult(is_valid=False, errors=[f"Root JSON must be an object, got {type(data).__name__}"])

    errors: list[str] = []

    # 3. Required top-level fields
    # schema_version
    schema_version = data.get("schema_version")
    if schema_version is None:
        errors.append("Missing required field: 'schema_version'")
    elif isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.append(f"'schema_version' must be an integer, got {type(schema_version).__name__}")
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"Unsupported schema_version {schema_version}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}")

    # device_id
    device_id = data.get("device_id")
    if device_id is None:
        errors.append("Missing required field: 'device_id'")
    elif not isinstance(device_id, str):
        errors.append(f"'device_id' must be a string, got {type(device_id).__name__}")
    elif not DEVICE_ID_PATTERN.match(device_id):
        errors.append(f"'device_id' does not match allowed pattern [a-zA-Z0-9_-]{{1,64}}: {device_id!r}")

    # source
    source = data.get("source")
    if source is None:
        errors.append("Missing required field: 'source'")
    elif not isinstance(source, str):
        errors.append(f"'source' must be a string, got {type(source).__name__}")
    elif source not in VALID_SOURCES:
        errors.append(f"Invalid 'source' value: {source!r}; must be one of {sorted(VALID_SOURCES)}")

    # timestamp
    timestamp: datetime | None = None
    if "timestamp" in data:
        try:
            timestamp = _parse_iso_timestamp(data["timestamp"])
        except Exception as exc:
            errors.append(f"Invalid 'timestamp': {exc}")

    # 4. Sensor readings (optional, nullable)
    temp_c: float | None = None
    hum_pct: float | None = None
    lux: float | None = None
    mq135: int | None = None

    try:
        temp_c = _check_float_or_null(data.get("temperature_c"), "temperature_c", -40.0, 80.0)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        hum_pct = _check_float_or_null(data.get("humidity_pct"), "humidity_pct", 0.0, 100.0)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        lux = _check_float_or_null(data.get("light_lux"), "light_lux", min_val=0.0)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        mq135 = _check_int_or_null(data.get("mq135_raw"), "mq135_raw", 0, 4095)
    except ValueError as exc:
        errors.append(str(exc))

    # Note: HC-SR04 ultrasonic sensor has been removed from hardware.
    # Legacy distance_cm in incoming payloads is safely ignored.

    # 5. GPS sub-object
    gps_data: GPSData | None = None
    if "gps" in data:
        try:
            gps_data = _validate_gps(data.get("gps"))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        log.warning("quarantined telemetry payload from %s: %s", device_id or "unknown", "; ".join(errors))
        return ValidationResult(is_valid=False, errors=errors)

    msg = TelemetryMessage(
        schema_version=schema_version,
        device_id=device_id,
        source=source,
        received_at=received_at,
        timestamp=timestamp,
        temperature_c=temp_c,
        humidity_pct=hum_pct,
        light_lux=lux,
        mq135_raw=mq135,
        gps=gps_data,
        raw_payload=text,
    )
    return ValidationResult(is_valid=True, data=msg)


def validate_status_payload(
    raw_payload: str | bytes,
    received_at: datetime | None = None,
) -> ValidationResult[StatusMessage]:
    """Validate a raw payload against the ARIA Status contract."""
    if received_at is None:
        received_at = datetime.now(timezone.utc)

    if isinstance(raw_payload, bytes):
        try:
            text = raw_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            log.warning("quarantined status payload: malformed UTF-8: %s", exc)
            return ValidationResult(is_valid=False, errors=[f"Malformed UTF-8 encoding: {exc}"])
    elif isinstance(raw_payload, str):
        text = raw_payload
    else:
        return ValidationResult(is_valid=False, errors=[f"Expected bytes or str, got {type(raw_payload).__name__}"])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("quarantined status payload: invalid JSON: %s", exc)
        return ValidationResult(is_valid=False, errors=[f"Invalid JSON: {exc}"])

    if not isinstance(data, dict):
        return ValidationResult(is_valid=False, errors=[f"Root JSON must be an object, got {type(data).__name__}"])

    errors: list[str] = []

    # schema_version
    schema_version = data.get("schema_version")
    if schema_version is None:
        errors.append("Missing required field: 'schema_version'")
    elif isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.append(f"'schema_version' must be an integer, got {type(schema_version).__name__}")
    elif schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"Unsupported schema_version {schema_version}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}")

    # device_id
    device_id = data.get("device_id")
    if device_id is None:
        errors.append("Missing required field: 'device_id'")
    elif not isinstance(device_id, str):
        errors.append(f"'device_id' must be a string, got {type(device_id).__name__}")
    elif not DEVICE_ID_PATTERN.match(device_id):
        errors.append(f"'device_id' does not match allowed pattern: {device_id!r}")

    # status
    status = data.get("status")
    if status is None:
        errors.append("Missing required field: 'status'")
    elif not isinstance(status, str):
        errors.append(f"'status' must be a string, got {type(status).__name__}")
    elif status not in VALID_STATUSES:
        errors.append(f"Invalid 'status' value: {status!r}; must be one of {sorted(VALID_STATUSES)}")

    ip_address = data.get("ip_address")
    if ip_address is not None and not isinstance(ip_address, str):
        errors.append(f"'ip_address' must be a string or null, got {type(ip_address).__name__}")

    try:
        wifi_rssi = _check_int_or_null(data.get("wifi_rssi_dbm"), "wifi_rssi_dbm", -120, 0)
    except ValueError as exc:
        errors.append(str(exc))
        wifi_rssi = None

    try:
        uptime = _check_int_or_null(data.get("uptime_s"), "uptime_s", min_val=0)
    except ValueError as exc:
        errors.append(str(exc))
        uptime = None

    timestamp: datetime | None = None
    if "timestamp" in data:
        try:
            timestamp = _parse_iso_timestamp(data["timestamp"])
        except Exception as exc:
            errors.append(f"Invalid 'timestamp': {exc}")

    if errors:
        log.warning("quarantined status payload from %s: %s", device_id or "unknown", "; ".join(errors))
        return ValidationResult(is_valid=False, errors=errors)

    msg = StatusMessage(
        schema_version=schema_version,
        device_id=device_id,
        status=status,
        received_at=received_at,
        ip_address=ip_address,
        wifi_rssi_dbm=wifi_rssi,
        uptime_s=uptime,
        timestamp=timestamp,
        raw_payload=text,
    )
    return ValidationResult(is_valid=True, data=msg)
