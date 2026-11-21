# WORKDONE — ARIA

This file records verified project progress. Do not mark work complete based only on code being written.

## Current status
- Current phase: Advice Engine: Live Telemetry & Advice Synchronization Audit (Steps 1–5) — VERIFIED.
- Last verified milestone: Dashboard Telemetry Freshness & GPS Provenance Synchronization (`dashboard/templates/index.html`, `dashboard/static/css/dashboard.css`, `dashboard/static/js/dashboard.js`), strict TLS verification in `server/weather.py` via `certifi`, MQ-135 wording audit in `server/rules.py`, telemetry context and GPS provenance API integration in `server/advice_engine.py`, and all 179 automated tests passing across Phases 1–8.
- Python virtual environment and Python MQTT receiver: VERIFIED (Python 3.12.9, Paho MQTT 2.2.0, callback API v2).
- Telemetry contract formalization & validation (Phase 2): VERIFIED.
- SQLite persistence (Phase 3): VERIFIED.
- Flask API & endpoints (Phase 4): VERIFIED (Flask 3.1.3).
- Dashboard (Phase 5): VERIFIED.
- MQTT ↔ Flask ingestion integration (Phase 6): VERIFIED.
- Motor Control & Agricultural Redesign (Phase 7): VERIFIED via automated software test suite.
- Advice Engine Rules Engine (Phase 8 - Stage 1): VERIFIED.
- Advice Engine Weather Service (Phase 8 - Stage 2): VERIFIED.
- Advice Engine NVIDIA NIM Client (Phase 8 - Stage 3): VERIFIED.
- Advice Engine Aggregation & Flask API (Phase 8 - Stage 4): VERIFIED.
- Advice Engine Dashboard Integration (Phase 8 - Stage 5): VERIFIED.
- Live Telemetry & Advice Synchronization Audit: VERIFIED (179 automated tests passing).
- Physical Motor Hardware Verification: NOT PERFORMED (hardware rover execution pending ESP32 motor driver wiring and firmware flash).
- ESP32-C3 firmware: NOT MODIFIED.
- Final ESP32 board and rover electrical design: NOT VERIFIED.

---

## Session log

### 2026-11-17 — Advice Engine: Live Telemetry Freshness, GPS Provenance, and TLS Verification Audit

#### 1. What Changed

1. **`server/requirements.txt`**:
   - Added `certifi>=2024.2.2` to provide a verified CA bundle across platforms.
2. **`server/weather.py`**:
   - Implemented `_build_ssl_context()` utilizing `certifi.where()` as the default trusted CA bundle.
   - Enforced strict TLS verification (`ssl.CERT_REQUIRED`, `check_hostname=True`).
   - Removed all instances of unverified SSL contexts (`ssl._create_unverified_context()`).
   - Retained graceful degradation so that weather service connectivity failures do not crash the engine or prevent advice generation.
3. **`server/rules.py`**:
   - Audited MQ-135 wording in `ENV_MQ135_BASELINE` and `ENV_MQ135_ELEVATED` observations.
   - Explicitly clarified that uncalibrated ADC counts cannot establish "normal air quality" or confirm specific pollution levels without gas-specific calibration and a validated baseline.
4. **`server/advice_engine.py`**:
   - Added metadata tracking to advice generation payload:
     - `telemetry_context`: includes `telemetry_timestamp`, `telemetry_age_seconds`, `telemetry_freshness` (`current` vs `stale` based on `telemetry_stale_seconds` threshold), and `snapshot` of evaluated readings.
     - `gps_provenance`: distinguishes `current_fix` (active GPS lock on evaluated telemetry), `last_known_fix` (historical persisted GPS fix), or `unavailable`.
   - Propagates unexpected runtime/database crashes without overwriting or updating SQLite with corrupt advice state.
5. **`dashboard/templates/index.html`**:
   - Added DOM elements for advice freshness and provenance tracking:
     - `#adviceStaleTelemetryNotice`: dynamic stale advice warning banner alerting users when live telemetry is newer than the evaluated advice snapshot.
     - `#adviceTelemetryTime`: display of the exact evaluated telemetry timestamp.
     - `#adviceTelemetryFreshnessBadge`: visual status indicator (`CURRENT TELEMETRY`, `STALE TELEMETRY`, `NO TELEMETRY`).
     - `#adviceGpsProvenanceBadge`: visual indicator for GPS origin (`CURRENT GPS FIX`, `LAST KNOWN GPS`, `NO GPS FIX`).
6. **`dashboard/static/css/dashboard.css`**:
   - Added flex layout and spacing for `.summary-label-group` and `.summary-meta-group`.
7. **`dashboard/static/js/dashboard.js`**:
   - Cached new freshness and provenance DOM elements.
   - Added `parseTimestamp(ts)` for robust parsing of ISO/UTC timestamp strings.
   - Implemented `checkAdviceTelemetryStaleness()` to compare incoming live telemetry timestamps (`received_at`) against the advice evaluated telemetry snapshot (`telemetry_timestamp`). Notice is displayed only when live telemetry is more than 1 second newer than the evaluated telemetry.
   - Updated `updateLatestTelemetry()` to trigger staleness checks whenever new live telemetry arrives.
   - Updated `renderAdvice(data)` to format and render evaluated telemetry timestamps, freshness badges, GPS provenance badges (including last-known GPS timestamp and age in seconds), and clear the stale-advice notice upon fresh advice generation.
   - Verified JavaScript syntax with `node -c dashboard/static/js/dashboard.js`.
8. **Regression & Unit Tests Added**:
   - `tests/test_phase5_dashboard.py`: Added DOM structure and JS logic verification tests (`test_advice_dashboard_components_rendered`, `test_dashboard_js_freshness_and_provenance_logic`).
   - `tests/test_phase8_rules.py`: Added `test_mq135_baseline_and_elevated_never_imply_calibrated_air_quality`.
   - `tests/test_phase8_weather.py`: Added `test_tls_verification_strictly_enabled` verifying `verify_mode == CERT_REQUIRED` and `check_hostname == True`.
   - `tests/test_phase8_advice.py`: Added tests for GPS fix transition, provenance distinction, last-known GPS metadata, stale telemetry detection, API metadata exposition, and graceful weather fallback.

#### 2. Verification
- Total tests executed across entire project test suite: **179 passed, 0 failures, 0 errors**.
- Execution command: `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
- Execution duration: ~25.9 seconds.

### 2026-11-17 — Advice Engine: NVIDIA Nemotron-3.5-Lightning Reasoning Model Support

#### 1. What Changed

1. **Updated `server/nvidia_client.py`**:
   - Added support for NVIDIA reasoning/thinking models (specifically `nvidia/nemotron-3.5-lightning-30b-a3b`).
   - `_parse_response` now checks for `message.reasoning_content` if `message.content` is blank or empty, ensuring that reasoning traces are gracefully extracted without falsely failing on empty choices.
   - Raised default `max_tokens` (`DEFAULT_MAX_TOKENS = 2048`) to accommodate reasoning tokens without truncation.
2. **Updated `server/app.py`**:
   - Added `_load_env_file()` function using Python standard library to automatically parse `.env` at startup.
3. **Updated `.env.example`**:
   - Documented `NVIDIA_API_KEY`, `NVIDIA_MODEL` (highlighting `nvidia/nemotron-3.5-lightning-30b-a3b`), and `NVIDIA_MAX_TOKENS`.
4. **Updated `tests/test_phase8_nvidia.py`**:
   - Added tests verifying `reasoning_content` fallback parsing and content precedence.
   - Expanded test suite from 168 to 170 passing tests.

#### 2. Verification
- `tests/test_phase8_nvidia.py`: 49 tests passing.
- Full regression suite: **170 tests passing, 0 failures, 0 errors**.

---

### 2026-11-17 — Advice Engine Phase 5: Dashboard Advice Integration

#### 1. What Changed

1. **Updated `dashboard/templates/index.html`**:
   - Added Section 3: **Agronomic Advice & Field Intelligence** (`#adviceSection`):
     - **Header & Status Badges**: `#adviceModeBadge` (`FULL_CONTEXT`, `LIMITED_CONTEXT`, `GENERAL_GUIDANCE`), `#adviceProviderBadge` (`NVIDIA NIM AI` vs `Deterministic Fallback`).
     - **Manual Control Button**: `#btnGenerateAdvice` ("Generate Advice" with icon and spinner state).
     - **Cooldown Indicator**: `#adviceCooldownIndicator` and `#adviceCooldownTimer` (dynamic countdown display).
     - **Warning Alert Banner**: `#adviceAlert` for error notifications.
     - **Empty State**: `#adviceEmptyState` displayed when no advice has been generated yet.
     - **Content View**:
       - Executive Summary box (`#adviceSummaryBox`, `#adviceTimestamp`, `#adviceSummaryText`).
       - Field Recommendations list (`#adviceRecommendationsList`) with title, explanation, and confidence tags.
       - Dual Column Grid:
         - Sensor Health Audit (`#sensorHealthList`) with distinct status pills (`HEALTHY`, `STALE`, `UNRESPONSIVE NULL`, `OFFLINE`).
         - Localized Weather Context (`#weatherContextBody`) displaying temperature, humidity, precipitation, wind, and clearly marking whether coordinates originate from `CURRENT GPS FIX` or `LAST KNOWN GPS`.
       - Deterministic Rule Observations (`#adviceObservationsList`) with severity badges (`[WARNING]`, `[INFO]`).
       - Operating Notices & Guardrail Disclaimers (`#adviceLimitationsList`).

