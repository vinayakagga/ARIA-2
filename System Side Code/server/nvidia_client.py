"""NVIDIA NIM API Client for ARIA Advice Engine (Phase 3).

Provides AI-generated interpretation of deterministic rule observations via
NVIDIA's hosted NIM API (OpenAI-compatible chat completions endpoint).

Design principles:
- Single responsibility: build prompt, call API, return structured response.
- Uses Python standard library only (urllib.request, json). No external HTTP frameworks.
- Full fault tolerance: any error (network, auth, rate-limit, quota, parse)
  returns a structured NVIDIAResponse with available=False.
- Deterministic rules (Phase 1) drive content; AI only *interprets* confirmed evidence.
- Hard token and prompt limits to control cost and latency.
- Strict agricultural guardrails mirrored from rules.py:
    - Never assert calibrated CO₂, ppm, or soil moisture values.
    - Never recommend specific crop treatment, pesticide, or irrigation.
    - Never fabricate unavailable sensor data.
    - Acknowledge uncertainty and data limitations explicitly.
- API key read from NVIDIA_API_KEY environment variable only.
- Model configurable via NVIDIA_MODEL environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
import threading
from typing import Any
import urllib.error
import urllib.request

from server.rules import AdviceMode, RulesEvaluationResult

log = logging.getLogger("aria.nvidia_client")

# ---------------------------------------------------------------------------
# Configuration defaults (all overridable via environment variables)
# ---------------------------------------------------------------------------
NVIDIA_API_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CHAT_ENDPOINT = f"{NVIDIA_API_BASE_URL}/chat/completions"

DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.3   # Low temperature for conservative, grounded outputs

# System prompt preamble establishing guardrails for all AI responses
_SYSTEM_PROMPT = """\
You are ARIA's agricultural monitoring assistant. ARIA is an environmental \
monitoring rover equipped with sensors: DHT22 (ambient air temperature and \
relative humidity), BH1750 (ambient light in lux), MQ-135 (uncalibrated \
analogue gas sensor, raw ADC 0–4095), and NEO-6M GPS.

You receive structured observations from a deterministic rule engine and \
optionally localized weather data. Your task is to interpret the confirmed \
evidence and offer cautious, general-purpose observations.

