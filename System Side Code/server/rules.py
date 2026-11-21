"""Deterministic Rules Engine for ARIA Advice Engine (Phase 1).

Evaluates available telemetry and external weather context independently and deterministically.
Distinguishes:
- Individual missing sensor readings (null/unresponsive on active telemetry).
- Stale telemetry (> threshold age).
- Device offline / disconnected state.
- Sensor health vs agricultural environmental observations.
- Configurable monitoring thresholds (not universal agricultural standards).
- Advice mode classification: FULL_CONTEXT, LIMITED_CONTEXT, GENERAL_GUIDANCE.
- Deterministic guidance fallback when AI interpretation is unavailable.

Strict guardrails:
- Never converts MQ-135 raw ADC to ppm or CO2.
- Never infers soil moisture from ambient humidity.
- Never infers crop temperature from DHT22 air temperature.
- Never fabricates crop-specific or irrigation advice.
- Explicitly notes GPS HDOP is unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any

from server.telemetry import TelemetryMessage

log = logging.getLogger("aria.rules")


class AdviceMode(str, Enum):
    FULL_CONTEXT = "FULL_CONTEXT"
    LIMITED_CONTEXT = "LIMITED_CONTEXT"
    GENERAL_GUIDANCE = "GENERAL_GUIDANCE"


class SensorState(str, Enum):
    HEALTHY = "healthy"
    UNRESPONSIVE_NULL = "unresponsive_null"
    STALE = "stale"
    DEVICE_OFFLINE = "device_offline"
    NOT_EQUIPPED = "not_equipped"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ALERT = "alert"


@dataclass(frozen=True)
class SensorHealthRecord:
    sensor: str
    state: SensorState
    detail: str


@dataclass(frozen=True)
class RuleObservation:
    rule_id: str
    title: str
    explanation: str
    severity: Severity
    source: str  # "sensor" | "weather" | "system" | "combined"


@dataclass
class MonitoringThresholds:
    """Configurable monitoring thresholds for environmental rules.

    Clearly designated as operational monitoring triggers, NOT universal
    or crop-specific agricultural standards.
    """
    temp_high_c: float = 35.0
    temp_low_c: float = 4.0
    humidity_high_pct: float = 85.0
    humidity_low_pct: float = 30.0
    light_high_lux: float = 60000.0
    light_low_lux: float = 5000.0
    mq135_elevated_raw: int = 2500
    temp_weather_delta_c: float = 5.0
    precip_prob_warning_pct: float = 60.0
    telemetry_stale_seconds: float = 15.0


@dataclass
class RulesEvaluationResult:
    advice_mode: AdviceMode
    available_inputs: list[str]
    unavailable_inputs: list[str]
    sensor_health: list[SensorHealthRecord]
    observations: list[RuleObservation]
    limitations: list[str]
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "advice_mode": self.advice_mode.value,
            "available_inputs": self.available_inputs,
            "unavailable_inputs": self.unavailable_inputs,
            "sensor_health": [
                {"sensor": h.sensor, "state": h.state.value, "detail": h.detail}
                for h in self.sensor_health
            ],
            "observations": [
                {
                    "rule_id": o.rule_id,
                    "title": o.title,
                    "explanation": o.explanation,
                    "severity": o.severity.value,
                    "source": o.source,
                }
                for o in self.observations
            ],
            "limitations": self.limitations,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def evaluate_rules(
    telemetry: TelemetryMessage | None,
    device_online: bool = True,
    weather: dict[str, Any] | None = None,
    thresholds: MonitoringThresholds | None = None,
    current_time: datetime | None = None,
) -> RulesEvaluationResult:
    """Evaluate deterministic rules against available telemetry and weather.

    Parameters:
        telemetry: Latest TelemetryMessage from database or client, or None.
        device_online: Whether rover device is currently online (from /api/v1/status/latest).
        weather: Optional cached or fresh weather dictionary.
        thresholds: Configurable thresholds (defaults to MonitoringThresholds()).
        current_time: Evaluation UTC timestamp (defaults to datetime.now(timezone.utc)).
    """
    if thresholds is None:
        thresholds = MonitoringThresholds()
    if current_time is None:
        current_time = datetime.now(timezone.utc)

    available_inputs: list[str] = []
    unavailable_inputs: list[str] = []
    sensor_health: list[SensorHealthRecord] = []
    observations: list[RuleObservation] = []
    limitations: list[str] = []

    # 1. Determine Telemetry Freshness & Device Presence
    is_telemetry_fresh = False
    telemetry_age_s: float | None = None

    if telemetry is not None:
        ref_time = telemetry.received_at
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)
        telemetry_age_s = max(0.0, (current_time - ref_time).total_seconds())
        is_telemetry_fresh = telemetry_age_s <= thresholds.telemetry_stale_seconds

    # 2. Audit Sensor Health Independently
    # Physical sensor list: temperature_c, humidity_pct, light_lux, mq135_raw, gps
    sensor_map = {
        "temperature_c": getattr(telemetry, "temperature_c", None) if telemetry else None,
        "humidity_pct": getattr(telemetry, "humidity_pct", None) if telemetry else None,
        "light_lux": getattr(telemetry, "light_lux", None) if telemetry else None,
        "mq135_raw": getattr(telemetry, "mq135_raw", None) if telemetry else None,
    }

    # Audit 4 primary environmental sensors
    for sensor_name, sensor_val in sensor_map.items():
        if not device_online and telemetry is None:
            sensor_health.append(SensorHealthRecord(
                sensor=sensor_name,
                state=SensorState.DEVICE_OFFLINE,
                detail="Rover device is offline; no connection established.",
            ))
            unavailable_inputs.append(sensor_name)
        elif telemetry is None:
            sensor_health.append(SensorHealthRecord(
                sensor=sensor_name,
                state=SensorState.UNRESPONSIVE_NULL,
                detail="No telemetry record exists in storage.",
            ))
            unavailable_inputs.append(sensor_name)
        elif not is_telemetry_fresh:
            # Telemetry exists but is stale
            if sensor_val is not None:
                sensor_health.append(SensorHealthRecord(
                    sensor=sensor_name,
                    state=SensorState.STALE,
                    detail=f"Last reading recorded {telemetry_age_s:.0f}s ago; telemetry is stale.",
                ))
                available_inputs.append(sensor_name)
            else:
                sensor_health.append(SensorHealthRecord(
                    sensor=sensor_name,
                    state=SensorState.UNRESPONSIVE_NULL,
                    detail="Reading was null/unresponsive in stale telemetry record.",
                ))
                unavailable_inputs.append(sensor_name)
        else:
            # Fresh telemetry
            if sensor_val is not None:
                sensor_health.append(SensorHealthRecord(
                    sensor=sensor_name,
                    state=SensorState.HEALTHY,
                    detail=f"Live valid reading available ({sensor_val}).",
                ))
                available_inputs.append(sensor_name)
            else:
                sensor_health.append(SensorHealthRecord(
                    sensor=sensor_name,
                    state=SensorState.UNRESPONSIVE_NULL,
                    detail="Sensor unread or disconnected on rover (null reading in fresh payload).",
                ))
                unavailable_inputs.append(sensor_name)

    # Audit GPS
    gps_data = getattr(telemetry, "gps", None) if telemetry else None
    gps_has_fix = bool(gps_data and gps_data.fix and gps_data.latitude is not None and gps_data.longitude is not None)

    if not device_online and telemetry is None:
        sensor_health.append(SensorHealthRecord(
            sensor="gps",
            state=SensorState.DEVICE_OFFLINE,
            detail="Rover device offline; GPS status unconfirmed.",
        ))
        unavailable_inputs.append("gps")
    elif telemetry is None:
        sensor_health.append(SensorHealthRecord(
            sensor="gps",
            state=SensorState.UNRESPONSIVE_NULL,
            detail="No telemetry available for GPS status.",
        ))
        unavailable_inputs.append("gps")
    elif not is_telemetry_fresh:
        if gps_has_fix:
            sensor_health.append(SensorHealthRecord(
                sensor="gps",
                state=SensorState.STALE,
                detail=f"GPS lock recorded in stale telemetry ({telemetry_age_s:.0f}s ago).",
            ))
            available_inputs.append("gps")
        else:
            sensor_health.append(SensorHealthRecord(
                sensor="gps",
                state=SensorState.UNRESPONSIVE_NULL,
                detail="GPS fix not established in stale telemetry.",
            ))
            unavailable_inputs.append("gps")
    else:
        if gps_has_fix:
            sensor_health.append(SensorHealthRecord(
                sensor="gps",
                state=SensorState.HEALTHY,
                detail=f"Outdoor 2D/3D fix locked ({gps_data.satellites or 0} satellites). Horizontal precision (HDOP) unverified.",
            ))
            available_inputs.append("gps")
        else:
            sensor_health.append(SensorHealthRecord(
                sensor="gps",
                state=SensorState.UNRESPONSIVE_NULL,
                detail="No GPS satellite fix locked. Coordinates unavailable.",
            ))
            unavailable_inputs.append("gps")

    # Audit Weather
    weather_valid = False
    if weather and weather.get("status") in ("available", "cached_prior") and weather.get("current"):
        weather_valid = True
        available_inputs.append("weather")
    else:
        unavailable_inputs.append("weather")

    # 3. Add Health-Level Observations (System/Operational)
    if telemetry is not None and not is_telemetry_fresh:
        observations.append(RuleObservation(
            rule_id="SYS_TELEMETRY_STALE",
            title="Telemetry Stale",
            explanation=f"Telemetry data is {telemetry_age_s:.0f} seconds old. Readings reflect previous field conditions, not live microclimate.",
            severity=Severity.WARNING,
            source="system",
        ))
        limitations.append(f"Rover telemetry is {telemetry_age_s:.0f}s old. Observations based on historical sample.")

    if not device_online:
        observations.append(RuleObservation(
            rule_id="SYS_DEVICE_OFFLINE",
            title="Rover Inactive / Offline",
            explanation="Rover device is currently unreachable over Wi-Fi. Live sensor telemetry is stopped.",
            severity=Severity.WARNING,
            source="system",
        ))
        limitations.append("Rover device is currently offline.")

    # 4. Evaluate Environmental Rules Independently

    # Rule: Ambient Temperature
    temp_val = sensor_map["temperature_c"]
    if temp_val is not None:
        if temp_val >= thresholds.temp_high_c:
            observations.append(RuleObservation(
                rule_id="ENV_TEMP_HIGH",
                title="Elevated Ambient Temperature",
                explanation=f"Ambient temperature is {temp_val:.1f}°C (exceeds {thresholds.temp_high_c:.1f}°C trigger). Indicates high atmospheric heat. Note: Reflects ambient air, not crop canopy temperature.",
                severity=Severity.WARNING,
                source="sensor",
            ))
        elif temp_val <= thresholds.temp_low_c:
            observations.append(RuleObservation(
                rule_id="ENV_TEMP_LOW",
                title="Low Ambient Temperature / Frost Watch",
                explanation=f"Ambient temperature is {temp_val:.1f}°C (below {thresholds.temp_low_c:.1f}°C trigger). Monitor for cool-temperature field conditions.",
                severity=Severity.WARNING,
                source="sensor",
            ))
        else:
            observations.append(RuleObservation(
                rule_id="ENV_TEMP_NOMINAL",
                title="Moderate Ambient Temperature",
                explanation=f"Ambient air temperature is {temp_val:.1f}°C, within typical daytime operating bounds.",
                severity=Severity.INFO,
                source="sensor",
            ))
    else:
        limitations.append("DHT22 ambient temperature sensor is unavailable.")

    # Rule: Relative Humidity
    hum_val = sensor_map["humidity_pct"]
    if hum_val is not None:
        if hum_val >= thresholds.humidity_high_pct:
            observations.append(RuleObservation(
                rule_id="ENV_HUM_HIGH",
                title="Elevated Ambient Relative Humidity",
                explanation=f"Ambient relative humidity is {hum_val:.1f}% (exceeds {thresholds.humidity_high_pct:.1f}% trigger). Elevated atmospheric moisture increases canopy condensation risk. Strictly reflects air humidity, NOT soil moisture.",
                severity=Severity.WARNING,
                source="sensor",
            ))
        elif hum_val <= thresholds.humidity_low_pct:
            observations.append(RuleObservation(
                rule_id="ENV_HUM_LOW",
                title="Low Ambient Relative Humidity",
                explanation=f"Ambient relative humidity is {hum_val:.1f}% (below {thresholds.humidity_low_pct:.1f}% trigger). Atmospheric evaporative demand is elevated. Reflects air moisture only, not root zone soil moisture.",
                severity=Severity.INFO,
                source="sensor",
            ))
        else:
            observations.append(RuleObservation(
                rule_id="ENV_HUM_NOMINAL",
                title="Moderate Ambient Humidity",
                explanation=f"Ambient relative humidity is {hum_val:.1f}%, within standard atmospheric ranges.",
                severity=Severity.INFO,
                source="sensor",
            ))
    else:
        limitations.append("DHT22 relative humidity sensor is unavailable.")

    # Rule: Ambient Light
    lux_val = sensor_map["light_lux"]
    if lux_val is not None:
        if lux_val >= thresholds.light_high_lux:
            observations.append(RuleObservation(
                rule_id="ENV_LIGHT_HIGH",
                title="Intense Direct Solar Illuminance",
                explanation=f"Ambient light level is {lux_val:.0f} lux. Direct high-intensity solar radiation detected.",
                severity=Severity.INFO,
                source="sensor",
            ))
        elif lux_val <= thresholds.light_low_lux:
            observations.append(RuleObservation(
                rule_id="ENV_LIGHT_LOW",
                title="Low Ambient Illuminance",
                explanation=f"Ambient light level is {lux_val:.0f} lux. Indicates heavy cloud cover, low sun angle, or dawn/dusk conditions.",
                severity=Severity.INFO,
                source="sensor",
            ))
        else:
            observations.append(RuleObservation(
                rule_id="ENV_LIGHT_MODERATE",
                title="Standard Ambient Illuminance",
                explanation=f"Ambient light level is {lux_val:.0f} lux, reflecting moderate diffuse daylight.",
                severity=Severity.INFO,
                source="sensor",
            ))
    else:
        limitations.append("BH1750 ambient light sensor is unavailable.")

    # Rule: MQ-135 Raw Analogue ADC (Strict Guardrail)
    mq_val = sensor_map["mq135_raw"]
    if mq_val is not None:
        if mq_val >= thresholds.mq135_elevated_raw:
            observations.append(RuleObservation(
                rule_id="ENV_MQ135_ELEVATED",
                title="Elevated MQ-135 Analogue Response",
                explanation=(
                    f"MQ-135 reading is {mq_val} raw ADC counts (trigger: {thresholds.mq135_elevated_raw}). "
                    "Uncalibrated raw analogue sensor voltage is elevated above baseline. "
                    "Notice: Does NOT represent calibrated air quality index, CO2 ppm, or specific chemical concentration; "
                    "raw ADC counts cannot establish a pollution level without calibration and a validated baseline."
                ),
                severity=Severity.WARNING,
                source="sensor",
            ))
        else:
            observations.append(RuleObservation(
                rule_id="ENV_MQ135_BASELINE",
                title="Baseline MQ-135 Analogue Response",
                explanation=(
                    f"MQ-135 reading is {mq_val} raw ADC counts. Uncalibrated analogue response is within typical non-elevated sensor range. "
                    "Notice: Raw ADC counts cannot establish normal air quality or a pollution level without calibration and a validated baseline."
                ),
                severity=Severity.INFO,
                source="sensor",
            ))
    else:
        limitations.append("MQ-135 gas sensor is unavailable.")

    # Rule: GPS Precision & Weather Cross-Check
    if gps_has_fix:
        limitations.append("NEO-6M GPS satellite lock established, but horizontal precision (HDOP) is unverified in telemetry contract.")
    else:
        limitations.append("NEO-6M GPS has no valid outdoor fix. Localized weather retrieval depends on previously cached coordinates or is unavailable.")

    # Weather Rules (if weather is available)
    if weather_valid:
        w_current = weather.get("current", {})
        w_temp = w_current.get("temperature_2m")
        w_precip = w_current.get("precipitation")
        w_wind = w_current.get("wind_speed_10m")

        # Discrepancy between rover ambient temperature and regional weather station
        if temp_val is not None and w_temp is not None:
            delta = abs(temp_val - w_temp)
            if delta >= thresholds.temp_weather_delta_c:
                observations.append(RuleObservation(
                    rule_id="WTH_TEMP_MICROCLIMATE_DELTA",
                    title="Microclimate / Regional Forecast Temperature Variance",
                    explanation=(
                        f"Rover measured on-ground ambient temperature ({temp_val:.1f}°C) differs by {delta:.1f}°C "
                        f"from regional forecast station temperature ({w_temp:.1f}°C). "
                        "Reflects localized ground-level microclimate variation."
                    ),
                    severity=Severity.INFO,
                    source="combined",
                ))

        # Active Precipitation in Weather Forecast
        if w_precip is not None and w_precip > 0.0:
            observations.append(RuleObservation(
                rule_id="WTH_PRECIPITATION_ACTIVE",
                title="Regional Precipitation Reported",
                explanation=f"Regional weather station reports active precipitation ({w_precip} mm). Check field surface conditions.",
                severity=Severity.WARNING,
                source="weather",
            ))

        # Hourly precipitation probability check
        w_hourly = weather.get("hourly", {})
        probs = w_hourly.get("precipitation_probability", [])
        if probs and any(p >= thresholds.precip_prob_warning_pct for p in probs[:6]):
            max_prob = max(probs[:6])
            observations.append(RuleObservation(
                rule_id="WTH_PRECIP_PROB_HIGH",
                title="Elevated Rain Probability in Forecast",
                explanation=f"Regional forecast indicates elevated precipitation probability ({max_prob}%) within next 6 hours.",
                severity=Severity.WARNING,
                source="weather",
            ))

        # Wind speed monitoring
        if w_wind is not None and w_wind >= 25.0:
            observations.append(RuleObservation(
                rule_id="WTH_WIND_ELEVATED",
                title="Elevated Regional Wind Speed",
                explanation=f"Regional forecast reports wind speeds of {w_wind:.1f} km/h. Atmospheric dispersion and drift potential elevated.",
                severity=Severity.INFO,
                source="weather",
            ))
    else:
        limitations.append("Localized Open-Meteo weather forecast data is currently unavailable.")

    # 5. Determine Advice Mode (Refined 3-Tier Classification)
    # Valid physical sensors = count of non-null among (temp, hum, lux, mq135)
    valid_physical_sensor_count = sum(1 for val in sensor_map.values() if val is not None)

    if is_telemetry_fresh and valid_physical_sensor_count >= 2 and weather_valid:
        mode = AdviceMode.FULL_CONTEXT
    elif valid_physical_sensor_count >= 1 or weather_valid:
        mode = AdviceMode.LIMITED_CONTEXT
    else:
        mode = AdviceMode.GENERAL_GUIDANCE

    return RulesEvaluationResult(
        advice_mode=mode,
        available_inputs=available_inputs,
        unavailable_inputs=unavailable_inputs,
        sensor_health=sensor_health,
        observations=observations,
        limitations=limitations,
        evaluated_at=current_time,
    )


def generate_deterministic_fallback_guidance(rules_result: RulesEvaluationResult) -> dict[str, Any]:
    """Generate structured general agricultural guidance when NVIDIA AI is unavailable.

    Guarantees:
    - Never fabricates crop-specific recommendations.
    - Never recommends irrigation, fertilization, or chemical applications.
    - Synthesizes available rule observations into cautious general stewardship notes.
    """
    mode = rules_result.advice_mode.value
    obs_titles = [o.title for o in rules_result.observations]

    if mode == AdviceMode.FULL_CONTEXT.value:
        summary = (
            f"Evaluated {len(rules_result.available_inputs)} environmental inputs against monitoring thresholds. "
            f"Local telemetry and weather forecast are operational. {len(rules_result.observations)} observation(s) noted."
        )
    elif mode == AdviceMode.LIMITED_CONTEXT.value:
        missing_str = ", ".join(rules_result.unavailable_inputs)
        summary = (
            f"Operating under limited context ({len(rules_result.available_inputs)} available inputs). "
            f"Missing inputs: {missing_str}. Guidance is conservative and based only on confirmed evidence."
        )
    else:
        summary = (
            "General agricultural monitoring guidance. No live ARIA physical sensors or localized weather "
            "data are currently available. The following principles reflect general seasonal stewardship."
        )

    # General recommendations derived purely from general stewardship and available observations
    recommendations = []
    has_temp_high = any(o.rule_id == "ENV_TEMP_HIGH" for o in rules_result.observations)
    has_rain_warning = any("Precipitation" in o.title or "Rain" in o.title for o in rules_result.observations)
    has_elevated_mq = any(o.rule_id == "ENV_MQ135_ELEVATED" for o in rules_result.observations)

    if has_temp_high:
        recommendations.append({
            "title": "Monitor Field Equipment and Ground Surface Heat",
            "explanation": "High ambient air temperatures increase thermal stress on mobile field electronics and machinery batteries.",
            "confidence": "high",
        })

    if has_rain_warning:
        recommendations.append({
            "title": "Verify Field Accessibility Before Deployment",
            "explanation": "Forecasted precipitation may impact ground traction and soil compaction for rover wheel movement.",
            "confidence": "medium",
        })

    if has_elevated_mq:
        recommendations.append({
            "title": "Check Uncalibrated Gas Sensor Operating Environment",
            "explanation": "Elevated raw ADC voltage may indicate sensor preheat status, organic vapors, or nearby vehicle exhaust. Do not interpret as specific toxic pollutant without calibrated instruments.",
            "confidence": "medium",
        })

    if not recommendations:
        recommendations.append({
            "title": "Maintain Routine Visual Field Inspection",
            "explanation": "Ambient rover telemetry serves as baseline monitoring; periodic physical observation confirms overall canopy and soil conditions.",
            "confidence": "low",
        })

    # Standard general agricultural stewardship advice when context is general
    if mode == AdviceMode.GENERAL_GUIDANCE.value:
        recommendations.append({
            "title": "Consult Local Agronomic Calendar",
            "explanation": "In the absence of live rover telemetry, regional extension service guidelines provide baseline seasonal management timelines.",
            "confidence": "low",
        })

    return {
        "status": "available",
        "provider": "deterministic_fallback",
        "summary": summary,
        "observations": [
            {
                "title": o.title,
                "explanation": o.explanation,
                "source": o.source,
            }
            for o in rules_result.observations
        ],
        "recommendations": recommendations,
        "limitations": rules_result.limitations + [
            "NVIDIA AI service is unavailable. Guidance generated strictly by deterministic rule evaluator.",
            "No crop type, soil moisture, or disease data available; recommendations are non-specific.",
        ],
        "generated_at": rules_result.evaluated_at.isoformat(),
    }