2. **Updated `dashboard/static/css/dashboard.css`**:
   - Added comprehensive, responsive styling matching the agricultural design system:
     - `.advice-section`, `.advice-header`, `.advice-heading-row`, `.advice-actions`.
     - `.btn-generate-advice` (hover, active, disabled states).
     - `.advice-cooldown-text` in monospace wheat accent.
     - `.advice-summary-box` with olive left accent border.
     - `.recommendation-item` and confidence level badges (`.rec-confidence-high`, `.rec-confidence-medium`, `.rec-confidence-low`).
     - `.advice-columns-grid` (2-column layout collapsing to 1-column on mobile screens <= 860px).
     - `.sensor-health-row`, `.weather-metric-row`, `.observation-row`, `.advice-limitations-box`.
     - Mobile responsiveness for `< 600px` viewports.

3. **Updated `dashboard/static/js/dashboard.js`**:
   - Cached new advice DOM elements in `el`.
   - Added `escapeHtml()` utility to prevent XSS during dynamic DOM rendering.
   - Added `startCooldownTimer(remainingSeconds)` to manage 1-second countdown, disable the button, and re-enable it when cooldown expires.
   - Added `renderAdvice(data)` to populate all UI widgets, badges, lists, and disclaimers.
   - Added `fetchLatestAdvice()`: called automatically on dashboard load (`DOMContentLoaded`) to retrieve previously saved advice from `GET /api/v1/advice/latest` without triggering unnecessary AI generation.
   - Added `handleGenerateAdviceClick()`: sends `POST /api/v1/advice/generate`, updates UI, handles errors, and initiates cooldown countdown.
   - Bound advice controls on `DOMContentLoaded`.

4. **Updated `tests/test_phase5_dashboard.py`**:
   - Added `test_advice_dashboard_components_rendered` asserting presence of all new advice section IDs, controls, badges, and containers.
   - Verified that all existing dashboard tests (routes, static assets, broker status, guardrails) continue passing.

#### 2. Test Execution & Verification

- **Dashboard Tests:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest tests.test_phase5_dashboard -v
  ```
  Result: `Ran 5 tests in 0.739s — OK`
- **Full Project Regression Test Suite:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
  ```
  Result: `Ran 168 tests in 21.424s — OK` (168 tests passing, 0 failures, 0 errors).

---

### 2026-11-17 — Advice Engine Phase 4: Advice Aggregation & Flask API Integration

#### 1. What Changed

1. **Created `server/advice_engine.py`** — Advice Engine Aggregator:
   - **`AdviceEngine`**: Central coordinator bringing together database records, `WeatherService`, `evaluate_rules()`, and `NVIDIAClient`.
   - **Dynamic Rover Device ID Resolution**:
     - Automatically resolves device ID from the latest recorded telemetry or status message in SQLite.
     - Defaults to production rover `aria-esp32` (or `ARIA_DEVICE_ID` environment variable), avoiding development hardcoding of `aria-c3`.
     - Supports explicit device parameter override.
   - **Thread-Safe 10-Minute Cooldown with Atomic Reservation**:
     - `ADVICE_COOLDOWN_SECONDS = 600.0` (10 minutes).
     - Protects the external NVIDIA API from request storms and duplicate calls using `self._lock` and atomic SQLite reservation (`db.record_ai_call_attempt`) performed **before** the network call.
     - Failed NVIDIA attempts (timeouts, HTTP 500, rate limits) still activate the cooldown to prevent continuous external retries.
     - Cooldown timestamp is stored in SQLite (`device_advice.last_ai_call_at`) so backend restarts do not reset the cooldown.
   - **Sensible Re-evaluation During Cooldown**:
     - During cooldown, does not blindly return old advice text.
     - Re-evaluates current telemetry and localized weather deterministically.
     - Generates current deterministic guidance observations and recommendations based on live field conditions.
     - Sets `provider = "deterministic_fallback"` and reports cooldown remaining seconds; never claims that NVIDIA generated fresh advice.
   - **Graceful Multi-Failure Degradation**:
     - Partial/missing sensors: evaluated independently without zero-conversion.
     - GPS fix loss: recovers weather via persistent SQLite last-known coordinates (up to 24h).
     - Weather service failure: operates rules in limited context.
     - Rover offline: flags `SYS_DEVICE_OFFLINE` and sets `device_online = False`.
     - Unconfigured/failed NVIDIA: falls back seamlessly to deterministic agricultural guidance.
   - **Sanitized Exception Handling**:
     - Distinguishes expected field/API degradations from genuine programming/database bugs.
     - Unexpected internal crashes are logged with full stack trace and propagated as sanitized HTTP 500 errors.

2. **Updated `server/database.py`**:
   - Added `device_advice` table to `INIT_SQL`:
     ```sql
     CREATE TABLE IF NOT EXISTS device_advice (
         device_id TEXT PRIMARY KEY,
         advice_mode TEXT NOT NULL,
         provider TEXT NOT NULL,
         created_at TEXT NOT NULL,
         last_ai_call_at TEXT,
         payload TEXT NOT NULL
     );
     CREATE INDEX IF NOT EXISTS idx_advice_device_created ON device_advice(created_at DESC);
     ```
   - Added `record_ai_call_attempt(device_id, attempt_time)`: atomically records AI attempt timestamp.
   - Added `get_last_ai_call_at(device_id)`: retrieves timestamp of the latest AI attempt.
   - Added `save_latest_advice(device_id, advice_payload, created_at, last_ai_call_at)`: persists generated advice payload.
   - Added `get_latest_advice(device_id)`: retrieves latest generated advice from SQLite.
   - Preserved `SCHEMA_VERSION = 1` and all Phase 3 tests intact.

3. **Updated `server/app.py`**:
   - Added `advice_engine` parameter to application factory `create_app()`.
   - Added endpoints:
     - `GET /api/v1/advice/latest` (and `GET /api/v1/advice`): retrieves latest generated advice without calling external APIs. Returns 200 with `data: null` if none generated yet.
     - `POST /api/v1/advice/generate` (and `POST /api/v1/advice`): manually triggers advice generation, enforcing the 10-minute cooldown and full fault tolerance.
     - Validates `device_id` (rejects whitespace/empty with HTTP 400).
     - Returns sanitized HTTP 500 on unexpected exceptions.

