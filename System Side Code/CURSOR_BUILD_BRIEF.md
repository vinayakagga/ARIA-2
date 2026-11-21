# ARIA — Cursor Build Brief

**Purpose:** This document is the implementation contract for Cursor. Read it together with `docs/PROJECT_SPEC.md` and `WORKDONE.md` before changing code.

## 1. Product definition

Build ARIA as a local-network, manually operated 2WD environmental-monitoring rover with a PC-hosted control station.

- PC: Windows 11, acts as MQTT broker host, Python backend, database host and dashboard server.
- Temporary device: ESP32-C3 Mini, initially used only as a simulated MQTT telemetry publisher and safe command-path test device.
- Planned final device: standard ESP32 DevKit, not yet physically verified.
- Device development: Arduino IDE, Arduino framework, C++.
- PC backend: Python 3.12, Flask, Paho MQTT, SQLite.
- Browser UI: HTML, CSS and vanilla JavaScript; Chart.js for charts; Leaflet for GPS map.
- MQTT broker: installed Mosquitto on the PC.
- Network: user's existing Wi-Fi router; no cloud dependency.

The current operating mode is manual control only. Do not implement autonomous navigation.

## 2. Non-negotiable scope constraints

Do not add these unless the user explicitly approves a scope change:

- Autonomous navigation or autonomous obstacle avoidance.
- Servo.
- Soil-moisture sensor.
- Cloud services, hosted databases, cloud MQTT or paid APIs.
- Crop disease prediction, soil-health diagnosis, or calibrated pollution/CO2 claims.
- Extra hardware or sensors not listed in `docs/PROJECT_SPEC.md`.

The HC-SR04 is for front distance measurement and a forward-motion safety inhibit only. It is not an autonomous navigation sensor.

MQ-135 values must be labelled as raw/un calibrated readings unless a documented calibration procedure and validated conversion are later supplied. Do not label raw ADC values as ppm, CO2, or a certified air-quality index.

Simulated data must always be visibly labelled as simulated. Never display simulated values as live physical measurements.

## 3. Source-of-truth order

Before each task, inspect:

1. `docs/PROJECT_SPEC.md` — project scope and decisions.
2. `WORKDONE.md` — latest verified state and next task.
3. Existing code and tests — actual repository state.

If documents conflict, do not silently choose. Report the conflict and ask the user. Newer explicit user decisions override older documentation; update the relevant document after approval.

Never infer that a task is complete because a file exists. Verify it by running the relevant command/test.

## 4. Required engineering behaviour

- Work in small, reviewable phases. Do not build the whole product in one pass.
- Before editing, state the phase goal, files expected to change, and acceptance tests.
- Inspect the existing repository before creating or overwriting files.
- Preserve working code unless a change is necessary and explained.
- Do not invent pin mappings, board revisions, wiring, electrical ratings, calibration constants, test results, or hardware capabilities.
- Treat unknown facts as unknown. Ask the user or create a clearly marked TODO requiring physical verification.
- Do not run destructive commands, delete user files, overwrite existing work, or change system configuration without explicit approval.
- Do not install global Python packages. Use the project virtual environment.
- Do not add dependencies without explaining why and recording them in the relevant requirements/lock file.
- Prefer simple, maintained, well-documented libraries and avoid unnecessary frameworks.
- Keep secrets and Wi-Fi credentials out of source control. Use a local `.env` file if needed and provide `.env.example` with placeholders.
- Validate all incoming MQTT payloads. Malformed messages must not crash the backend.
- Use timestamps consistently. Store UTC timestamps in the database and convert to local display time in the UI.
- Use parameterized SQL. Never construct SQL using string concatenation with user input.
- Keep MQTT callbacks lightweight. Do not perform slow database or web work directly inside callbacks if it can block network processing.
- Log errors with enough context to diagnose them, but do not log passwords or secrets.
- Do not claim offline map tiles are available. Leaflet may load map tiles from the internet unless an offline tile source is separately configured.
- Explain meaningful implementation choices in plain language so the user can understand and maintain the system.

## 5. Approved software tools

Use this stack unless the user approves a change:

| Area | Approved tool | Requirement |
|---|---|---|
| Editor/agent | Cursor | Use Agent for scoped implementation; review diffs before accepting |
| Device IDE | Arduino IDE | Arduino framework and C++ |
| PC OS | Windows 11 | Commands must work in PowerShell |
| Runtime | Python 3.12 | Use `.venv` inside project |
| MQTT broker | Eclipse Mosquitto | Already installed; local broker test passed |
| Python MQTT client | Paho MQTT | Use callback API v2; pin tested version in requirements |
| Web backend | Flask | Keep API small and explicit |
| Database | SQLite | Built into Python; no separate server |
| Frontend | HTML, CSS, vanilla JavaScript | Avoid React/Vue unless user approves |
| Charts | Chart.js | Use for time-series display |
| Map | Leaflet | Show GPS only when fix is valid |
| Version control | Git | Small commits after verified milestones |

