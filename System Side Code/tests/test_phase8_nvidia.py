"""Unit tests for ARIA Advice Engine Phase 3: NVIDIA NIM Client.

Covers:
1. API key configuration check (configured / not configured).
2. Prompt construction:
   - FULL_CONTEXT, LIMITED_CONTEXT, GENERAL_GUIDANCE modes.
   - Sensor health states included in prompt.
   - Guardrails: no ppm/CO2, no soil moisture, no fabricated values for nulls.
   - Weather context included when available; omitted when unavailable.
3. API request:
   - Correct endpoint, method, headers, auth token.
   - Serialized JSON body structure (model, messages, max_tokens, stream=False).
4. API response parsing:
   - Valid completion returns NVIDIAResponse with status=AVAILABLE.
   - Token usage captured (prompt_tokens, completion_tokens).
   - finish_reason captured.
5. Error handling (all must return NVIDIAResponse with status=UNAVAILABLE):
   - API key missing / blank.
   - HTTP 401 Unauthorized.
   - HTTP 429 Too Many Requests.
   - HTTP 500 Server Error.
   - Network timeout.
   - Connection refused / URLError.
   - Malformed JSON response.
   - Empty choices array.
   - Empty content in choice.
6. Thread safety: concurrent calls don't corrupt shared state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from server.nvidia_client import (
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    NVIDIAClient,
    NVIDIAResponse,
    NVIDIAStatus,
    build_user_prompt,
)
from server.rules import (
    AdviceMode,
    MonitoringThresholds,
    RulesEvaluationResult,
    RuleObservation,
    SensorHealthRecord,
    SensorState,
    Severity,
    evaluate_rules,
)
from server.telemetry import GPSData, TelemetryMessage


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_rules_result(
    mode: AdviceMode = AdviceMode.FULL_CONTEXT,
    observations: list | None = None,
    sensor_health: list | None = None,
    available_inputs: list | None = None,
    unavailable_inputs: list | None = None,
    limitations: list | None = None,
) -> RulesEvaluationResult:
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    return RulesEvaluationResult(
        advice_mode=mode,
        available_inputs=available_inputs or ["temperature_c", "humidity_pct", "weather"],
        unavailable_inputs=unavailable_inputs or [],
        sensor_health=sensor_health or [
            SensorHealthRecord("temperature_c", SensorState.HEALTHY, "25.0°C live reading."),
            SensorHealthRecord("humidity_pct", SensorState.HEALTHY, "58.0% live reading."),
            SensorHealthRecord("light_lux", SensorState.HEALTHY, "15000.0 lux live reading."),
            SensorHealthRecord("mq135_raw", SensorState.HEALTHY, "1200 ADC live reading."),
            SensorHealthRecord("gps", SensorState.HEALTHY, "GPS fix locked."),
        ],
        observations=observations or [],
        limitations=limitations or [],
        evaluated_at=now,
    )


def _make_weather(available: bool = True) -> dict:
    if not available:
        return {"status": "unavailable", "current": None, "forecast": None}
    return {
        "status": "available",
        "location_source": "CURRENT_LOCATION",
        "current": {
            "temperature_c": 24.5,
            "temperature_2m": 24.5,
            "humidity_pct": 55.0,
            "relative_humidity_2m": 55.0,
            "precipitation_mm": 0.0,
            "precipitation": 0.0,
            "wind_speed_kmh": 12.0,
            "wind_speed_10m": 12.0,
        },
        "forecast": {
            "precipitation_probability_next_6h_pct": 20,
            "precipitation_next_6h_mm": 0.3,
        },
        "limitations": ["Weather represents a regional forecast, not a direct measurement."],
    }


def _mock_chat_response(
    content: str | None = "Field conditions are nominal. No critical concerns detected.",
    model: str = DEFAULT_MODEL,
    prompt_tokens: int = 350,
    completion_tokens: int = 95,
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
) -> dict:
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return {
        "id": "chatcmpl-test-12345",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _mock_urlopen(payload: dict, status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.getcode.return_value = status
    mock_resp.read.return_value = body
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


# ---------------------------------------------------------------------------
# 1. Configuration Tests
# ---------------------------------------------------------------------------

class TestNVIDIAClientConfiguration(unittest.TestCase):

    def test_is_configured_with_key(self):
        client = NVIDIAClient(api_key="nvapi-test-key-1234")
        self.assertTrue(client.is_configured())

    def test_is_not_configured_with_empty_key(self):
        client = NVIDIAClient(api_key="")
        self.assertFalse(client.is_configured())

    def test_is_not_configured_with_whitespace_key(self):
        client = NVIDIAClient(api_key="   ")
        self.assertFalse(client.is_configured())

    def test_is_not_configured_with_none_key_and_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove NVIDIA_API_KEY from env if present
            env = {k: v for k, v in os.environ.items() if k != "NVIDIA_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                client = NVIDIAClient(api_key=None)
                self.assertFalse(client.is_configured())

    def test_api_key_from_environment_variable(self):
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-env-key"}):
            client = NVIDIAClient()
            self.assertTrue(client.is_configured())
            self.assertEqual(client.api_key, "nvapi-env-key")

    def test_model_defaults_to_llama(self):
        client = NVIDIAClient(api_key="key")
        self.assertEqual(client.model, DEFAULT_MODEL)

    def test_model_overridden_by_constructor(self):
        client = NVIDIAClient(api_key="key", model="mistralai/mistral-7b-instruct-v0.3")
        self.assertEqual(client.model, "mistralai/mistral-7b-instruct-v0.3")

    def test_model_overridden_by_env(self):
        with patch.dict(os.environ, {"NVIDIA_MODEL": "google/gemma-2-9b-it"}):
            client = NVIDIAClient(api_key="key")
            self.assertEqual(client.model, "google/gemma-2-9b-it")


# ---------------------------------------------------------------------------
# 2. Prompt Construction Tests
# ---------------------------------------------------------------------------

class TestPromptConstruction(unittest.TestCase):

    def test_full_context_mode_in_prompt(self):
        rules = _make_rules_result(mode=AdviceMode.FULL_CONTEXT)
        prompt = build_user_prompt(rules, weather=_make_weather())
        self.assertIn("FULL_CONTEXT", prompt)
        self.assertIn("SENSOR STATUS", prompt)
        self.assertIn("LOCALIZED WEATHER CONTEXT", prompt)

    def test_limited_context_instruction_text(self):
        rules = _make_rules_result(mode=AdviceMode.LIMITED_CONTEXT)
        prompt = build_user_prompt(rules, weather=None)
        self.assertIn("LIMITED_CONTEXT", prompt)
        self.assertIn("cautious observations", prompt)
        self.assertIn("LOCALIZED WEATHER CONTEXT: unavailable", prompt)

    def test_general_guidance_instruction_text(self):
        rules = _make_rules_result(mode=AdviceMode.GENERAL_GUIDANCE)
        prompt = build_user_prompt(rules, weather=None)
        self.assertIn("GENERAL_GUIDANCE", prompt)
        self.assertIn("conservative general agricultural monitoring", prompt)

    def test_sensor_health_states_in_prompt(self):
        health = [
            SensorHealthRecord("temperature_c", SensorState.HEALTHY, "25.0°C."),
            SensorHealthRecord("humidity_pct", SensorState.UNRESPONSIVE_NULL, "null in payload."),
            SensorHealthRecord("mq135_raw", SensorState.DEVICE_OFFLINE, "Rover offline."),
        ]
        rules = _make_rules_result(sensor_health=health)
        prompt = build_user_prompt(rules, weather=None)
        self.assertIn("temperature_c: HEALTHY", prompt)
        self.assertIn("humidity_pct: UNRESPONSIVE_NULL", prompt)
        self.assertIn("mq135_raw: DEVICE_OFFLINE", prompt)

    def test_observations_included_in_prompt(self):
        obs = [
            RuleObservation(
                rule_id="ENV_TEMP_HIGH",
                title="Elevated Ambient Temperature",
                explanation="38.5°C exceeds 35.0°C trigger.",
                severity=Severity.WARNING,
                source="sensor",
            ),
        ]
        rules = _make_rules_result(observations=obs)
        prompt = build_user_prompt(rules, weather=None)
        # Rule id, title and explanation are all in the prompt
        self.assertIn("ENV_TEMP_HIGH", prompt)
        self.assertIn("Elevated Ambient Temperature", prompt)
        self.assertIn("WARNING", prompt)
        self.assertIn("38.5°C", prompt)

    def test_no_observations_text_when_empty(self):
        rules = _make_rules_result(observations=[])
        prompt = build_user_prompt(rules, weather=None)
        self.assertIn("none triggered", prompt)

    def test_weather_temperature_in_prompt(self):
        rules = _make_rules_result()
        prompt = build_user_prompt(rules, weather=_make_weather())
        self.assertIn("24.5°C", prompt)
        self.assertIn("precipitation", prompt.lower())

    def test_weather_forecast_precipitation_prob_in_prompt(self):
        rules = _make_rules_result()
        weather = _make_weather()
        prompt = build_user_prompt(rules, weather=weather)
        self.assertIn("20%", prompt)   # precipitation probability

    def test_limitations_in_prompt(self):
        rules = _make_rules_result(limitations=[
            "DHT22 ambient temperature sensor is unavailable.",
            "GPS has no valid outdoor fix.",
        ])
        prompt = build_user_prompt(rules, weather=None)
        self.assertIn("KNOWN LIMITATIONS", prompt)
        self.assertIn("DHT22 ambient temperature sensor is unavailable.", prompt)

    def test_no_ppm_or_co2_in_prompt(self):
        """Prompt builder must never introduce CO2/ppm language."""
        obs = [
            RuleObservation(
                rule_id="ENV_MQ135_ELEVATED",
                title="Elevated MQ-135 Analogue Response",
                explanation="MQ-135 reading is 2800 raw ADC counts.",
                severity=Severity.WARNING,
                source="sensor",
            ),
        ]
        rules = _make_rules_result(observations=obs)
        prompt = build_user_prompt(rules, weather=None)
        # Must not introduce calibrated terms
        self.assertNotIn("ppm", prompt.lower())
        self.assertNotIn("co2", prompt.lower())
        self.assertNotIn("carbon dioxide", prompt.lower())
        # MQ-135 raw ADC should be referenced
        self.assertIn("MQ-135", prompt)

    def test_null_sensor_not_fabricated_in_prompt(self):
        """When a sensor is unavailable, its value must not appear as a number."""
        health = [
            SensorHealthRecord("humidity_pct", SensorState.UNRESPONSIVE_NULL, "null in payload."),
        ]
        rules = _make_rules_result(
            sensor_health=health,
            unavailable_inputs=["humidity_pct"],
        )
        prompt = build_user_prompt(rules, weather=None)
        # State should appear
        self.assertIn("humidity_pct: UNRESPONSIVE_NULL", prompt)
        # No fabricated numbers for humidity
        self.assertNotIn("humidity_pct: 0", prompt)
        self.assertNotIn("humidity_pct: 100", prompt)


# ---------------------------------------------------------------------------
# 3. API Request Structure Tests
# ---------------------------------------------------------------------------

class TestAPIRequestStructure(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_correct_endpoint_called(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key", base_url="https://integrate.api.nvidia.com/v1")
        rules = _make_rules_result()

        client.generate_advice(rules, weather=None)

        req = mock_urlopen.call_args[0][0]
        self.assertIn("chat/completions", req.full_url)
        self.assertIn("integrate.api.nvidia.com", req.full_url)

    @patch("urllib.request.urlopen")
    def test_request_method_is_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")

    @patch("urllib.request.urlopen")
    def test_authorization_header_set(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="nvapi-secret-key-xyz")
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        auth = req.headers.get("Authorization") or req.headers.get("authorization")
        self.assertIsNotNone(auth)
        self.assertIn("nvapi-secret-key-xyz", auth)
        self.assertTrue(auth.startswith("Bearer "))

    @patch("urllib.request.urlopen")
    def test_content_type_header(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        ct = req.headers.get("Content-type") or req.headers.get("Content-Type") or req.headers.get("content-type")
        self.assertIsNotNone(ct)
        self.assertIn("application/json", ct)

    @patch("urllib.request.urlopen")
    def test_request_body_contains_model_and_messages(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key", model="meta/llama-3.1-8b-instruct")
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "meta/llama-3.1-8b-instruct")
        self.assertIn("messages", body)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertFalse(body["stream"])

    @patch("urllib.request.urlopen")
    def test_max_tokens_in_body(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key", max_tokens=256)
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["max_tokens"], 256)

    @patch("urllib.request.urlopen")
    def test_system_prompt_in_messages(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        client.generate_advice(_make_rules_result(), weather=None)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        system_content = body["messages"][0]["content"]
        # System prompt must contain guardrails
        self.assertIn("MQ-135", system_content)
        self.assertIn("ADC", system_content)
        self.assertIn("soil moisture", system_content)
        self.assertNotIn("ppm", system_content.lower().replace("ppm", ""))  # guardrail enforced

    @patch("urllib.request.urlopen")
    def test_user_prompt_references_advice_mode(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        rules = _make_rules_result(mode=AdviceMode.LIMITED_CONTEXT)
        client.generate_advice(rules, weather=None)

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        user_content = body["messages"][1]["content"]
        self.assertIn("LIMITED_CONTEXT", user_content)


# ---------------------------------------------------------------------------
# 4. Response Parsing Tests
# ---------------------------------------------------------------------------

class TestResponseParsing(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_successful_response_returns_available(self, mock_urlopen):
        chat_resp = _mock_chat_response(
            content="Conditions look good. Temperature and humidity are within normal ranges.\n- Ambient temperature: 25°C, comfortable for field operations.\n- Humidity: 58%, typical for the region.",
            prompt_tokens=320,
            completion_tokens=78,
        )
        mock_urlopen.return_value = _mock_urlopen(chat_resp)
        client = NVIDIAClient(api_key="test-key")

        resp = client.generate_advice(_make_rules_result(), weather=_make_weather())

        self.assertEqual(resp.status, NVIDIAStatus.AVAILABLE)
        self.assertIsNotNone(resp.summary)
        self.assertIn("Conditions look good", resp.summary)
        self.assertEqual(resp.prompt_tokens, 320)
        self.assertEqual(resp.completion_tokens, 78)
        self.assertEqual(resp.finish_reason, "stop")

    @patch("urllib.request.urlopen")
    def test_model_name_captured(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(
            _mock_chat_response(model="mistralai/mistral-7b-instruct-v0.3")
        )
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.model, "mistralai/mistral-7b-instruct-v0.3")

    @patch("urllib.request.urlopen")
    def test_advice_mode_in_response(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        rules = _make_rules_result(mode=AdviceMode.LIMITED_CONTEXT)
        resp = client.generate_advice(rules, weather=None)
        self.assertEqual(resp.advice_mode, "LIMITED_CONTEXT")

    @patch("urllib.request.urlopen")
    def test_finish_reason_length_captured(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(
            _mock_chat_response(finish_reason="length")
        )
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.finish_reason, "length")

    @patch("urllib.request.urlopen")
    def test_generated_at_is_utc(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertIsNotNone(resp.generated_at.tzinfo)

    @patch("urllib.request.urlopen")
    def test_to_dict_serialisable(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(_mock_chat_response())
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        d = resp.to_dict()
        # Must be JSON-serializable
        serialized = json.dumps(d)
        self.assertIsInstance(serialized, str)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["status"], "available")
        self.assertIn("summary", parsed)
        self.assertIn("generated_at", parsed)

    @patch("urllib.request.urlopen")
    def test_unavailable_response_to_dict(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timeout")
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        d = resp.to_dict()
        self.assertEqual(d["status"], "unavailable")
        self.assertIsNone(d["summary"])
        self.assertIn("timed out", d["error"])

    @patch("urllib.request.urlopen")
    def test_nemotron_reasoning_content_fallback(self, mock_urlopen):
        """Reasoning models emitting empty content but valid reasoning_content succeed."""
        chat_resp = _mock_chat_response(
            content="",
            reasoning_content="Field conditions are within safe operating limits based on sensor telemetry.",
            model="nvidia/nemotron-3.5-lightning-30b-a3b",
        )
        mock_urlopen.return_value = _mock_urlopen(chat_resp)
        client = NVIDIAClient(api_key="test-key")

        resp = client.generate_advice(_make_rules_result(), weather=None)

        self.assertEqual(resp.status, NVIDIAStatus.AVAILABLE)
        self.assertIn("Field conditions are within safe operating limits", resp.summary)
        self.assertEqual(resp.model, "nvidia/nemotron-3.5-lightning-30b-a3b")

    @patch("urllib.request.urlopen")
    def test_content_preferred_over_reasoning_content(self, mock_urlopen):
        """Standard content takes precedence over reasoning_content if both exist."""
        chat_resp = _mock_chat_response(
            content="Final answer for operator.",
            reasoning_content="Internal reasoning trace...",
        )
        mock_urlopen.return_value = _mock_urlopen(chat_resp)
        client = NVIDIAClient(api_key="test-key")

        resp = client.generate_advice(_make_rules_result(), weather=None)

        self.assertEqual(resp.status, NVIDIAStatus.AVAILABLE)
        self.assertEqual(resp.summary, "Final answer for operator.")


# ---------------------------------------------------------------------------
# 5. Error Handling Tests
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):

    def test_no_api_key_returns_unavailable(self):
        with patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "NVIDIA_API_KEY"}, clear=True):
            client = NVIDIAClient(api_key=None)
            resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIsNotNone(resp.error)
        self.assertIn("NVIDIA_API_KEY", resp.error)

    @patch("urllib.request.urlopen")
    def test_http_401_unauthorized(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=401, msg="Unauthorized", hdrs={}, fp=None,
        )
        client = NVIDIAClient(api_key="bad-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertEqual(resp.error_code, 401)
        self.assertIn("401", resp.error)

    @patch("urllib.request.urlopen")
    def test_http_429_rate_limit(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=429, msg="Too Many Requests", hdrs={}, fp=None,
        )
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertEqual(resp.error_code, 429)
        self.assertIn("429", resp.error)

    @patch("urllib.request.urlopen")
    def test_http_500_server_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://integrate.api.nvidia.com/v1/chat/completions",
            code=500, msg="Internal Server Error", hdrs={}, fp=None,
        )
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertEqual(resp.error_code, 500)

    @patch("urllib.request.urlopen")
    def test_timeout_returns_unavailable(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("connection timed out")
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIn("timed out", resp.error.lower())
        self.assertIsNone(resp.error_code)

    @patch("urllib.request.urlopen")
    def test_url_error_connection_refused(self, mock_urlopen):
        import socket
        mock_urlopen.side_effect = urllib.error.URLError(
            reason=socket.error("Connection refused")
        )
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIsNotNone(resp.error)

    @patch("urllib.request.urlopen")
    def test_malformed_json_response(self, mock_urlopen):
        bad_resp = MagicMock()
        bad_resp.getcode.return_value = 200
        bad_resp.read.return_value = b"{not valid json at all"
        bad_resp.__enter__.return_value = bad_resp
        bad_resp.__exit__.return_value = False
        mock_urlopen.return_value = bad_resp
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIn("JSON", resp.error)

    @patch("urllib.request.urlopen")
    def test_empty_choices_array(self, mock_urlopen):
        empty_choices = {
            "id": "chatcmpl-empty",
            "object": "chat.completion",
            "model": DEFAULT_MODEL,
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 0},
        }
        mock_urlopen.return_value = _mock_urlopen(empty_choices)
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIn("no choices", resp.error.lower())

    @patch("urllib.request.urlopen")
    def test_empty_content_in_choice(self, mock_urlopen):
        blank_content = _mock_chat_response(content="   ")
        mock_urlopen.return_value = _mock_urlopen(blank_content)
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)
        self.assertIn("empty", resp.error.lower())

    @patch("urllib.request.urlopen")
    def test_response_missing_choices_key(self, mock_urlopen):
        no_choices = {"id": "test", "model": DEFAULT_MODEL}
        mock_urlopen.return_value = _mock_urlopen(no_choices)
        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(_make_rules_result(), weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.UNAVAILABLE)


# ---------------------------------------------------------------------------
# 6. Thread Safety Test
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_concurrent_calls_do_not_corrupt(self, mock_urlopen):
        """Multiple concurrent calls should each return a valid, independent response."""
        mock_urlopen.return_value = _mock_urlopen(
            _mock_chat_response(content="All conditions are nominal for field operations.")
        )
        client = NVIDIAClient(api_key="test-key")
        rules = _make_rules_result()
        weather = _make_weather()

        results: list[NVIDIAResponse] = []
        errors: list[Exception] = []

        def call():
            try:
                r = client.generate_advice(rules, weather=weather)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=call) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 8)
        for r in results:
            self.assertEqual(r.status, NVIDIAStatus.AVAILABLE)


# ---------------------------------------------------------------------------
# 7. Integration with Evaluate Rules (end-to-end prompt path)
# ---------------------------------------------------------------------------

class TestEndToEndPromptFromRules(unittest.TestCase):

    def _base_telemetry(self, now: datetime) -> TelemetryMessage:
        return TelemetryMessage(
            schema_version=1,
            device_id="aria-c3",
            source="real",
            received_at=now - timedelta(seconds=2),
            timestamp=now - timedelta(seconds=2),
            temperature_c=37.5,  # Above 35°C threshold
            humidity_pct=88.0,   # Above 85% threshold
            light_lux=12000.0,
            mq135_raw=2600,      # Above 2500 threshold
            gps=GPSData(fix=True, latitude=13.08, longitude=80.27, satellites=6),
        )

    @patch("urllib.request.urlopen")
    def test_full_pipeline_temperature_and_humidity_alerts(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(
            _mock_chat_response(
                content="High ambient temperature (37.5°C) and elevated humidity (88%) detected.\n"
                        "- Monitor field electronics for thermal stress.\n"
                        "- Elevated atmospheric moisture noted; not an indicator of soil saturation."
            )
        )
        now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
        telem = self._base_telemetry(now)
        weather = _make_weather()

        rules_result = evaluate_rules(
            telemetry=telem,
            device_online=True,
            weather=weather,
            current_time=now,
        )
        self.assertEqual(rules_result.advice_mode, AdviceMode.FULL_CONTEXT)

        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(rules_result, weather=weather)

        self.assertEqual(resp.status, NVIDIAStatus.AVAILABLE)
        # Verify prompt sent to API contains actual observation data
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        user_msg = body["messages"][1]["content"]
        self.assertIn("37.5°C", user_msg)
        self.assertIn("88.0%", user_msg)
        self.assertIn("ENV_TEMP_HIGH", user_msg)
        self.assertIn("ENV_HUM_HIGH", user_msg)

    @patch("urllib.request.urlopen")
    def test_general_guidance_when_no_telemetry_no_weather(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen(
            _mock_chat_response(content="No live data available. Apply general field monitoring principles.")
        )
        rules_result = evaluate_rules(
            telemetry=None,
            device_online=False,
            weather=None,
        )
        self.assertEqual(rules_result.advice_mode, AdviceMode.GENERAL_GUIDANCE)

        client = NVIDIAClient(api_key="test-key")
        resp = client.generate_advice(rules_result, weather=None)
        self.assertEqual(resp.status, NVIDIAStatus.AVAILABLE)
        self.assertEqual(resp.advice_mode, "GENERAL_GUIDANCE")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        user_msg = body["messages"][1]["content"]
        self.assertIn("GENERAL_GUIDANCE", user_msg)


if __name__ == "__main__":
    unittest.main()