4. **Created `tests/test_phase8_advice.py`** — 16 unit and integration tests:
   - Dynamic device ID resolution (auto-detects `aria-esp32`, explicit override).
   - Full context generation (fresh telemetry + weather + NVIDIA AI).
   - Limited context with missing sensors.
   - General guidance when rover is offline and all sensors null.
   - Stale telemetry detection.
   - NVIDIA failure fallback to deterministic rules.
   - Cooldown enforcement: prevents second call within 10 minutes, re-evaluates current conditions, resumes after cooldown.
   - Failed refresh preserves previously generated advice.
   - Failed NVIDIA attempt still activates cooldown.
   - Concurrent requests trigger only one NVIDIA call.
   - Backend restart does not reset cooldown.
   - Flask API endpoints (`GET /api/v1/advice/latest`, `POST /api/v1/advice/generate`).
   - Query/body parameter validation (empty device_id -> 400).
   - Sanitized HTTP 500 on unexpected errors.

#### 2. Test Execution & Verification

- **Phase 4 Advice Tests:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest tests.test_phase8_advice -v
  ```
  Result: `Ran 16 tests in 1.578s — OK`
- **Full Project Regression Test Suite:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
  ```
  Result: `Ran 167 tests in 19.849s — OK` (167 tests passing, 0 failures, 0 errors).

---

### 2026-11-17 — Advice Engine Phase 3: NVIDIA NIM Client

#### 1. What Changed

1. **Created `server/nvidia_client.py`** — complete NVIDIA NIM API client:
   - **`NVIDIAClient`**: Thread-safe class wrapping the NVIDIA NIM OpenAI-compatible chat completions endpoint (`https://integrate.api.nvidia.com/v1/chat/completions`).
   - **`build_user_prompt(rules_result, weather)`**: Constructs a structured, fact-grounded user prompt from `RulesEvaluationResult` and optional weather dict. Includes:
     - Advice mode context header.
     - Per-sensor health states from the rules engine (name, state, detail).
     - Deterministic rule observations (severity, rule_id, title, explanation).
     - Localized Open-Meteo weather context (current conditions + 6h forecast summary) when available.
     - Known limitations list from the rules engine.
     - Mode-appropriate instruction paragraph (FULL/LIMITED/GENERAL).
   - **`_SYSTEM_PROMPT`**: Static guardrails included in every API call:
     - MQ-135 strictly raw ADC only — no ppm/CO₂ conversion ever.
     - DHT22 is ambient air only — no soil moisture inference.
     - No crop-specific treatment, pesticide, or irrigation recommendations.
     - No fabricated values for absent sensors.
   - **Configuration via environment variables**: `NVIDIA_API_KEY`, `NVIDIA_MODEL`, `NVIDIA_TIMEOUT_SECONDS`, `NVIDIA_MAX_TOKENS`, `NVIDIA_TEMPERATURE`.
   - **Defaults**: Model `meta/llama-3.1-8b-instruct`, 30s timeout, 512 max tokens, temperature 0.3.
   - **`NVIDIAResponse`** dataclass with `to_dict()`, `unavailable()` factory, full token usage, finish_reason, UTC timestamp.
   - **Fault tolerance**: All errors (HTTP 401/429/500, timeout, `URLError`, malformed JSON, empty choices, empty content) return `NVIDIAResponse.unavailable(...)` — never raises.
   - **`urllib.request` only** — no external HTTP frameworks.

2. **Created `tests/test_phase8_nvidia.py`** — 47 unit tests:
   - `TestNVIDIAClientConfiguration` (8): key presence/absence, env var override, model defaults/override.
   - `TestPromptConstruction` (10): all three advice modes, sensor health states, observations, guardrails (no ppm/CO2), null sensor not fabricated, weather/forecast sections, limitations.
   - `TestAPIRequestStructure` (8): endpoint, POST method, auth header, content-type, body model/messages, max_tokens, system prompt guardrails, user prompt includes advice mode.
   - `TestResponseParsing` (7): available status, model captured, advice mode, finish_reason, UTC timestamp, `to_dict()` JSON-serializable, unavailable `to_dict()`.
   - `TestErrorHandling` (11): no key, HTTP 401, 429, 500, timeout, URLError, malformed JSON, empty choices, empty content, missing choices key.
   - `TestThreadSafety` (1): 8 concurrent calls all return valid independent responses.
   - `TestEndToEndPromptFromRules` (2): full pipeline from `evaluate_rules()` through prompt construction, checking observation data appears in actual API request body; GENERAL_GUIDANCE with no data.

#### 2. Test Execution & Verification

- **Phase 3 Tests:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest tests.test_phase8_nvidia -v
  ```
  Result: `Ran 47 tests in 0.071s — OK`
- **Full Project Regression Suite:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
  ```
  Result: `Ran 151 tests in 18.267s — OK` (151 tests, 0 failures, 0 errors)

---

### 2026-11-17 — Advice Engine Phase 2: Resilient Weather Service

#### 1. What Changed

1. **Persistent SQLite GPS Location Storage (`server/database.py`):**
   - Added table `device_gps_location` to `INIT_SQL`:
     ```sql
     CREATE TABLE IF NOT EXISTS device_gps_location (
         device_id TEXT PRIMARY KEY,
         latitude REAL NOT NULL,
         longitude REAL NOT NULL,
         last_fix_at TEXT NOT NULL,
         updated_at TEXT NOT NULL
     );
     CREATE INDEX IF NOT EXISTS idx_gps_device_updated ON device_gps_location(updated_at DESC);
     ```
   - Added `save_last_known_gps(device_id, latitude, longitude, last_fix_at, updated_at)`.
   - Added `get_last_known_gps(device_id)` returning dict with parsed ISO timestamps and coordinates.
   - Hooked `insert_telemetry` so whenever telemetry arrives with `gps.fix == True` and non-null coordinates, the coordinates are automatically persisted.
   - Kept schema version at `SCHEMA_VERSION = 1` using idempotent `CREATE TABLE IF NOT EXISTS`, preserving 100% backward compatibility with all Phase 3 tests.

2. **Resilient Weather Service (`server/weather.py`):**
   - **Open-Meteo Integration:** Connects to `https://api.open-meteo.com/v1/forecast` using standard library `urllib.request` (zero external HTTP dependencies; no `requests`, no `httpx`).
   - **Persistent GPS Recovery:**
     - Live valid GPS fix: Uses `LocationSource.CURRENT_LOCATION` and persists coordinates to SQLite immediately.
     - GPS fix loss: Falls back to `LocationSource.LAST_KNOWN_LOCATION` from SQLite if within 24 hours (`max_location_age_s=86400.0`), preventing loss of weather capabilities when the rover drives under tree canopy or indoor cover.
     - Stale GPS: Beyond 24 hours, marks `LocationSource.LOCATION_UNAVAILABLE`.
   - **Independent Weather Forecast Caching:**
     - 15-minute TTL (`cache_ttl_s=900.0`).
     - Spatial tolerance (`coord_tolerance_deg=0.01`, ~1.1 km): small movements within the field reuse the existing forecast to avoid rate limiting. Large position shifts trigger an immediate fresh request.
     - `force_refresh` option available to bypass cache when explicitly requested.
   - **Error Handling & Fallbacks:**
     - Network timeout, HTTP 429 rate limit, or HTTP 500 error gracefully falls back to unexpired cached weather with an explanatory limitation note.
     - If rover loses GPS fix but an unexpired cached forecast exists, returns the cached forecast.
     - Expired weather is never presented as current fresh weather.
   - **Payload Normalization & Guardrails:**
     - Computes next-6-hour forecast summary: max precipitation probability, total expected rainfall (mm), max temperature, max wind speed.
     - Provides aliased fields (`temperature_2m`, `relative_humidity_2m`, `precipitation`, `wind_speed_10m`) for full cross-compatibility with `server/rules.py`.
     - Explicit attribution: `"Weather data by Open-Meteo.com"`.
     - Explicit limitations list: Regional forecast disclaimer, and note that GPS horizontal precision is unverified because HDOP is omitted from the telemetry contract.

