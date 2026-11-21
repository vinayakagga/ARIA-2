# ARIA — MQTT Topic & Payload Contract (Version 1)

**Document Status:** Formalized Specification (Phase 2)  
**Last Updated:** 2026-11-15  
**Applies To:** PC Backend, ESP32 Firmware, Dashboard  

---

## 1. Overview & Protocol Policy

ARIA uses standard MQTT over TCP on the local Wi-Fi network.

- **Broker:** Eclipse Mosquitto on PC (`127.0.0.1:1883` locally; PC LAN IP for ESP32).
- **Protocol Version:** MQTT v3.1.1 / v5.
- **Client Library:** Paho MQTT (Python callback API v2 on PC).
- **Base Topic Tree:** `aria/v1/#`

### Quality of Service (QoS) & Retain Policy

| Topic | Direction | Recommended QoS | Retain Policy | Rationale |
|---|---|---|---|---|
| `aria/v1/telemetry` | Device → PC | 0 | **Do NOT retain (`retain=false`)** | Telemetry is high-frequency streaming sensor data. Retaining stale telemetry risks a newly connected subscriber displaying old data as live. |
| `aria/v1/status` | Device → PC | 1 | **Retain (`retain=true`)** | Status represents last known device availability (online/offline). Combined with MQTT Last Will and Testament (LWT), retains current device presence. |
| `aria/v1/command` | PC → Device | 1 | **Strictly NEVER retain (`retain=false`)** | **SAFETY CRITICAL.** Stale movement commands must NEVER cause unexpected rover movement upon device reboot or reconnect. |
| `aria/v1/ack` | Device → PC | 1 | **Do NOT retain (`retain=false`)** | Ephemeral receipt confirmation for discrete commands. |

---

## 2. Topic: `aria/v1/telemetry`

Published periodically by the device (or development simulator) to report environmental and rover state.

### 2.1 Payload Structure (JSON)

```json
{
  "schema_version": 1,
  "device_id": "aria-c3",
  "source": "simulated",
  "timestamp": "2026-11-15T02:00:00Z",
  "temperature_c": 28.4,
  "humidity_pct": 62.0,
  "light_lux": 340.0,
  "mq135_raw": 1250,
  "gps": {
    "fix": false,
    "latitude": null,
    "longitude": null,
    "satellites": 0,
    "altitude_m": null,
    "speed_mps": null
  }
}
```

### 2.2 Field Definitions & Validation Rules

| Field | Type | Required? | Nullable? | Valid Range / Format | Description |
|---|---|---|---|---|---|
| `schema_version` | integer | **Yes** | No | `1` | Schema version identifier. Unsupported versions are rejected. |
| `device_id` | string | **Yes** | No | 1–64 characters `[a-zA-Z0-9_-]` | Device identity (e.g. `aria-c3`, `aria-devkit`). |
| `source` | string | **Yes** | No | `"simulated"` or `"real"` | Data provenance. Must distinguish simulator from physical hardware. |
| `timestamp` | string | Optional | Yes | ISO 8601 UTC (e.g. `YYYY-MM-DDTHH:MM:SSZ`) | Device generation time in UTC. If omitted/null (e.g. RTC uncalibrated), backend stamps `received_at`. |
| `temperature_c` | float/int | Optional | Yes | -40.0 to +80.0 °C | DHT22 temperature. |
| `humidity_pct` | float/int | Optional | Yes | 0.0 to 100.0 % | DHT22 relative humidity. |
| `light_lux` | float/int | Optional | Yes | >= 0.0 lux | BH1750 ambient light. |
| `mq135_raw` | integer | Optional | Yes | 0 to 4095 | ESP32 12-bit ADC raw reading. **Uncalibrated. Never label as ppm or CO2.** |
| `gps` | object | Optional | Yes | See GPS sub-object | GPS receiver state. |

*Note on HC-SR04 removal:* The ultrasonic distance sensor (`distance_cm`) has been permanently removed from hardware. Any legacy payload including `distance_cm` is safely parsed without error, but the field is no longer validated, stored in active message models, or rendered in dashboard charts.

### 2.3 GPS Sub-Object Rules

| Sub-field | Type | Required? | Nullable? | Rules |
|---|---|---|---|---|
| `gps.fix` | boolean | **Yes** | No | `true` if GPS has 2D/3D fix; `false` otherwise. |
| `gps.latitude` | float | Conditional | Yes | -90.0 to +90.0. **Must be null if `fix=false`; must be valid float if `fix=true`.** |
| `gps.longitude` | float | Conditional | Yes | -180.0 to +180.0. **Must be null if `fix=false`; must be valid float if `fix=true`.** |
| `gps.satellites` | integer | Optional | Yes | >= 0. Number of tracked satellites. |
| `gps.altitude_m` | float/int | Optional | Yes | Altitude in meters above sea level. |
| `gps.speed_mps` | float/int | Optional | Yes | Speed over ground in meters per second. |

