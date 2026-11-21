"""Phase 5 verification tests for ARIA Web Dashboard."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from server.app import create_app
from server.database import Database


class TestPhase5Dashboard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_dashboard.db"
        self.db = Database(self.db_path)
        self.db.initialize()
        # Disable MQTT for dashboard tests: no broker required, no worker threads.
        self.app = create_app(db=self.db, start_mqtt=False)
        self.client = self.app.test_client()

    def tearDown(self):
        # Stop MQTT client (no-op when start_mqtt=False, safe to call).
        self.app.stop_mqtt()
        self.db.close()
        time.sleep(0.1)
        self.temp_dir.cleanup()

    def test_dashboard_index_route(self):
        """Verify GET / renders dashboard HTML with status 200."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)
        html = resp.get_data(as_text=True)

        # Title and header
        self.assertIn("ARIA", html)
        self.assertIn("Agricultural Rover Intelligence", html)

        # External libraries
        self.assertIn("leaflet.js", html)
        self.assertIn("chart.umd.min.js", html)

        # Key UI components
        self.assertIn('id="gpsMap"', html)
        self.assertIn('id="provenanceBadge"', html)
        self.assertIn('id="staleAlert"', html)
        self.assertIn('id="nodeBroker"', html)
        self.assertIn('id="nodeBackend"', html)
        self.assertIn('id="nodeDevice"', html)

        # Motor control UI components
        self.assertIn('id="btnForward"', html)
        self.assertIn('id="btnReverse"', html)
        self.assertIn('id="btnLeft"', html)
        self.assertIn('id="btnRight"', html)
        self.assertIn('id="btnCenterStop"', html)
        self.assertIn('id="btnEmergencyStop"', html)
        self.assertIn('id="currentCommandReadout"', html)
        self.assertIn('id="controlLockBanner"', html)

    def test_dashboard_static_assets_served(self):
        """Verify CSS and JavaScript static assets are accessible."""
        css_resp = self.client.get("/static/css/dashboard.css")
        self.assertEqual(css_resp.status_code, 200)
        self.assertIn("text/css", css_resp.content_type)
        self.assertIn("--bg-primary", css_resp.get_data(as_text=True))
        css_resp.close()

        js_resp = self.client.get("/static/js/dashboard.js")
        self.assertEqual(js_resp.status_code, 200)
        self.assertTrue(
            "javascript" in js_resp.content_type or "text/plain" in js_resp.content_type,
            f"Unexpected content-type: {js_resp.content_type}",
        )
        self.assertIn("POLL_INTERVAL_MS", js_resp.get_data(as_text=True))
        js_resp.close()

    def test_health_check_includes_broker_status(self):
        """Verify /api/v1/health reports MQTT broker connectivity status."""
        resp = self.client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("broker", data)
        # Because local Mosquitto is running on port 1883
        self.assertEqual(data["broker"], "connected")

    def test_disclaimers_and_guardrails_in_html(self):
        """Verify non-negotiable scope disclaimers are present in the UI markup."""
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)

        # MQ-135 must be labelled raw ADC, uncalibrated, not ppm or CO2
        self.assertIn("MQ-135", html)
        self.assertIn("Raw ADC", html)
        self.assertIn("Not calibrated ppm or CO", html)

        # HC-SR04 has been removed from the hardware and markup completely
        self.assertNotIn("HC-SR04", html)
        self.assertNotIn("chartDistance", html)

        # GPS fix unavailable overlay present
        self.assertIn("GPS Fix Not Available", html)

    def test_advice_dashboard_components_rendered(self):
        """Verify the Agronomic Advice & Intelligence UI components are present in the markup."""
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)

        # Advice section and controls
        self.assertIn('id="adviceSection"', html)
        self.assertIn('id="adviceModeBadge"', html)
        self.assertIn('id="adviceProviderBadge"', html)
        self.assertIn('id="btnGenerateAdvice"', html)
        self.assertIn('id="adviceCooldownIndicator"', html)
        self.assertIn('id="adviceAlert"', html)

        # Advice content containers
        self.assertIn('id="adviceEmptyState"', html)
        self.assertIn('id="adviceContent"', html)
        self.assertIn('id="adviceSummaryBox"', html)
        self.assertIn('id="adviceRecommendationsList"', html)
        self.assertIn('id="sensorHealthList"', html)
        self.assertIn('id="weatherContextBody"', html)
        self.assertIn('id="adviceObservationsList"', html)
        self.assertIn('id="adviceLimitationsList"', html)

        # Freshness and GPS provenance elements
        self.assertIn('id="adviceStaleTelemetryNotice"', html)
        self.assertIn('id="adviceTelemetryTime"', html)
        self.assertIn('id="adviceTelemetryFreshnessBadge"', html)
        self.assertIn('id="adviceGpsProvenanceBadge"', html)

    def test_dashboard_js_freshness_and_provenance_logic(self):
        """Verify dashboard.js implements timestamp parsing, freshness, and GPS provenance handling."""
        resp = self.client.get("/static/js/dashboard.js")
        self.assertEqual(resp.status_code, 200)
        js_code = resp.get_data(as_text=True)
        resp.close()

        self.assertIn("parseTimestamp", js_code)
        self.assertIn("checkAdviceTelemetryStaleness", js_code)
        self.assertIn("adviceStaleTelemetryNotice", js_code)
        self.assertIn("CURRENT GPS FIX", js_code)
        self.assertIn("LAST KNOWN GPS", js_code)
        self.assertIn("NO GPS FIX", js_code)
        self.assertIn("CURRENT TELEMETRY", js_code)
        self.assertIn("STALE TELEMETRY", js_code)
        self.assertIn("location_age_seconds", js_code)


if __name__ == "__main__":
    unittest.main()