3. **Created `tests/test_phase8_weather.py`:**
   - 23 comprehensive unit and integration tests covering:
     - GPS coordinate sanity and boundary validation (valid range, out-of-range, null, boolean, nan).
     - Persistent SQLite storage, restart recovery, and device isolation.
     - Automatic GPS persistence upon telemetry ingestion.
     - Location resolution: current fix, fallback to last-known on fix loss, stale cutoff (> 24h).
     - Open-Meteo API fetching and parsing (current conditions + 6-hour forecast summary).
     - HTTP error handling: 429 rate limit, timeouts, malformed JSON.
     - Weather caching: 15m TTL, 0.01° spatial tolerance, force refresh, cache fallback during API outages or GPS loss.
     - Fault tolerance: all physical sensors null/disconnected, empty database, rover offline, SQLite database exceptions handled without crashing the service.

#### 2. Test Execution & Verification

- **Weather Service Tests:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest tests/test_phase8_weather.py -v
  ```
  Result: `Ran 23 tests in 0.381s - OK`
- **Full Project Regression Test Suite:**
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
  ```
  Result: `Ran 104 tests in 17.806s - OK` (104 tests passing, 0 failures, 0 errors).

---

### 2026-11-17 — Advice Engine Phase 1: Deterministic Rules Engine

#### 1. What Changed

1. **Created `server/rules.py`:**
   - **Advice Mode Classification:** Implemented refined 3-tier classification:
     - `FULL_CONTEXT`: Fresh telemetry (< 15 s old), at least two valid physical sensor measurements, and valid localized weather.
     - `LIMITED_CONTEXT`: Useful contextual data exists (e.g. at least one sensor or weather), but one or more inputs are missing, weather is unavailable, or telemetry is stale.
     - `GENERAL_GUIDANCE`: No usable physical measurements and no valid localized weather.
   - **Decoupled Input Evaluation:** Each physical sensor (`temperature_c`, `humidity_pct`, `light_lux`, `mq135_raw`) and GPS is audited independently. A missing sensor reading is never treated as zero and does not invalidate other surviving sensor readings.
   - **Disconnection vs Stale vs Fault Distinction:**
     - Device offline (`device_online=False` and no telemetry): Classified as `SensorState.DEVICE_OFFLINE`, not a physical sensor fault.
     - Stale telemetry (age > `telemetry_stale_seconds`): Classified as `SensorState.STALE` with an operational warning.
     - Live null reading: Classified as `SensorState.UNRESPONSIVE_NULL`.
     - Valid reading: Classified as `SensorState.HEALTHY`.
   - **Configurable Monitoring Thresholds (`MonitoringThresholds`):**
     - Ambient temperature: High heat (>= 35°C), low frost watch (<= 4°C), and nominal range (15–28°C).
     - Relative humidity: High ambient moisture (>= 85%), low atmospheric humidity (<= 30%), and nominal range.
     - Ambient light: High direct solar illuminance (>= 60,000 lux), low illuminance (<= 5,000 lux), and moderate daylight.
     - MQ-135 raw ADC: Elevated raw voltage (>= 2500) vs baseline.
     - Weather cross-checks: Microclimate vs regional temperature discrepancy (>= 5°C delta), active precipitation, and elevated forecast rain probability (>= 60%).
   - **Strict Agricultural Guardrails Enforced:**
     - MQ-135 is strictly labeled as `"raw ADC counts (0–4095)"`. Never outputs ppm, CO₂, or an air quality score.
     - Ambient relative humidity rule explicitly notes: *"Strictly reflects air humidity, NOT soil moisture."*
     - Ambient temperature rule explicitly notes: *"Reflects ambient air, not crop canopy temperature."*
     - GPS status explicitly disclaims: *"Horizontal precision (HDOP) is unverified in telemetry contract."*
   - **Deterministic Fallback Guidance Generator (`generate_deterministic_fallback_guidance`):**
     - Generates structured observations and general agricultural stewardship notes without AI.
     - Synthesizes equipment operating conditions, ground accessibility, and atmospheric evaporative demand.
     - Strictly forbids inventing crop-specific, irrigation, fertilizer, or chemical recommendations.
2. **Created `tests/test_phase8_rules.py`:**
   - Added 23 comprehensive unit tests covering mode classification, independent missing sensors, stale data, device disconnection, threshold overrides, strict guardrails, and deterministic fallback outputs.

#### 2. Files Modified / Created

- `server/rules.py` — **CREATED** Deterministic rules engine, dataclasses, thresholds, and fallback generator.
- `tests/test_phase8_rules.py` — **CREATED** 23 unit and integration tests.
- `WORKDONE.md` — Updated milestone status and test log.

#### 3. Test Execution & Verified Results

1. **Phase 1 Rules Engine Test Suite:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase8_rules.py -v
   ```
   *Output:*
   ```text
   test_all_sensors_missing ... ok
   test_ambient_light_thresholds ... ok
   test_custom_threshold_override ... ok
   test_device_offline_not_marked_as_sensor_fault ... ok
   test_each_sensor_missing_individually ... ok
   test_fallback_guidance_generation ... ok
   test_fallback_guidance_in_general_guidance_mode ... ok
   test_full_context_mode ... ok
   test_general_guidance_mode_when_no_sensors_and_no_weather ... ok
   test_gps_hdop_unverified_disclaimer ... ok
   test_humidity_does_not_infer_soil_moisture ... ok
   test_humidity_thresholds ... ok
   test_limited_context_when_telemetry_is_stale ... ok
   test_limited_context_when_weather_unavailable ... ok
   test_limited_context_with_single_sensor_only ... ok
   test_limited_context_with_weather_only ... ok
   test_mq135_raw_adc_strict_guardrail ... ok
   test_stale_telemetry_sensor_state ... ok
   test_temperature_does_not_infer_crop_canopy ... ok
   test_temperature_thresholds ... ok
   test_unresponsive_null_sensor_on_live_telemetry ... ok
   test_weather_active_precipitation_and_high_probability ... ok
   test_weather_microclimate_temperature_delta ... ok
   Ran 23 tests in 0.005s
   OK
   ```

2. **Full Regression Test Suite (Phases 1–8):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
   ```
   *Output:*
   ```text
   Ran 81 tests in 17.149s
   OK
   ```

#### 4. Known Limitations & Scope Status

- **Phase 1 Scope:** Focused strictly on the deterministic rules engine and fallback guidance.
- **Weather Service (Phase 2):** Not yet implemented; mock weather structures were used in rules testing.
- **NVIDIA Client (Phase 3):** Not yet implemented.
- **Advice Aggregator & Endpoints (Phase 4):** Not yet implemented.
- **Dashboard Advice Panel (Phase 5):** Not yet implemented.

---


### 2026-11-17 — ARIA Motor Control, HC-SR04 Removal & Agricultural Redesign

#### 1. What Changed

1. **Complete Removal of HC-SR04 Ultrasonic Sensor:**
   - Removed `distance_cm` from `TelemetryMessage` dataclass and validation bounds in `server/telemetry.py`.
   - Incoming payloads containing `distance_cm` (e.g. from legacy simulators) are safely tolerated and parsed without errors, but the field is ignored.
   - Database layer (`server/database.py`): Preserved the existing `distance_cm REAL` column in SQLite tables to prevent destructive migrations and preserve historical samples, but inserted value is set to `None`.
   - REST API (`server/app.py`): Removed `distance_cm` from `_telemetry_to_dict()`.
   - Dashboard (`dashboard/templates/index.html`, `dashboard/static/js/dashboard.js`, `dashboard/static/css/dashboard.css`): Removed distance metric card, proximity alert banner, collision safety disclaimers, and Chart 3 (`chartDistance`). Environmental monitoring now renders 2 clean time-series charts.
2. **Manual MQTT Motor Control (`POST /api/v1/command`):**
   - Implemented `publish_command(command: str)` in `ARIAClient` (`server/mqtt_client.py`):
     - Validates command strictly against `{"forward", "reverse", "left", "right", "stop"}`.
     - Publishes to topic `aria/v1/command` with payload `{"command": "<cmd>"}`.
     - Enforces **QoS 1** (acknowledged delivery) and **`retain=False`** (strictly non-retained).
     - Verifies actual delivery receipt via `wait_for_publish()` and `is_published()`; does not report success merely because the client is marked connected.
     - Prevents arbitrary topic or payload injection.
   - Implemented `POST /api/v1/command` in `server/app.py`:
     - Enforces `application/json` Content-Type and object body format.
     - Validates `command` field (case-insensitive, whitespace-trimmed).
     - Returns HTTP 400 for malformed JSON, missing fields, non-string types, or unsupported commands.
     - Returns HTTP 503 if backend MQTT client is stopped, broker is disconnected, or message acknowledgment times out.
     - Returns HTTP 200 `{"status": "success", "command": "<cmd>"}` on verified delivery.