### 2.4 Sensor Nullability vs. Malformed Payloads

- **Unavailable Sensor (Valid Payload):** If a sensor fails to read, disconnected, or lacks fix, its key must be `null` (or omitted). The backend accepts this and records the value as unavailable.
- **Malformed Payload (Quarantined):** Payloads with invalid JSON syntax, wrong types (e.g. string `"28.4"` for temperature), out-of-range values, invalid GPS fix/coordinate combinations, or missing required fields (`schema_version`, `device_id`, `source`) are quarantined and logged without crashing the backend.

---

## 3. Topic: `aria/v1/status`

Published by the device to declare its operational status and network health.

### 3.1 Payload Structure (JSON)

```json
{
  "schema_version": 1,
  "device_id": "aria-c3",
  "status": "online",
  "ip_address": "192.168.1.105",
  "wifi_rssi_dbm": -65,
  "uptime_s": 3600,
  "timestamp": "2026-11-15T02:00:00Z"
}
```

### 3.2 Field Definitions & Rules

| Field | Type | Required? | Nullable? | Valid Values / Range |
|---|---|---|---|---|
| `schema_version` | integer | **Yes** | No | `1` |
| `device_id` | string | **Yes** | No | 1–64 characters `[a-zA-Z0-9_-]` |
| `status` | string | **Yes** | No | `"online"`, `"offline"`, `"degraded"`, `"error"` |
| `ip_address` | string | Optional | Yes | Valid IPv4 string |
| `wifi_rssi_dbm` | integer | Optional | Yes | -120 to 0 dBm |
| `uptime_s` | integer | Optional | Yes | >= 0 seconds |
| `timestamp` | string | Optional | Yes | ISO 8601 UTC string |

### 3.3 Last Will and Testament (LWT)

Before connecting, the device configures an MQTT Last Will and Testament:
- **Topic:** `aria/v1/status`
- **QoS:** 1
- **Retain:** `true`
- **Payload:**
  ```json
  {
    "schema_version": 1,
    "device_id": "aria-c3",
    "status": "offline",
    "timestamp": null
  }
  ```
When the device connects normally, it publishes `status="online"` with `retain=true` to update the retained state.

---

## 4. Topic: `aria/v1/command`

**Implemented in Phase 7.** Used by PC backend / operator dashboard to stream motor drive instructions to the rover's L298N dual DC motor controller.

- **Publisher:** PC Backend (`POST /api/v1/command`)
- **Subscriber:** ESP32 Rover Firmware
- **QoS:** 1 (acknowledged delivery)
- **Retain Policy:** **Strictly NEVER retain (`retain=false`)**. Stale movement commands must NEVER cause unexpected rover movement upon device boot or Wi-Fi reconnect.
- **Payload Structure (JSON):**
  ```json
  {
    "command": "forward"
  }
  ```
- **Allowed Command Values:**
  - `"forward"` — Drive both wheels forward.
  - `"reverse"` — Drive both wheels in reverse.
  - `"left"` — Turn rover left (counter-rotate or differential turn).
  - `"right"` — Turn rover right (counter-rotate or differential turn).
  - `"stop"` — Immediately halt all motor drive signals.
- **Streaming & Safety Rules:**
  - Operator holds down a direction control on the dashboard; the dashboard streams command packets at ~200 ms intervals (`STREAM_INTERVAL_MS = 200`).
  - Releasing control, window blur, tab switching, or pointer cancellation immediately dispatches `{"command": "stop"}`.
  - **ESP32 Hardware Watchdog Timeout:** The ESP32 firmware enforces an independent 500 ms command watchdog. If no valid movement packet arrives within 500 ms, the ESP32 automatically halts all motor drive signals.
  - STOP takes absolute priority at all times.
  - Movement commands are rejected/disabled if the rover device is offline.
  - No queuing or automatic resumption upon reconnect.

---

## 5. Topic: `aria/v1/ack` (Future Reference)

*Not implemented in Phase 2; documented for architectural completeness.*

- **QoS:** 1
- **Retain:** `false`.
- **Payload Structure:**
  ```json
  {
    "schema_version": 1,
    "device_id": "aria-c3",
    "command_id": "cmd-00123",
    "status": "accepted",
    "message": "motor stopped",
    "timestamp": "2026-11-15T02:00:00.100Z"
  }
  ```