Do not introduce Docker, Redis, PostgreSQL, Node build tooling, React, Celery, cloud services or a frontend framework for this project unless the user explicitly requests it and the reason is documented.

## 6. Repository layout

Create only what the current phase needs. Target layout:

```text
ARIA/
├── .cursor/
│   └── rules/
│       └── aria.mdc
├── docs/
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   ├── MQTT_CONTRACT.md
│   ├── TEST_RESULTS.md
│   └── PIN_MAP.md          # only after exact board is verified
├── firmware/
│   └── c3_simulator/       # temporary C3 publisher sketch
├── server/
│   ├── app.py
│   ├── mqtt_client.py
│   ├── telemetry.py
│   ├── database.py
│   ├── command_handler.py
│   └── requirements.txt
├── dashboard/
│   ├── templates/
│   └── static/
│       ├── css/
│       ├── js/
│       └── images/
├── data/                   # runtime database; gitignored
├── tests/
├── .env.example
├── .gitignore
├── WORKDONE.md
└── README.md
```

Do not create empty placeholder modules for every future feature. Add files only when the corresponding phase begins.

## 7. MQTT contract — initial contract to formalize

Use these topic names:

| Topic | Direction | Meaning |
|---|---|---|
| `aria/v1/telemetry` | Device → PC | Sensor/GPS readings |
| `aria/v1/status` | Device → PC | Online/health state |
| `aria/v1/command` | PC → Device | Manual movement/stop command |
| `aria/v1/ack` | Device → PC | Command acknowledgement |

The backend may subscribe to `aria/v1/#`. Do not retain movement commands. Telemetry/status retain behaviour must be explicitly decided and documented rather than assumed.

Initial telemetry example:

```json
{
  "schema_version": 1,
  "device_id": "aria-c3",
  "source": "simulated",
  "timestamp": "2026-11-14T12:00:00Z",
  "temperature_c": 28.4,
  "humidity_pct": 62.0,
  "light_lux": 340.0,
  "mq135_raw": 1250,
  "distance_cm": 85.0,
  "gps": {
    "fix": false,
    "latitude": null,
    "longitude": null
  }
}
```

This is a proposed schema example, not a measured reading. Before implementation, write `docs/MQTT_CONTRACT.md` defining required/optional fields, types, units, null behaviour, timestamp format, source values, schema versioning, topic QoS and retained-message policy. Ask the user if a product decision is needed; do not silently make a consequential choice.

The backend must reject or quarantine invalid payloads without crashing. It must distinguish malformed messages from valid messages with unavailable sensor fields.

## 8. Safety requirements for the future motor command path

The initial PC backend and C3 publisher phase must not energize motors.

When motor control is later implemented:

- UI controls are press-and-hold; release sends stop.
- Device-side timeout stops motors if fresh valid movement commands stop arriving.
- Wi-Fi/MQTT loss, invalid commands, stale commands and emergency stop result in stop.
- Stop has priority over movement.
- Forward motion is inhibited if the front distance sensor is below a user-approved threshold or its reading is invalid/stale, once that policy is agreed.
- Movement commands are never retained.
- Device must not resume movement automatically after reconnect.
- A physical motor-power disconnect is recommended.
- Test command parsing and acknowledgements with motors disconnected before connecting motor power.

The approximately 500 ms timeout mentioned in project discussion is only a provisional target, not a frozen requirement. Ask before treating it as final.

No final motor pin map or power wiring exists. Do not generate final motor firmware or wiring instructions until exact board, L298N module, motors, battery and buck converter have been inspected and verified.

## 9. Phased implementation plan and acceptance criteria

### Phase 0 — Repository and environment audit
- Inspect current files and Git status.
- Confirm Python version and whether `C:\ARIA\.venv` exists.
- Confirm Mosquitto service/process and local port 1883 availability.
- Do not overwrite or delete existing files.

**Accept when:** a concise environment report is produced and the next step is clear.

### Phase 1 — Python MQTT receiver (current next phase)
- Create/activate `C:\ARIA\.venv` using Python 3.12.
- Install Paho MQTT in the venv and pin the tested version.
- Implement a minimal receiver subscribing to `aria/#`.
- Use Paho callback API v2.
- Handle connection failures and malformed UTF-8 safely.
- Provide a clean Ctrl+C shutdown.