3. **Dashboard Motor Controls & Safety Architecture:**
   - Dedicated **ARIA Rover Control** card with tactile 3×3 D-Pad: Forward (Up), Reverse (Down), Left (Left), Right (Right), Center Stop (■).
   - Prominent Emergency STOP button (`btnEmergencyStop`).
   - Press-and-hold movement streaming:
     - Uses `pointerdown`, `pointerup`, `pointercancel` with **pointer capture** (`setPointerCapture`) so dragging outside the button maintains release tracking.
     - Streams command packets approximately every 200 ms while held.
     - On release or pointer cancellation, immediately halts stream and dispatches STOP.
   - Safety Releases:
     - Global window `blur` and document `visibilitychange` (tab hidden) immediately execute the shared `emergencyStop()` routine.
     - Movement mutex: Prevents multiple simultaneous movement commands.
     - STOP bypass: STOP is always available and immediately clears any active movement timers regardless of mutex state.
     - Offline lockout: Movement controls are disabled when device status is not `"online"`. Commands are never queued, and rover movement never automatically resumes upon reconnect.
     - Explicit UI readout: Shows requested command stream (e.g. `REQUESTED: FORWARD` / `IDLE (STOP)`); clearly disclaims that transmission does not represent physical movement confirmation.
     - Error banner: Displays transmission errors and alerts operator that the ESP32 500 ms hardware safety timeout is active.
4. **Agricultural Visual Redesign:**
   - Swapped generic tech aesthetic for an agricultural field-station design system:
     - Muted olive green (`#688448`, `#495e32`, `#83a55c`).
     - Earthy beige and sand (`#f5f4ed`, `#e5ded0`, `#c7beab`).
     - Deep slate charcoal (`#121513`, `#181d1a`, `#202622`).
     - Restrained amber warnings (`#c88214`).
   - Clean, readable typography and structured 2×2 environmental sensor grid (Temp, Humidity, Light, MQ-135 Raw ADC).
   - Prominent disclaimers: MQ-135 explicitly designated as uncalibrated raw ADC (0–4095), not ppm or CO₂.
   - Leaflet outdoor GPS map with fix lock enforcement.

#### 2. Files Modified / Created

- `server/telemetry.py` — Removed `distance_cm` from dataclass and validation bounds; added `VALID_COMMANDS`.
- `server/database.py` — Updated `insert_telemetry()` and `_row_to_telemetry()` to exclude active `distance_cm` while preserving historical DB column.
- `server/mqtt_client.py` — Added `publish_command()` with QoS 1, `retain=False`, and verified acknowledgment check.
- `server/app.py` — Added `POST /api/v1/command` endpoint; removed `distance_cm` from `_telemetry_to_dict()`.
- `dashboard/templates/index.html` — Redesigned agricultural layout, removed ultrasonic cards/charts, added Rover Control D-Pad & Emergency STOP.
- `dashboard/static/css/dashboard.css` — Applied muted olive/beige/charcoal palette, tactile control buttons, responsive layout.
- `dashboard/static/js/dashboard.js` — Implemented pointer-capture press-and-hold motor streaming (200 ms), emergency stop, offline lockout, removed ultrasonic chart.
- `tests/test_phase2_contract.py` — Updated to remove `distance_cm` bounds test; added test verifying legacy `distance_cm` is safely ignored.
- `tests/test_phase3_database.py` — Updated DB persistence tests to remove `distance_cm` assertions.
- `tests/test_phase4_api.py` — Verified API serialization excludes `distance_cm`.
- `tests/test_phase5_dashboard.py` — Verified HC-SR04 and `chartDistance` are absent from HTML; verified motor control elements present.
- `tests/test_phase6_mqtt_integration.py` — Updated integration test payload.
- `tests/test_phase7_motor_control.py` — **CREATED** 10 automated unit and integration tests covering motor control endpoint, validation, QoS 1, retain, timeout, and broker failure modes.
- `docs/PROJECT_SPEC.md` — Updated hardware inventory and operating scope.
- `docs/MQTT_CONTRACT.md` — Updated telemetry schema and documented `aria/v1/command` protocol contract.
- `WORKDONE.md` — Recorded Phase 7 milestone and verified results.

#### 3. Test Execution & Verified Results

1. **Phase 7 Motor Control Test Suite:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase7_motor_control.py -v
   ```
   *Output:*
   ```text
   test_broker_disconnected_returns_503 ... ok
   test_case_insensitive_and_whitespace_strip ... ok
   test_invalid_command_strings_rejected ... ok
   test_missing_or_non_string_command_field ... ok
   test_mqtt_client_not_running_returns_503 ... ok
   test_mqtt_publish_error_returns_503 ... ok
   test_mqtt_publish_timeout_returns_503 ... ok
   test_non_json_body_rejected ... ok
   test_stop_command_always_available ... ok
   test_valid_motor_commands ... ok
   Ran 10 tests in 2.222s
   OK
   ```

2. **Complete Regression Test Suite (Phases 1–7):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
   ```
   *Output:*
   ```text
   Ran 58 tests in 17.763s
   OK
   ```

#### 4. Remaining Limitations & Hardware Testing Status

- **Hardware Testing Status:** Physical motor hardware testing was **NOT** performed. All verification is based on unit tests, mock Paho broker clients, and live Mosquitto CLI loopback.
- **Physical Rover Limitations:**
  - L298N motor driver wiring, jumper configuration, and motor battery supply remain unverified on physical hardware.
  - ESP32 firmware motor subscriber has not been compiled or flashed to hardware.
  - The device-side 500 ms command watchdog and motor driver pins must be tested on the physical rover once the hardware controller is wired.

---


### 2026-11-15 — Phase 6 MQTT Ingestion Integration

#### 1. Problem Statement

`server/app.py` created the Flask application and database but never created or started `ARIAClient`. Mosquitto received messages from the simulator but nothing consumed them: no validation, no persistence, no API visibility.

#### 2. Architecture Changes

**`server/app.py`** — Significantly modified:
- Added imports: `atexit`, `uuid`, `ARIAClient`, `StatusMessage`, `TelemetryMessage`.
- Changed `create_app()` signature to `create_app(db=None, mqtt_client=None, start_mqtt=None) -> Flask`, enabling dependency injection for testing without a real broker.
- Inside factory: creates `ARIAClient` with per-process client ID (`f"aria-backend-{uuid.uuid4().hex[:8]}"`) to avoid client ID collisions in multi-process deployments.
- Registered handlers:
  - `on_telemetry` → `database.insert_telemetry(msg)` (persists validated telemetry)
  - `on_status` → `database.insert_status(msg)` (persists validated status)
  - `on_quarantine` → `log.warning(...)` (logs rejected messages without storing them)
- Attached `app.mqtt_client = client` for health check inspection.
- Added `app.stop_mqtt()` teardown function and registered with `atexit.register()`.
- Guard against Werkzeug double-start: MQTT client is not started in the reloader's master process (`WERKZEUG_RUN_MAIN` env var check).
- Updated health check to prefer `app.mqtt_client.is_connected` before falling back to TCP socket probe.

**`server/mqtt_client.py`** — Modified:
- Added `is_running` property: `return self._running`.
- Improved `start()`: rolls back `_running = True` on `OSError` during `connect()` or `loop_start()`.
- `stop()` already in place with socket pair cleanup.

