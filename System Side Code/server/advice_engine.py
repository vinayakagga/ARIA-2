"""Advice Engine Aggregator for ARIA (Phase 4).

Coordinates:
1. Latest telemetry and device status from SQLite database.
2. Resilient Open-Meteo weather service with persistent GPS memory.
3. Deterministic rules engine (independent sensor audit & agricultural guardrails).
4. Hosted NVIDIA NIM API client (OpenAI-compatible chat completion).
5. Deterministic fallback guidance generator.

Enforces:
- 10-minute cooldown on external NVIDIA AI calls with atomic SQLite reservation and thread safety.
- Dynamic device ID resolution (auto-detects active rover e.g. aria-esp32; defaults to aria-esp32, not aria-c3).
- Graceful degradation when sensors, weather, or AI are partially or fully unavailable.
- Re-evaluates live sensor conditions during cooldown instead of blindly reusing stale advice.
- Sanitized exception propagation on genuine programming/database bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import threading
from typing import Any

from server.database import Database
from server.nvidia_client import NVIDIAClient, NVIDIAResponse, NVIDIAStatus
from server.rules import (
    AdviceMode,
    MonitoringThresholds,
    RulesEvaluationResult,
    evaluate_rules,
    generate_deterministic_fallback_guidance,
)
from server.weather import WeatherResponse, WeatherService

log = logging.getLogger("aria.advice_engine")

DEFAULT_COOLDOWN_SECONDS = 600.0  # 10 minutes
DEFAULT_DEVICE_ID = "aria-esp32"
DEVICE_ONLINE_TIMEOUT_SECONDS = 60.0


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class AdviceEngine:
    """Aggregates telemetry, weather, deterministic rules, and NVIDIA AI advice."""

    def __init__(
        self,
        db: Database | None = None,
        weather_service: WeatherService | None = None,
        nvidia_client: NVIDIAClient | None = None,
        thresholds: MonitoringThresholds | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        self.db = db if db is not None else Database()
        self.weather_service = (
            weather_service
            if weather_service is not None
            else WeatherService(db=self.db)
        )
        self.nvidia_client = (
            nvidia_client
            if nvidia_client is not None
            else NVIDIAClient()
        )
        self.thresholds = thresholds if thresholds is not None else MonitoringThresholds()
        self.cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else float(os.environ.get("ADVICE_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
        )
        self._lock = threading.Lock()

    def resolve_device_id(self, device_id: str | None = None) -> str:
        """Resolve effective device ID.

        If explicitly provided, returns that ID.
        Otherwise dynamically determines device ID from the most recent
        telemetry or status record in SQLite, falling back to ARIA_DEVICE_ID
        or 'aria-esp32' (never hardcoding development aria-c3).
        """
        if device_id and device_id.strip():
            return device_id.strip()

        # Check most recent telemetry record
        latest_telem = self.db.get_latest_telemetry()
        if latest_telem and latest_telem.device_id:
            return latest_telem.device_id

        # Check most recent status record
        latest_status = self.db.get_latest_status()
        if latest_status and latest_status.device_id:
            return latest_status.device_id

        return os.environ.get("ARIA_DEVICE_ID", DEFAULT_DEVICE_ID)

    def is_device_online(self, device_id: str, now: datetime) -> bool:
        """Check if rover is online based on latest status record."""
        latest_status = self.db.get_latest_status(device_id=device_id)
        if not latest_status:
            return False
        if latest_status.status.lower() != "online":
            return False
        rec_at = latest_status.received_at
        if rec_at is None:
            return False
        if rec_at.tzinfo is None:
            rec_at = rec_at.replace(tzinfo=timezone.utc)
        age_s = (now - rec_at).total_seconds()
        return age_s <= DEVICE_ONLINE_TIMEOUT_SECONDS

    def get_cooldown_status(
        self, device_id: str, now: datetime
    ) -> tuple[bool, float]:
        """Check whether AI call cooldown is active for device.

        Returns (is_active, remaining_seconds).
        """
        last_call = self.db.get_last_ai_call_at(device_id)
        if not last_call:
            return False, 0.0
        if last_call.tzinfo is None:
            last_call = last_call.replace(tzinfo=timezone.utc)
        elapsed_s = max(0.0, (now - last_call).total_seconds())
        if elapsed_s < self.cooldown_seconds:
            remaining = round(self.cooldown_seconds - elapsed_s, 1)
            return True, remaining
        return False, 0.0

    def get_latest_advice(self, device_id: str | None = None) -> dict[str, Any] | None:
        """Retrieve most recent persisted advice from SQLite."""
        dev_id = self.resolve_device_id(device_id)
        return self.db.get_latest_advice(device_id=dev_id)

    def generate_advice(
        self,
        device_id: str | None = None,
        force_refresh_weather: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate structured agricultural advice coordinating all sub-services.

        1. Resolves device identity.
        2. Queries latest telemetry and device connectivity from SQLite.
        3. Queries localized weather (current GPS fix or persistent last-known GPS).
        4. Runs deterministic rules evaluation (audits sensor health & observations).
        5. Enforces AI cooldown with atomic SQLite attempt reservation and thread safety.
        6. Calls NVIDIA NIM if cooldown elapsed; otherwise runs deterministic fallback.
        7. Persists generated advice to SQLite.
        8. Returns unified structured advice dictionary.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        dev_id = self.resolve_device_id(device_id)

        # 1. Retrieve telemetry and status
        telemetry = self.db.get_latest_telemetry(device_id=dev_id)
        device_online = self.is_device_online(dev_id, now)

        telemetry_timestamp = None
        telemetry_age_seconds = None
        telemetry_freshness = "missing"
        if telemetry is not None:
            ref_time = telemetry.received_at
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            telemetry_age_seconds = max(0.0, (now - ref_time).total_seconds())
            telemetry_timestamp = _iso(ref_time)
            telemetry_freshness = (
                "current" if telemetry_age_seconds <= self.thresholds.telemetry_stale_seconds else "stale"
            )

        # 2. Retrieve localized weather (gracefully handles GPS loss and API outages)
        weather_resp: WeatherResponse = self.weather_service.get_weather(
            telemetry=telemetry,
            device_id=dev_id,
            force_refresh=force_refresh_weather,
            now=now,
        )
        weather_dict = weather_resp.to_dict()

        # Determine GPS provenance for advice context
        gps = getattr(telemetry, "gps", None) if telemetry else None
        if gps and gps.fix and gps.latitude is not None and gps.longitude is not None:
            gps_provenance = "current_fix"
        elif weather_resp.location_source == "LAST_KNOWN_LOCATION":
            gps_provenance = "last_known_fix"
        else:
            gps_provenance = "unavailable"

        telemetry_snapshot = None
        if telemetry is not None:
            telemetry_snapshot = {
                "temperature_c": telemetry.temperature_c,
                "humidity_pct": telemetry.humidity_pct,
                "light_lux": telemetry.light_lux,
                "mq135_raw": telemetry.mq135_raw,
                "gps_fix": bool(telemetry.gps and telemetry.gps.fix),
                "gps_satellites": telemetry.gps.satellites if telemetry.gps else None,
            }

        # 3. Evaluate deterministic rules
        rules_result: RulesEvaluationResult = evaluate_rules(
            telemetry=telemetry,
            device_online=device_online,
            weather=weather_dict,
            thresholds=self.thresholds,
            current_time=now,
        )

        # Base limitations gathered from rules + weather
        combined_limitations = list(rules_result.limitations)

        # 4. Determine AI vs Deterministic Fallback with thread-safe atomic cooldown check
        ai_response: NVIDIAResponse | None = None
        provider = "deterministic_fallback"
        summary: str = ""
        recommendations: list[dict[str, Any]] = []
        cooldown_active = False
        remaining_s = 0.0

        with self._lock:
            cooldown_active, remaining_s = self.get_cooldown_status(dev_id, now)

            if cooldown_active:
                log.info(
                    "AI cooldown active for %s (%.1fs remaining); using deterministic guidance",
                    dev_id,
                    remaining_s,
                )
                combined_limitations.append(
                    f"AI generation cooldown active ({remaining_s:.0f}s remaining). "
                    "Guidance generated from live deterministic rule evaluations."
                )
            elif not self.nvidia_client.is_configured():
                log.info("NVIDIA API key not configured; using deterministic fallback guidance")
                combined_limitations.append(
                    "NVIDIA AI service is unconfigured. Guidance generated strictly by deterministic rule evaluator."
                )
            else:
                # Cooldown has elapsed and API key is present.
                # Atomically record the AI call attempt in SQLite BEFORE making the network request.
                # This guarantees that concurrent requests and failed attempts still activate cooldown.
                try:
                    self.db.record_ai_call_attempt(dev_id, now)
                except Exception:
                    log.exception("failed to record AI call attempt for %s in SQLite", dev_id)

                # Set cooldown as now active for immediate subsequent requests
                cooldown_active = True
                remaining_s = self.cooldown_seconds

                # Execute NVIDIA NIM call
                ai_response = self.nvidia_client.generate_advice(
                    rules_result=rules_result,
                    weather=weather_dict,
                )

        # 5. Process AI result or Generate Deterministic Guidance
        if ai_response and ai_response.status == NVIDIAStatus.AVAILABLE and ai_response.summary:
            provider = "nvidia_nim"
            summary = ai_response.summary

            # Base recommendations on confirmed rule observations
            fallback_data = generate_deterministic_fallback_guidance(rules_result)
            recommendations = fallback_data.get("recommendations", [])
            log.info("successfully generated NVIDIA NIM advice for %s (mode=%s)", dev_id, rules_result.advice_mode.value)
        else:
            # Deterministic fallback path (used during cooldown, unconfigured key, or API error)
            provider = "deterministic_fallback"
            if ai_response and ai_response.status == NVIDIAStatus.UNAVAILABLE:
                err_msg = ai_response.error or "Unknown API error"
                log.warning("NVIDIA NIM call failed for %s: %s; falling back to deterministic rules", dev_id, err_msg)
                combined_limitations.append(
                    f"NVIDIA AI generation failed ({err_msg}). Operating in deterministic fallback mode."
                )

            fallback_data = generate_deterministic_fallback_guidance(rules_result)
            summary = fallback_data.get("summary", "")
            recommendations = fallback_data.get("recommendations", [])

        # 6. Assemble Unified Advice Response Payload
        advice_payload: dict[str, Any] = {
            "status": "available",
            "device_id": dev_id,
            "device_online": device_online,
            "advice_mode": rules_result.advice_mode.value,
            "provider": provider,
            "summary": summary,
            "recommendations": recommendations,
            "observations": [
                {
                    "rule_id": o.rule_id,
                    "title": o.title,
                    "explanation": o.explanation,
                    "severity": o.severity.value,
                    "source": o.source,
                }
                for o in rules_result.observations
            ],
            "sensor_health": [
                {
                    "sensor": h.sensor,
                    "state": h.state.value,
                    "detail": h.detail,
                }
                for h in rules_result.sensor_health
            ],
            "weather": weather_dict,
            "limitations": combined_limitations,
            "cooldown": {
                "active": cooldown_active,
                "remaining_seconds": remaining_s,
                "cooldown_seconds": self.cooldown_seconds,
            },
            "generated_at": _iso(now),
            # Telemetry provenance: identifies the exact sensor snapshot used for evaluation
            # so the dashboard can warn when advice is based on older readings.
            "telemetry_context": {
                "telemetry_timestamp": telemetry_timestamp,
                "telemetry_age_seconds": round(telemetry_age_seconds, 1) if telemetry_age_seconds is not None else None,
                "telemetry_freshness": telemetry_freshness,
                "snapshot": telemetry_snapshot,
            },
            # GPS provenance: distinguishes current fix vs last-known stored coordinates vs unavailable.
            "gps_provenance": gps_provenance,
        }

        if ai_response and ai_response.status == NVIDIAStatus.AVAILABLE:
            advice_payload["ai_metadata"] = {
                "model": ai_response.model,
                "prompt_tokens": ai_response.prompt_tokens,
                "completion_tokens": ai_response.completion_tokens,
                "finish_reason": ai_response.finish_reason,
            }

        # 7. Persist to SQLite
        try:
            self.db.save_latest_advice(
                device_id=dev_id,
                advice_payload=advice_payload,
                created_at=now,
            )
        except Exception:
            log.exception("failed to persist generated advice for %s", dev_id)

        return advice_payload