STRICT GUARDRAILS — you MUST follow these at all times:
1. MQ-135 provides raw ADC counts only. Never convert to ppm, CO₂ \
concentration, or any calibrated air-quality index. If you mention MQ-135, \
state only that the raw analogue reading is elevated or at baseline.
2. DHT22 measures ambient AIR temperature and humidity — not soil, canopy, \
or root-zone temperature. Never infer soil moisture from humidity readings.
3. Never recommend specific crop varieties, pesticide applications, \
fertilization rates, or irrigation volumes.
4. Never invent sensor readings that are absent or null. If a sensor is \
unavailable, acknowledge the gap honestly.
5. If data is limited or absent, give conservative general guidance only. \
Never fabricate specifics to fill gaps.
6. Keep your response brief, grounded, and practical for a field operator.
7. Format: a short summary paragraph (2–4 sentences) followed by \
2–4 concise bullet-point observations or recommendations. No markdown headers.
"""


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

class NVIDIAStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass
class NVIDIAResponse:
    """Structured result from NVIDIA NIM API call."""
    status: NVIDIAStatus
    model: str | None
    advice_mode: str
    summary: str | None
    raw_text: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    error: str | None
    error_code: int | None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "model": self.model,
            "advice_mode": self.advice_mode,
            "summary": self.summary,
            "raw_text": self.raw_text,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "finish_reason": self.finish_reason,
            "error": self.error,
            "error_code": self.error_code,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def unavailable(
        cls,
        advice_mode: str,
        error: str,
        error_code: int | None = None,
        model: str | None = None,
    ) -> "NVIDIAResponse":
        return cls(
            status=NVIDIAStatus.UNAVAILABLE,
            model=model,
            advice_mode=advice_mode,
            summary=None,
            raw_text=None,
            prompt_tokens=None,
            completion_tokens=None,
            finish_reason=None,
            error=error,
            error_code=error_code,
        )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _fmt_sensor(label: str, value: Any, unit: str = "", available: bool = True) -> str:
    if not available or value is None:
        return f"  - {label}: UNAVAILABLE"
    return f"  - {label}: {value}{unit}"


def build_user_prompt(
    rules_result: RulesEvaluationResult,
    weather: dict[str, Any] | None = None,
) -> str:
    """Build a focused, fact-grounded user prompt from rule evaluation output.

    Only confirmed facts from the rules engine are included. No values are
    fabricated for missing sensors.
    """
    lines: list[str] = []
    mode = rules_result.advice_mode.value

    lines.append(f"ADVICE CONTEXT: {mode}")
    lines.append("")

    # --- Sensor Health Summary ---
    lines.append("SENSOR STATUS:")
    for health in rules_result.sensor_health:
        state = health.state.value.upper()
        lines.append(f"  - {health.sensor}: {state} — {health.detail}")
    lines.append("")

    # --- Rule Observations ---
    if rules_result.observations:
        lines.append("DETERMINISTIC RULE OBSERVATIONS:")
        for obs in rules_result.observations:
            sev = obs.severity.value.upper()
            lines.append(f"  [{sev}] {obs.rule_id}: {obs.title}")
            lines.append(f"    {obs.explanation}")
        lines.append("")
    else:
        lines.append("DETERMINISTIC RULE OBSERVATIONS: none triggered.")
        lines.append("")

    # --- Weather Context ---
    if weather and weather.get("status") == "available" and weather.get("current"):
        curr = weather["current"]
        lines.append("LOCALIZED WEATHER CONTEXT (Open-Meteo forecast, not direct measurement):")
        t = curr.get("temperature_c") or curr.get("temperature_2m")
        h = curr.get("humidity_pct") or curr.get("relative_humidity_2m")
        p = curr.get("precipitation_mm") or curr.get("precipitation")
        w = curr.get("wind_speed_kmh") or curr.get("wind_speed_10m")
        lines.append(f"  - Regional air temperature: {t:.1f}°C" if t is not None else "  - Regional air temperature: unavailable")
        lines.append(f"  - Regional relative humidity: {h:.1f}%" if h is not None else "  - Regional relative humidity: unavailable")
        lines.append(f"  - Current precipitation: {p:.1f} mm" if p is not None else "  - Current precipitation: unavailable")
        lines.append(f"  - Wind speed: {w:.1f} km/h" if w is not None else "  - Wind speed: unavailable")

        forecast = weather.get("forecast") or {}
        if forecast:
            prob = forecast.get("precipitation_probability_next_6h_pct")
            rain_6h = forecast.get("precipitation_next_6h_mm")
            if prob is not None:
                lines.append(f"  - Rain probability (next 6h): {prob}%")
            if rain_6h is not None:
                lines.append(f"  - Expected rainfall (next 6h): {rain_6h:.1f} mm")

        loc_src = weather.get("location_source", "unknown")
        limitations = weather.get("limitations", [])
        lines.append(f"  - Location source: {loc_src}")
        if limitations:
            lines.append(f"  - Weather limitations: {'; '.join(limitations[:2])}")
        lines.append("")
    else:
        lines.append("LOCALIZED WEATHER CONTEXT: unavailable.")
        lines.append("")

    # --- Known Limitations ---
    if rules_result.limitations:
        lines.append("KNOWN LIMITATIONS:")
        for lim in rules_result.limitations:
            lines.append(f"  - {lim}")
        lines.append("")

    # --- Instruction ---
    if mode == AdviceMode.FULL_CONTEXT.value:
        lines.append(
            "Provide a brief, grounded interpretation of the above field conditions. "
            "Reference only confirmed sensor readings and forecast data. "
            "Do not recommend specific crop treatments, irrigation volumes, or pesticide use."
        )
    elif mode == AdviceMode.LIMITED_CONTEXT.value:
        lines.append(
            "Data is partially available. Provide cautious observations based strictly on "
            "confirmed evidence. Clearly note which gaps prevent more specific guidance. "
            "Do not fabricate values for unavailable sensors."
        )
    else:
        lines.append(
            "No live sensor data or localized weather is available. "
            "Provide only conservative general agricultural monitoring principles. "
            "Do not make location-specific or crop-specific claims."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class NVIDIAClient:
    """Thread-safe NVIDIA NIM API client for ARIA Advice Engine."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY", "")
        self.model = (
            model
            or os.environ.get("NVIDIA_MODEL", DEFAULT_MODEL)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else float(os.environ.get("NVIDIA_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_S)))
        )
        self.max_tokens = (
            max_tokens
            if max_tokens is not None
            else int(os.environ.get("NVIDIA_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        )
        self.temperature = (
            temperature
            if temperature is not None
            else float(os.environ.get("NVIDIA_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        )
        self.chat_endpoint = (
            f"{(base_url or NVIDIA_API_BASE_URL).rstrip('/')}/chat/completions"
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if an API key has been provided."""
        return bool(self.api_key and self.api_key.strip())

    def generate_advice(
        self,
        rules_result: RulesEvaluationResult,
        weather: dict[str, Any] | None = None,
    ) -> NVIDIAResponse:
        """Call NVIDIA NIM to generate AI agricultural advice.

        Returns NVIDIAResponse.unavailable() on any failure — never raises.
        """
        mode_str = rules_result.advice_mode.value

        if not self.is_configured():
            log.info("NVIDIA API key not configured; skipping AI call")
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error="NVIDIA_API_KEY environment variable is not set.",
                model=self.model,
            )

        user_prompt = build_user_prompt(rules_result, weather)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }

        try:
            response_data = self._post_json(payload)
        except urllib.error.HTTPError as exc:
            log.warning("NVIDIA NIM HTTP error %s: %s", exc.code, exc.reason)
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error=f"HTTP {exc.code}: {exc.reason}",
                error_code=exc.code,
                model=self.model,
            )
        except TimeoutError as exc:
            log.warning("NVIDIA NIM request timed out: %s", exc)
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error=f"Request timed out after {self.timeout_s}s.",
                model=self.model,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("NVIDIA NIM response parse error: %s", exc)
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error=f"Invalid JSON response: {exc}",
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("NVIDIA NIM unexpected error: %s", exc)
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error=str(exc),
                model=self.model,
            )

        return self._parse_response(response_data, mode_str)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON payload to the NIM chat completions endpoint.

        Uses urllib.request (stdlib only). Raises on HTTP error or timeout.
        """
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.chat_endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "ARIA-Agricultural-Rover/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
            status_code = response.getcode()
            raw = response.read().decode("utf-8")

        if status_code != 200:
            raise urllib.error.HTTPError(
                self.chat_endpoint, status_code,
                f"Unexpected status {status_code}", {}, None,
            )

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object, got {type(data).__name__}")
        return data

    def _parse_response(
        self,
        data: dict[str, Any],
        mode_str: str,
    ) -> NVIDIAResponse:
        """Extract content and usage from an OpenAI-compatible chat completion."""
        choices = data.get("choices") or []
        if not choices or not isinstance(choices, list):
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error="Response contained no choices.",
                model=data.get("model", self.model),
            )

        choice = choices[0]
        message = choice.get("message") or {}
        raw_text: str = message.get("content") or ""
        # Support reasoning models (e.g. nvidia/nemotron-3.5-lightning-30b-a3b) that may emit reasoning_content
        if not raw_text.strip() and message.get("reasoning_content"):
            raw_text = str(message.get("reasoning_content", "")).strip()

        finish_reason: str | None = choice.get("finish_reason")

        usage = data.get("usage") or {}
        prompt_tokens: int | None = usage.get("prompt_tokens")
        completion_tokens: int | None = usage.get("completion_tokens")
        model_used: str | None = data.get("model", self.model)

        if not raw_text.strip():
            return NVIDIAResponse.unavailable(
                advice_mode=mode_str,
                error="NVIDIA returned an empty response content.",
                model=model_used,
            )

        # Build a cleaned summary: strip leading/trailing whitespace, collapse
        # any internal blank lines to single breaks, limit total length.
        summary = raw_text.strip()

        log.info(
            "NVIDIA NIM advice generated: mode=%s model=%s tokens=%s/%s finish=%s",
            mode_str, model_used, prompt_tokens, completion_tokens, finish_reason,
        )

        return NVIDIAResponse(
            status=NVIDIAStatus.AVAILABLE,
            model=model_used,
            advice_mode=mode_str,
            summary=summary,
            raw_text=raw_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
            error=None,
            error_code=None,
        )
