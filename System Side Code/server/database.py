"""SQLite persistence layer for ARIA (Phase 3).

Stores validated telemetry and status messages in a local SQLite database.
- Uses parameterized queries exclusively.
- Stores received_at (server UTC) and device timestamp separately.
- Implements bounded history queries to prevent memory exhaustion.
- Handles out-of-order and duplicate device timestamps deliberately by using
  server received_at for deterministic ordering.
- Thread-safe connection handling using thread-local connections.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

from server.telemetry import GPSData, StatusMessage, TelemetryMessage

log = logging.getLogger("aria.database")

DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "aria.db"

MAX_QUERY_LIMIT = 1000
DEFAULT_QUERY_LIMIT = 100

SCHEMA_VERSION = 1

INIT_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    device_timestamp TEXT,
    temperature_c REAL,
    humidity_pct REAL,
    light_lux REAL,
    mq135_raw INTEGER,
    distance_cm REAL,
    gps_fix INTEGER NOT NULL,
    gps_lat REAL,
    gps_lon REAL,
    gps_satellites INTEGER,
    gps_altitude_m REAL,
    gps_speed_mps REAL,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_received_at ON telemetry(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_received ON telemetry(device_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts ON telemetry(device_id, device_timestamp DESC);

CREATE TABLE IF NOT EXISTS device_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    device_timestamp TEXT,
    ip_address TEXT,
    wifi_rssi_dbm INTEGER,
    uptime_s INTEGER,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_status_device_received ON device_status(device_id, received_at DESC);

CREATE TABLE IF NOT EXISTS device_gps_location (
    device_id TEXT PRIMARY KEY,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    last_fix_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gps_device_updated ON device_gps_location(updated_at DESC);

CREATE TABLE IF NOT EXISTS device_advice (
    device_id TEXT PRIMARY KEY,
    advice_mode TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_ai_call_at TEXT,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_advice_device_created ON device_advice(created_at DESC);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(val: str | None) -> datetime | None:
    if val is None or not val.strip():
        return None
    dt = datetime.fromisoformat(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    """Thread-safe SQLite database manager for ARIA."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            self._db_path_raw: str = os.environ.get("ARIA_DB_PATH", str(DEFAULT_DB_PATH))
        else:
            self._db_path_raw = str(db_path)

        # Detect SQLite URI strings (e.g. "file::memory:?cache=shared").
        # These are passed through as-is with uri=True; Path() must NOT be used.
        self._is_uri: bool = self._db_path_raw.startswith("file:")

        if self._is_uri:
            # Provide a Path-compatible attribute pointing at ":memory:" for
            # _ensure_dir so it correctly skips directory creation.
            self.db_path = Path(":memory:")
        else:
            self.db_path = Path(self._db_path_raw)

        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._initialized = False

    def _ensure_dir(self) -> None:
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection with Row factory.

        When ``db_path`` is a SQLite URI (``file:...``), opens with ``uri=True``
        so that shared-cache in-memory databases (``?cache=shared``) are
        accessible across multiple threads via the same URI name.
        """
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._ensure_dir()
            if self._is_uri:
                conn = sqlite3.connect(
                    self._db_path_raw,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                    timeout=10.0,
                    uri=True,
                )
            else:
                conn = sqlite3.connect(
                    str(self.db_path),
                    detect_types=sqlite3.PARSE_DECLTYPES,
                    timeout=10.0,
                )
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._conn_lock:
                self._connections.append(conn)
        return self._local.conn

    def initialize(self) -> None:
        """Initialize tables and indexes idempotently."""
        with self._init_lock:
            conn = self.get_connection()
            with conn:
                conn.executescript(INIT_SQL)
                # Check migration version
                cur = conn.cursor()
                cur.execute("SELECT version FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
                    )
            self._initialized = True
            log.info("database initialized at %s (schema v%s)", self.db_path, SCHEMA_VERSION)

    def close(self) -> None:
        """Close all connections across all threads."""
        import gc

        if hasattr(self, "_associated_client") and self._associated_client is not None:
            try:
                self._associated_client.stop()
            except Exception:
                pass
            self._associated_client = None

        with self._conn_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        if hasattr(self._local, "conn"):
            self._local.conn = None
        gc.collect()

    def insert_telemetry(self, msg: TelemetryMessage) -> int:
        """Insert a validated TelemetryMessage. Returns the auto-increment row id."""
        if not self._initialized:
            self.initialize()

        conn = self.get_connection()
        gps = msg.gps
        gps_fix = 1 if (gps and gps.fix) else 0
        lat = gps.latitude if gps else None
        lon = gps.longitude if gps else None
        sats = gps.satellites if gps else None
        alt = gps.altitude_m if gps else None
        spd = gps.speed_mps if gps else None

        sql = """
            INSERT INTO telemetry (
                schema_version, device_id, source, received_at, device_timestamp,
                temperature_c, humidity_pct, light_lux, mq135_raw, distance_cm,
                gps_fix, gps_lat, gps_lon, gps_satellites, gps_altitude_m, gps_speed_mps,
                raw_payload
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?
            )
        """
        params = (
            msg.schema_version,
            msg.device_id,
            msg.source,
            _iso(msg.received_at),
            _iso(msg.timestamp),
            msg.temperature_c,
            msg.humidity_pct,
            msg.light_lux,
            msg.mq135_raw,
            None,  # distance_cm: HC-SR04 removed from hardware; column kept for backward compatibility
            gps_fix,
            lat,
            lon,
            sats,
            alt,
            spd,
            msg.raw_payload,
        )
        with conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            row_id = cur.lastrowid
            log.debug("inserted telemetry row id=%s device=%s", row_id, msg.device_id)

        # Automatically update persistent last-known GPS location when valid fix is present
        if gps_fix == 1 and lat is not None and lon is not None:
            try:
                self.save_last_known_gps(
                    device_id=msg.device_id,
                    latitude=lat,
                    longitude=lon,
                    last_fix_at=msg.received_at,
                )
            except Exception:
                log.exception("failed to persist GPS coordinates for device %s", msg.device_id)

        return row_id

    def insert_status(self, msg: StatusMessage) -> int:
        """Insert a validated StatusMessage. Returns the auto-increment row id."""
        if not self._initialized:
            self.initialize()

        conn = self.get_connection()
        sql = """
            INSERT INTO device_status (
                schema_version, device_id, status, received_at, device_timestamp,
                ip_address, wifi_rssi_dbm, uptime_s, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            msg.schema_version,
            msg.device_id,
            msg.status,
            _iso(msg.received_at),
            _iso(msg.timestamp),
            msg.ip_address,
            msg.wifi_rssi_dbm,
            msg.uptime_s,
            msg.raw_payload,
        )
        with conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur.lastrowid

    def get_latest_telemetry(self, device_id: str | None = None) -> TelemetryMessage | None:
        """Retrieve the most recent telemetry reading by received_at."""
        if not self._initialized:
            self.initialize()

        conn = self.get_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "SELECT * FROM telemetry WHERE device_id = ? ORDER BY received_at DESC, id DESC LIMIT 1",
                (device_id,),
            )
        else:
            cur.execute("SELECT * FROM telemetry ORDER BY received_at DESC, id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_telemetry(row)

    def get_telemetry_history(
        self,
        device_id: str | None = None,
        limit: int = DEFAULT_QUERY_LIMIT,
        since: datetime | None = None,
    ) -> list[TelemetryMessage]:
        """Retrieve bounded historical telemetry ordered by received_at DESC.

        limit is capped at MAX_QUERY_LIMIT (1000).
        """
        if not self._initialized:
            self.initialize()

        capped_limit = max(1, min(limit, MAX_QUERY_LIMIT))
        conn = self.get_connection()
        cur = conn.cursor()

        conditions: list[str] = []
        params: list[Any] = []

        if device_id:
            conditions.append("device_id = ?")
            params.append(device_id)

        if since:
            conditions.append("received_at >= ?")
            params.append(_iso(since))

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM telemetry {where_clause} ORDER BY received_at DESC, id DESC LIMIT ?"
        params.append(capped_limit)

        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [self._row_to_telemetry(r) for r in rows]

    def get_latest_status(self, device_id: str | None = None) -> StatusMessage | None:
        """Retrieve latest status for a device or overall latest status."""
        if not self._initialized:
            self.initialize()

        conn = self.get_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "SELECT * FROM device_status WHERE device_id = ? ORDER BY received_at DESC, id DESC LIMIT 1",
                (device_id,),
            )
        else:
            cur.execute(
                "SELECT * FROM device_status ORDER BY received_at DESC, id DESC LIMIT 1"
            )
        row = cur.fetchone()
        if not row:
            return None
        return StatusMessage(
            schema_version=row["schema_version"],
            device_id=row["device_id"],
            status=row["status"],
            received_at=_from_iso(row["received_at"]),
            ip_address=row["ip_address"],
            wifi_rssi_dbm=row["wifi_rssi_dbm"],
            uptime_s=row["uptime_s"],
            timestamp=_from_iso(row["device_timestamp"]),
            raw_payload=row["raw_payload"] or "",
        )

    def count_telemetry(self, device_id: str | None = None) -> int:
        """Return count of stored telemetry records."""
        if not self._initialized:
            self.initialize()

        conn = self.get_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute("SELECT COUNT(*) FROM telemetry WHERE device_id = ?", (device_id,))
        else:
            cur.execute("SELECT COUNT(*) FROM telemetry")
        return cur.fetchone()[0]

    def save_last_known_gps(
        self,
        device_id: str,
        latitude: float,
        longitude: float,
        last_fix_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Save or update the persistent last known valid GPS location for a device."""
        if not self._initialized:
            self.initialize()
        now = datetime.now(timezone.utc)
        fix_dt = last_fix_at or now
        upd_dt = updated_at or now
        conn = self.get_connection()
        sql = """
            INSERT INTO device_gps_location (device_id, latitude, longitude, last_fix_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                last_fix_at = excluded.last_fix_at,
                updated_at = excluded.updated_at
        """
        with conn:
            conn.execute(sql, (device_id, float(latitude), float(longitude), _iso(fix_dt), _iso(upd_dt)))
        log.debug("saved persistent GPS location for device=%s lat=%.4f lon=%.4f", device_id, latitude, longitude)

    def get_last_known_gps(self, device_id: str | None = None) -> dict[str, Any] | None:
        """Retrieve the persistent last known valid GPS location for a device, or the most recent overall."""
        if not self._initialized:
            self.initialize()
        conn = self.get_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "SELECT device_id, latitude, longitude, last_fix_at, updated_at FROM device_gps_location WHERE device_id = ?",
                (device_id,),
            )
        else:
            cur.execute(
                "SELECT device_id, latitude, longitude, last_fix_at, updated_at FROM device_gps_location ORDER BY updated_at DESC LIMIT 1"
            )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "device_id": row["device_id"],
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "last_fix_at": _from_iso(row["last_fix_at"]),
            "updated_at": _from_iso(row["updated_at"]),
        }

    def record_ai_call_attempt(self, device_id: str, attempt_time: datetime | None = None) -> None:
        """Record the timestamp of an actual AI call attempt (including failed attempts) to enforce cooldown."""
        if not self._initialized:
            self.initialize()
        now = attempt_time or datetime.now(timezone.utc)
        conn = self.get_connection()
        sql = """
            INSERT INTO device_advice (device_id, advice_mode, provider, created_at, last_ai_call_at, payload)
            VALUES (?, 'PENDING', 'attempt', ?, ?, '{}')
            ON CONFLICT(device_id) DO UPDATE SET
                last_ai_call_at = excluded.last_ai_call_at
        """
        with conn:
            conn.execute(sql, (device_id, _iso(now), _iso(now)))
        log.debug("recorded AI call attempt for device=%s at=%s", device_id, _iso(now))

    def get_last_ai_call_at(self, device_id: str) -> datetime | None:
        """Retrieve the timestamp of the last AI call attempt for a device."""
        if not self._initialized:
            self.initialize()
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT last_ai_call_at FROM device_advice WHERE device_id = ?", (device_id,))
        row = cur.fetchone()
        if not row or not row["last_ai_call_at"]:
            return None
        return _from_iso(row["last_ai_call_at"])

    def save_latest_advice(
        self,
        device_id: str,
        advice_payload: dict[str, Any],
        created_at: datetime | None = None,
        last_ai_call_at: datetime | None = None,
    ) -> None:
        """Save latest advice payload for a device."""
        import json
        if not self._initialized:
            self.initialize()
        now = created_at or datetime.now(timezone.utc)
        payload_str = json.dumps(advice_payload)
        mode = advice_payload.get("advice_mode", "UNKNOWN")
        provider = advice_payload.get("provider", "UNKNOWN")
        conn = self.get_connection()

        existing_call_at = self.get_last_ai_call_at(device_id)
        effective_ai_call = last_ai_call_at or existing_call_at

        sql = """
            INSERT INTO device_advice (device_id, advice_mode, provider, created_at, last_ai_call_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                advice_mode = excluded.advice_mode,
                provider = excluded.provider,
                created_at = excluded.created_at,
                last_ai_call_at = COALESCE(excluded.last_ai_call_at, device_advice.last_ai_call_at),
                payload = excluded.payload
        """
        with conn:
            conn.execute(
                sql,
                (device_id, mode, provider, _iso(now), _iso(effective_ai_call), payload_str),
            )
        log.debug("saved advice for device=%s mode=%s provider=%s", device_id, mode, provider)

    def get_latest_advice(self, device_id: str | None = None) -> dict[str, Any] | None:
        """Retrieve the latest advice payload for a device, or most recent overall."""
        import json
        if not self._initialized:
            self.initialize()
        conn = self.get_connection()
        cur = conn.cursor()
        if device_id:
            cur.execute(
                "SELECT payload FROM device_advice WHERE device_id = ? AND payload != '{}' ORDER BY created_at DESC LIMIT 1",
                (device_id,),
            )
        else:
            cur.execute(
                "SELECT payload FROM device_advice WHERE payload != '{}' ORDER BY created_at DESC LIMIT 1"
            )
        row = cur.fetchone()
        if not row or not row["payload"]:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:
            log.exception("failed to parse stored advice payload")
            return None

    @staticmethod
    def _row_to_telemetry(row: sqlite3.Row) -> TelemetryMessage:
        gps_fix = bool(row["gps_fix"])
        gps = GPSData(
            fix=gps_fix,
            latitude=row["gps_lat"],
            longitude=row["gps_lon"],
            satellites=row["gps_satellites"],
            altitude_m=row["gps_altitude_m"],
            speed_mps=row["gps_speed_mps"],
        )
        return TelemetryMessage(
            schema_version=row["schema_version"],
            device_id=row["device_id"],
            source=row["source"],
            received_at=_from_iso(row["received_at"]),
            timestamp=_from_iso(row["device_timestamp"]),
            temperature_c=row["temperature_c"],
            humidity_pct=row["humidity_pct"],
            light_lux=row["light_lux"],
            mq135_raw=row["mq135_raw"],
            gps=gps,
            raw_payload=row["raw_payload"] or "",
        )
