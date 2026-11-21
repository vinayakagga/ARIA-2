# WORKDONE — ARIA

This file records verified project progress. Do not mark work complete based only on code being written.

## Current status
- Current phase: Phase 0 environment audit / Phase 1 Python MQTT receiver.
- Last verified milestone: Local Mosquitto CLI publisher/subscriber test passed on Windows.
- Python virtual environment and Python MQTT receiver: NOT VERIFIED.
- ESP32-C3 firmware: NOT STARTED.
- Flask, SQLite and dashboard: NOT STARTED.
- Final ESP32 board and rover electrical design: NOT VERIFIED.

## Session log

### 2026-11-14 — Initial project setup
- Confirmed Mosquitto installed on Windows 11.
- Confirmed local CLI pub/sub test on topic `aria/test`.
- Selected temporary ESP32-C3 Mini publisher approach.
- Arduino IDE remains the selected device IDE.
- Created project-level specification and Cursor build brief.

## Current next action
1. Inspect `C:\ARIA` and Git status; do not overwrite existing files.
2. Confirm Python 3.12 is available.
3. Create/activate `.venv` if missing.
4. Install Paho MQTT in `.venv`, pin the tested version.
5. Implement minimal Python subscriber for `aria/#`.
6. Publish a CLI test message to `aria/v1/telemetry`.
7. Record the actual result here. Do not proceed to Flask/database/dashboard until the user approves.