**`server/database.py`** — Modified:
- Added SQLite URI support: `Database` now detects when `db_path` starts with `"file:"` and passes `uri=True` to `sqlite3.connect()`. This enables named shared-cache in-memory databases (e.g. `file:name?mode=memory&cache=shared`) to be safely shared across threads — required for Phase 6 integration tests.
- Attribute `_db_path_raw` stores the raw string; `db_path` (a `Path`) is set to `Path(":memory:")` for URI paths so `_ensure_dir()` correctly skips directory creation.

**`tests/test_phase4_api.py`** — Modified:
- `create_app(db=self.db)` → `create_app(db=self.db, start_mqtt=False)` to avoid MQTT worker threads during API-only tests.
- `tearDown` now calls `self.app.stop_mqtt()` before `self.db.close()`.
- `test_health_check_degraded_when_db_fails` now passes `start_mqtt=False` to its inline `create_app()` call.

**`tests/test_phase5_dashboard.py`** — Modified:
- Same `start_mqtt=False` and `app.stop_mqtt()` in tearDown pattern as Phase 4.

**`tests/test_phase6_mqtt_integration.py`** — CREATED:
- Uses named shared-cache in-memory SQLite URIs (`file:<uuid>?mode=memory&cache=shared`) so the MQTT worker thread and test main thread share the same in-memory database across threads. No on-disk files → no WAL file locking on Windows.
- Centralized `setUp`/`tearDown`: `self._app` is set once per test; `tearDown` calls `self._app.stop_mqtt()` and then `self.db.close()` in the correct order.
- 6 tests:
  1. `test_valid_telemetry_stored_in_sqlite` — valid telemetry persisted to SQLite.
  2. `test_valid_status_stored_in_sqlite` — valid status persisted to SQLite.
  3. `test_invalid_telemetry_quarantined_and_not_stored` — invalid telemetry goes to quarantine handler, not stored.
  4. `test_app_startup_starts_mqtt_integration` — `connect()` and `loop_start()` called once on startup.
  5. `test_clean_shutdown_stops_client_and_worker` — `disconnect()` and `loop_stop()` called; worker thread joins.
  6. `test_client_not_started_multiple_times` — second `start()` on running client is a no-op.

#### 3. Key Architecture Decisions

| Decision | Rationale |
|---|---|
| `create_app(start_mqtt=False)` in Phase 4/5 tests | Prevents MQTT worker threads from holding DB file handles during teardown, eliminating Windows WAL file locking errors |
| Named shared-cache URI (`file:uuid?mode=memory&cache=shared`) in Phase 6 tests | In-memory DBs are per-connection; thread-local connections mean worker thread sees its own empty DB unless a shared cache URI is used |
| `atexit.register(stop_mqtt)` | Ensures clean MQTT shutdown even when Flask runs outside `try/finally` |
| Werkzeug reloader guard (`WERKZEUG_RUN_MAIN`) | Prevents duplicate MQTT client creation when Flask development server uses its file-change reloader |

#### 4. Commands Run & Observed Results

1. **Phase 6 integration tests (mocked broker, no real broker required):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase6_mqtt_integration.py -v
   ```
   *Output:*
   ```text
   test_app_startup_starts_mqtt_integration ... ok
   test_clean_shutdown_stops_client_and_worker ... ok
   test_client_not_started_multiple_times ... ok
   test_invalid_telemetry_quarantined_and_not_stored ... ok
   test_valid_status_stored_in_sqlite ... ok
   test_valid_telemetry_stored_in_sqlite ... ok
   Ran 6 tests in 1.945s
   OK
   ```

2. **Full regression suite (Phases 1–6):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   ```
   *Output:*
   ```text
   Ran 47 tests in 15.431s
   OK
   ```

#### 5. Run Commands for Live Verification

```powershell
# Start the server with MQTT ingestion active:
cd c:\Users\server\Downloads\ARIA_Cursor
$env:ARIA_MQTT_HOST = "127.0.0.1"
$env:ARIA_MQTT_PORT = "1883"
$env:ARIA_HTTP_PORT = "5000"
.\.venv\Scripts\python.exe -m server.app

# Publish a test telemetry message:
& "C:\Program Files\mosquitto\mosquitto_pub.exe" -h 127.0.0.1 -t "aria/v1/telemetry" -m '{
  "schema_version": 1, "device_id": "aria-c3", "source": "simulated",
  "timestamp": "2026-11-15T10:00:00Z",
  "temperature_c": 28.5, "humidity_pct": 65.2, "light_lux": 500.0,
  "mq135_raw": 1200, "distance_cm": 85.0,
  "gps": {"fix": false, "latitude": null, "longitude": null}
}'

# Confirm ingestion via REST API:
curl http://127.0.0.1:5000/api/v1/telemetry/latest
```

#### 6. Notes

- A real ESP32-C3 simulator is **actively publishing** to `aria/v1/telemetry` on the LAN Mosquitto broker. Tests that use the live broker use unique `device_id` values (not `"aria-c3"`) to avoid picking up ambient messages.
- Phase 7 (ESP32 firmware) has not been started. Awaiting user approval.

---

### 2026-11-15 — Phase 5 Web Dashboard

#### 1. Frontend Architecture & Implementation
- Created clean, responsive, agricultural rover monitoring interface under `dashboard/`:
  - `dashboard/templates/index.html`: Responsive HTML5 layout structured with semantic sections.
  - `dashboard/static/css/dashboard.css`: Modern dark slate theme (`#0b1120`, `#1e293b`) with agricultural accents, metric grids, proximity warning banners, and mobile breakpoints.
  - `dashboard/static/js/dashboard.js`: Vanilla JavaScript polling client (2000 ms interval) querying existing Flask REST endpoints (`/api/v1/health`, `/api/v1/status/latest`, `/api/v1/telemetry/latest`, `/api/v1/telemetry/history?limit=30`).
- Integrated Chart.js 4.4.7:
  - Chart 1: Temperature (°C) & Relative Humidity (%) time-series.
  - Chart 2: Ambient Light (lux) & Gas Sensor Raw ADC dual-axis time-series.
  - Chart 3: Front Obstacle Distance (cm) time-series.
- Integrated Leaflet 1.9.4:
  - Map initialized on `#gpsMap` with OpenStreetMap tile layer.
  - Strict fix enforcement: Coordinates readout and rover marker are rendered **only** when `gps.fix == true` and coordinates are valid numbers. When `fix == false` or `null`, markers are hidden and a dedicated overlay banner informs the operator that GPS fix is unavailable.
- Separate Connectivity Indicators:
  - **MQTT Broker**: Live TCP socket probe in `/api/v1/health` confirms broker connectivity (`CONNECTED` / `OFFLINE`).
  - **Flask Backend**: Confirms HTTP API and SQLite WAL responsiveness (`ONLINE` / `DEGRADED`).
  - **Rover Device**: Driven by `/api/v1/status/latest` with Wi-Fi RSSI and uptime (`ONLINE` / `DEGRADED` / `OFFLINE`).
- Provenance & Stale Data Guardrails:
  - Header provenance badge explicitly distinguishes `SIMULATED DATA` (amber) vs `REAL SENSOR DATA` (green).
  - Stale data detection triggers an alert banner whenever latest telemetry is older than 15 seconds, reporting the exact elapsed time and muting values.
  - Disconnected/null sensors display `UNAVAILABLE` rather than fabricated or zero values.
  - MQ-135 reading is explicitly labelled as `Raw ADC` with an onboard disclaimer: *"Uncalibrated raw ADC reading (0–4095). Not calibrated ppm or CO₂."*
  - HC-SR04 front distance includes proximity warning banner when `< 20 cm` and clarifies forward safety inhibit scope (not autonomous obstacle avoidance).
- Backend Updates (`server/app.py`):
  - Configured `template_folder` and `static_folder` pointing to `dashboard/templates` and `dashboard/static`.
  - Added `GET /` route serving `index.html`.
  - Added `_check_broker_connected()` probe to `GET /api/v1/health` reporting `"broker": "connected"`.