**Accept when:** a message published with Mosquitto CLI to `aria/v1/telemetry` is printed by the Python process. Record exact commands and observed output. Do not proceed based only on code inspection.

### Phase 2 — MQTT contract and backend foundation
- Formalize schema and topic behaviour.
- Separate MQTT transport, payload validation and application logic.
- Add unit tests for valid, missing, null, wrong-type and malformed payloads.
- Add structured logs.

**Accept when:** tests pass and sample messages are validated consistently.

### Phase 3 — SQLite persistence
- Define schema and migrations/initialization.
- Store received time and device timestamp separately.
- Store source/device identity and sensor fields.
- Handle duplicate/out-of-order timestamps deliberately.
- Add bounded queries for recent history.

**Accept when:** automated tests prove insert, query, null handling and database restart persistence.

### Phase 4 — Flask API
- Provide health/status, latest telemetry and historical telemetry endpoints.
- Validate query parameters and cap result sizes.
- Return predictable JSON errors.
- Keep MQTT networking separate from Flask request handling.

**Accept when:** endpoint tests pass and API responses are documented.

### Phase 5 — Dashboard
- Display broker/backend/device connection state distinctly.
- Display simulated/real source clearly.
- Show latest values with units and unavailable states.
- Plot historical readings.
- Show GPS marker only for a valid fix.
- Show a clear stale-data state; do not leave old values looking live.

**Accept when:** dashboard works locally at `http://localhost:5000` and handles no data, stale data and simulated data.

### Phase 6 — ESP32-C3 simulated publisher
- Use Arduino IDE and verified C3 board package/board selection.
- Verify exact C3 board pinout before assigning pins; no sensors or motors required for this phase.
- Connect to the user's Wi-Fi using credentials stored outside committed source.
- Publish clearly labelled simulated telemetry at a modest configurable interval.
- Publish status and reconnect safely.
- No motor outputs.

**Accept when:** backend and dashboard receive the C3 messages, source is visibly simulated, and reconnect behaviour is tested.

### Phase 7 — Command-path simulation
- Add safe test commands and acknowledgements without motor wiring.
- Validate command IDs, expiry and allowed command values.
- Never retain movement commands.
- Test disconnect and stale-command handling.

**Accept when:** command flow is demonstrated with no motor power connected.

### Phase 8 — Hardware audit and real sensor integration
- Inspect actual board and every module.
- Confirm power, signal levels, wiring and pin map.
- Test sensors individually.
- Do not reuse a classic ESP32 pin map on C3.
- Do not connect motor supply until electrical ratings and safety checks are complete.

**Accept when:** verified wiring and test records exist for each sensor and supply.

### Phase 9 — Motor control and rover validation
- Implement only after hardware audit.
- Test with wheels lifted and motor power physically controllable.
- Verify stop, timeout, disconnect, emergency stop and proximity inhibit.
- Perform controlled low-speed tests.

**Accept when:** every safety test is observed and recorded.

## 10. Required tests and quality bar

At minimum, add automated tests for:
- Valid telemetry payload.
- Missing optional sensor value.
- Null GPS coordinates when fix is false.
- Invalid JSON.
- Wrong field types.
- Unknown device ID handling.
- Database insert/query and restart persistence.
- API no-data and invalid-query behaviour.
- Stale telemetry indication.
- Command validation and expiry before any motor integration.

Do not write tests that merely assert the implementation's own assumptions. Tests must reflect the documented contract.

## 11. Documentation and checkpoint requirements

After each meaningful phase, update `WORKDONE.md` with:
- Date/time.
- Phase.
- Files created/modified.
- Commands actually run.
- Tests actually executed and their observed result.
- Known bugs and unresolved questions.
- Exact next action.

Update the relevant architecture/schema/test docs when decisions change. Keep `PROJECT_SPEC.md` as the product-level source of truth.

## 12. Cursor execution protocol

For each task, Cursor must:

1. Read `docs/PROJECT_SPEC.md`, `WORKDONE.md`, and this brief.
2. Inspect the existing code and repository state.
3. State the narrow task and acceptance criteria.
4. List files it intends to create/change.
5. Implement only that task.
6. Run the relevant tests/commands.
7. Report exact outcomes, including failures.
8. Update `WORKDONE.md`.
9. Stop and ask before moving to the next phase.

If the task depends on missing hardware facts, stop and ask. If an acceptance test cannot be run, mark it **NOT VERIFIED** rather than claiming success.

**Current authorised task:** Phase 0 audit, then Phase 1 Python MQTT receiver only. Do not implement Flask, database, dashboard, C3 firmware or motor control until Phase 1 is verified and the user authorises the next phase.