#### 2. Verification & Test Suite (`tests/test_phase5_dashboard.py`)
- Added 4 automated tests:
  - `test_dashboard_index_route`: Verifies `GET /` returns HTTP 200 `text/html` containing all core elements, Chart.js, and Leaflet.
  - `test_dashboard_static_assets_served`: Verifies CSS and JavaScript assets are accessible with correct MIME types.
  - `test_health_check_includes_broker_status`: Verifies `/api/v1/health` reports broker status.
  - `test_disclaimers_and_guardrails_in_html`: Verifies required disclaimers for MQ-135 raw ADC, HC-SR04 safety inhibit, and GPS fix overlays are present.

#### 3. Commands Run & Observed Results
1. **Phase 5 test suite execution:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase5_dashboard.py
   ```
   *Output:*
   ```text
   Ran 4 tests in 0.372s
   OK
   ```
2. **Complete regression test suite (Phases 1 + 2 + 3 + 4 + 5):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   ```
   *Output:*
   ```text
   Ran 41 tests in 12.080s
   OK
   ```

---

### 2026-11-15 — Phase 4 Flask REST API

#### 1. Architecture & Endpoints (`server/app.py`)
- Installed and pinned `flask==3.1.3` in `server/requirements.txt`.
- Implemented application factory `create_app(db: Database | None = None)` enabling dependency injection for isolated testing.
- Reused `Database` persistence layer from `server/database.py` exclusively with zero duplicated SQL queries.
- Endpoints implemented:
  - `GET /api/v1/health`: Checks DB responsiveness via `count_telemetry()`. Returns HTTP 200 `{"status": "ok", "database": "connected"}` or HTTP 503 `{"status": "degraded", "database": "disconnected"}`.
  - `GET /api/v1/telemetry/latest`: Returns latest telemetry sample. Returns HTTP 200 `{"status": "success", "data": null, "message": "no telemetry recorded yet"}` on empty database. Supports optional `device_id` filter (returns HTTP 404 if specific device not found; HTTP 400 if empty).
  - `GET /api/v1/telemetry/history`: Bounded time-series query ordered by `received_at DESC`. Validates query parameters:
    - `limit`: Integer between 1 and 1000 (defaults to 100). Returns HTTP 400 on non-integer, <=0, or >1000.
    - `since`: ISO-8601 string with UTC normalization and URL space/plus unescaping. Returns HTTP 400 on malformed timestamp.
    - `device_id`: Optional filter.
    - Returns HTTP 200 `{"status": "success", "count": 0, "data": []}` on empty database.
  - `GET /api/v1/status/latest`: Returns latest device status. Returns HTTP 200 `{"status": "success", "data": null}` on empty database, or HTTP 404 if specific `device_id` not found.
- Consistent Error Envelope: Custom error handlers for HTTP 400, 404, 405, and 500 guarantee predictable JSON: `{"status": "error", "message": "..."}`.
- Constraints Honored: Zero motor-control endpoints, authentication systems, WebSockets, or frontend code added. MQTT networking and DB writes remain completely decoupled from HTTP handling.

#### 2. Verification & Test Suite (`tests/test_phase4_api.py`)
- Added 13 automated tests using Flask test client:
  - `test_health_check_success`: 200 OK and connected state.
  - `test_health_check_degraded_when_db_fails`: 503 Service Unavailable when DB throws.
  - `test_telemetry_latest_empty_database`: 200 OK with `data: null`.
  - `test_telemetry_latest_success`: Formatted telemetry serialization with all sensor and GPS fields.
  - `test_telemetry_latest_filter_by_device_id`: Device filtering, 404 on missing device, 400 on whitespace device ID.
  - `test_telemetry_history_empty_database`: 200 OK with `count: 0`, `data: []`.
  - `test_telemetry_history_success_and_ordering`: Correct descending order verification.
  - `test_telemetry_history_limit_parameter`: Valid limit, non-integer limit (400), zero limit (400), and limit > 1000 (400).
  - `test_telemetry_history_since_parameter`: Time filtering and invalid date handling (400).
  - `test_status_latest_empty_database`: 200 OK with `data: null`.
  - `test_status_latest_success`: Valid status retrieval and 404 on missing device.
  - `test_method_not_allowed`: 405 Method Not Allowed handling on POST.
  - `test_not_found_endpoint`: 404 on nonexistent route.

#### 3. Commands Run & Observed Results
1. **Flask installation:**
   ```powershell
   .\.venv\Scripts\pip.exe install flask
   ```
   *Output:* Successfully installed `flask-3.1.3`, `werkzeug-3.1.9`, `jinja2-3.1.6`, `click-8.5.1`, `blinker-1.9.0`, `itsdangerous-2.2.0`, `markupsafe-3.0.4`.
2. **Phase 4 test suite execution:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase4_api.py
   ```
   *Output:*
   ```text
   Ran 13 tests in 1.040s
   OK
   ```
3. **Full regression test suite (Phases 1 + 2 + 3 + 4):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   ```
   *Output:*
   ```text
   Ran 37 tests in 11.631s
   OK
   ```

---

### 2026-11-15 — Phase 3 SQLite Persistence

#### 1. Architecture & Schema Design (`server/database.py`)
- SQLite storage configured with WAL mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;`).
- Schema migrations tracking initialized via `schema_migrations` table (`version=1`).
- Tables created:
  - `telemetry`: Stores `schema_version`, `device_id`, `source`, `received_at` (server UTC), `device_timestamp` (device RTC/uncalibrated time), `temperature_c`, `humidity_pct`, `light_lux`, `mq135_raw`, `distance_cm`, `gps_fix`, `gps_lat`, `gps_lon`, `gps_satellites`, `gps_altitude_m`, `gps_speed_mps`, and `raw_payload`.
  - `device_status`: Stores device lifecycle and LWT events (`status`, `ip_address`, `wifi_rssi_dbm`, `uptime_s`, timestamps).
- Indexes added for query performance:
  - `idx_telemetry_received_at` ON `telemetry(received_at DESC)`
  - `idx_telemetry_device_received` ON `telemetry(device_id, received_at DESC)`
  - `idx_status_device_received` ON `device_status(device_id, received_at DESC)`

#### 2. Key Persistence Policies Enforced
- **Server vs. Device Timestamps**: Server `received_at` (monotonically stamped server UTC) is stored independently of `device_timestamp` (optional reported time from device).
- **Out-of-Order & Duplicate Handling**: Unambiguously resolved by ordering all time-series queries by `(received_at DESC, id DESC)`. Duplicate device timestamps do not collide or overwrite previous samples.
- **Null Safety**: Disconnected or unavailable sensors cleanly store `NULL` in the database and return `None` in Python models.
- **Bounded Queries**: `get_telemetry_history()` enforces a strict upper bound (`MAX_QUERY_LIMIT = 1000`, default `100`) preventing unbounded memory usage.
- **Thread Safety & Resource Hygiene**: Uses thread-local connections with a centralized registry closing all connections on shutdown, preventing file locks on Windows.
- **Parameterized SQL**: Zero dynamic SQL string concatenation.

#### 3. Verification & Test Suite (`tests/test_phase3_database.py`)
- Added 9 automated tests:
  - `test_schema_initialization_and_idempotence`: Table creation and migration idempotency.
  - `test_insert_and_query_full_telemetry`: Full telemetry round-trip verification.
  - `test_insert_and_query_null_sensor_fields`: Missing/null sensor preservation.
  - `test_separate_received_at_and_device_timestamp`: Independent timestamp tracking.
  - `test_duplicate_and_out_of_order_timestamps`: Deterministic ordering with duplicate/out-of-order device timestamps.
  - `test_bounded_history_queries`: Limit bounding, capping, and `since` time filtering.
  - `test_status_insert_and_query`: Status transitions and LWT offline records.
  - `test_database_restart_persistence`: Persistence across full connection close, process garbage collection, and database reopen from disk.
  - `test_end_to_end_mqtt_to_database`: End-to-end integration proving `ARIAClient` automatically parses and inserts valid telemetry and status published via Mosquitto CLI into SQLite.

#### 4. Commands Run & Observed Results
1. **Phase 3 test suite execution:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase3_database.py
   ```
   *Output:*
   ```text
   Ran 9 tests in 2.824s
   OK
   ```
2. **Full project test suite discovery (Phases 1 + 2 + 3):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   ```
   *Output:*
   ```text
   Ran 24 tests in 10.531s
   OK
   ```

---

### 2026-11-15 — Phase 2 MQTT Contract & Backend Foundation

#### 1. Contract Formalization (`docs/MQTT_CONTRACT.md`)
- Documented full topic topology:
  - `aria/v1/telemetry`: Device → PC, QoS 0, `retain=false` (streaming sensor telemetry; stale retained readings prohibited).
  - `aria/v1/status`: Device → PC, QoS 1, `retain=true` (device health & connectivity; integrates with MQTT LWT).
  - `aria/v1/command`: PC → Device, QoS 1, `retain=false` (**strictly never retain**; safety-critical motor inhibitor).
  - `aria/v1/ack`: Device → PC, QoS 1, `retain=false`.
- Defined payload schemas, required vs. optional fields, types, units, and ranges:
  - `schema_version`: integer (`1`).
  - `device_id`: string `[a-zA-Z0-9_-]{1,64}`.
  - `source`: `"simulated"` or `"real"`.
  - Environmental fields: `temperature_c` (-40..80 °C), `humidity_pct` (0..100 %), `light_lux` (>=0), `mq135_raw` (ADC 0..4095; explicitly uncalibrated raw value), `distance_cm` (2..400 cm).
  - GPS object: `fix` (bool), `latitude` (-90..90) & `longitude` (-180..180) required when `fix=true`, strictly `null` when `fix=false`.
- Contract rule enforced: unavailable/disconnected sensors are represented as `null` or omitted (valid message); malformed JSON, wrong types, out-of-bounds, or missing required fields are quarantined without crashing the backend.

#### 2. Modular Implementation
- Created `server/telemetry.py`:
  - Standard library implementation using Python 3.12 `dataclasses`, `datetime`, `json`, `typing`.
  - Zero external dependencies introduced.
  - Implements `validate_telemetry_payload()` and `validate_status_payload()`.
  - Distinguishes malformed payloads from valid payloads with unavailable sensor readings.
  - Strict type checking (including `bool` vs `int` disambiguation).
- Created `server/mqtt_client.py`:
  - Decoupled `ARIAClient` class using Paho MQTT v2 Callback API.
  - Non-blocking network callbacks: incoming messages are queued immediately to an internal worker thread.
  - Dispatches validated messages to registered handlers: `on_telemetry`, `on_status`, and `on_quarantine`.
  - Fixed subscription topic to `aria/v1/#` (preventing duplicate delivery caused by wildcard overlap).
  - Clean lifecycle management (`start()`, `stop()`).
- Created `tests/test_phase2_contract.py`:
  - 11 unit & integration tests covering valid full telemetry, nullable sensors, malformed JSON, missing fields, unsupported schema versions, invalid source values, wrong types, GPS inconsistency, sensor bounds, status LWT payloads, and live broker pub/sub integration.

#### 3. Commands Run & Observed Results
1. **Phase 2 test suite execution:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase2_contract.py
   ```
   *Output:*
   ```text
   Ran 11 tests in 2.128s
   OK
   ```
2. **Complete project test discovery (Phases 1 + 2):**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   ```
   *Output:*
   ```text
   Ran 15 tests in 7.865s
   OK
   ```

---

### 2026-11-15 — Phase 0 Environment Audit & Phase 1 Python MQTT Subscriber

#### 1. Environment Audit Findings (Phase 0)
- **Operating System:** Windows 11.
- **Python Version:**
  - System Python in `PATH`: Python 3.14.7.
  - Project `.venv`: Python 3.12.9 (`cpython-3.12.9-windows-x86_64-none`).
- **MQTT Broker:**
  - Eclipse Mosquitto version 2.1.2 installed at `C:\Program Files\mosquitto`.
  - Windows service `mosquitto` (DisplayName: `Mosquitto Broker`, PID 6268) is `Running`.
  - Ports: Listening on TCP 1883 (`0.0.0.0:1883` and `[::]:1883`).
  - Note: `C:\Program Files\mosquitto` was not in non-interactive session `PATH`; CLI commands invoked directly from `C:\Program Files\mosquitto\mosquitto_pub.exe`.
- **Git Repository Audit:**
  - Workspace `c:\Users\server\Downloads\ARIA_Cursor` is not an independent git root; git detected parent repository rooted at `C:\Users\server`.
- **Discrepancies & Missing Files Identified:**
  - `docs/PROJECT_SPEC.md` was originally present as `docs/ARIA_PROJECT_SPEC.md`. Created `docs/PROJECT_SPEC.md` as exact copy to match all brief references and rules.
  - `WORKDONE.md` did not exist; only `WORKDONE_TEMPLATE.md` existed. Initialized `WORKDONE.md`.
  - References in brief to `C:\ARIA` mapped to actual workspace path `c:\Users\server\Downloads\ARIA_Cursor`.

#### 2. Implementation (Phase 1)
- Verified `server/requirements.txt`: pinned `paho-mqtt==2.2.0`.
- Verified `server/mqtt_receiver.py`:
  - Python 3.12 execution in `.venv`.
  - Paho MQTT Callback API v2 (`CallbackAPIVersion.VERSION2`).
  - Client ID: `aria-pc-receiver`.
  - Subscribes to `aria/#` with QoS 0.
  - Configurable host/port via `ARIA_MQTT_HOST` and `ARIA_MQTT_PORT` (defaults: `127.0.0.1:1883`).
  - Validates connection and subscription reason codes.
  - Safely handles malformed non-UTF-8 payloads without crashing (`UnicodeDecodeError` caught and logged).
  - Handles connection errors (`OSError` caught and logged, exits with returncode 1).
  - Handles clean Ctrl+C / `KeyboardInterrupt` shutdown using Windows console interrupt handlers, disconnects cleanly, stops loops, and exits 0.
- Created `.env.example` with default host/port.
- Created `tests/test_phase1_mqtt.py` for automated regression testing of all Phase 1 acceptance criteria.

#### 3. Commands Run & Observed Results
1. **Python version check:**
   ```powershell
   .\.venv\Scripts\python.exe --version
   ```
   *Output:* `Python 3.12.9`
2. **Mosquitto broker service and port check:**
   ```powershell
   Get-Service -Name *mosquitto*; Get-NetTCPConnection -LocalPort 1883
   ```
   *Output:* Service `mosquitto` status `Running`; TCP 1883 State `Listen` (PID 6268).
3. **Dependency check:**
   ```powershell
   .\.venv\Scripts\pip.exe list
   ```
   *Output:* `paho-mqtt 2.2.0`, `pip 26.3`
4. **Mosquitto CLI test publication:**
   ```powershell
   & "C:\Program Files\mosquitto\mosquitto_pub.exe" -h 127.0.0.1 -t "aria/test" -m "broker_verification"
   ```
   *Output:* Exit code 0 (publication successful).
5. **Phase 1 Automated Acceptance Test Suite:**
   ```powershell
   .\.venv\Scripts\python.exe -m unittest tests/test_phase1_mqtt.py
   ```
   *Output:*
   ```text
   Ran 4 tests in 5.808s
   OK
   ```

---

### 2026-11-14 — Initial project setup
- Confirmed Mosquitto installed on Windows 11.
- Confirmed local CLI pub/sub test on topic `aria/test`.
- Selected temporary ESP32-C3 Mini publisher approach.
- Arduino IDE remains the selected device IDE.
- Created project-level specification and Cursor build brief.

---

## Current next action (Phase 6)
1. Request user approval to proceed to Phase 6 (ESP32-C3 simulated publisher sketch for Arduino IDE).
2. Create sketch directory `firmware/c3_simulator/` with `c3_simulator.ino` and configuration headers.
3. Store Wi-Fi credentials outside version control (provide `.example.h` template).
4. Connect to user's Wi-Fi and the PC's Mosquitto broker (IP on LAN).
5. Publish clearly labelled simulated telemetry (`source: "simulated"`) and status with LWT using agreed schemas.
6. Verify reception in backend and live visualization on dashboard.
