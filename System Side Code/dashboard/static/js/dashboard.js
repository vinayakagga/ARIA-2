/**
 * ARIA Agricultural Field Monitoring Station & Rover Control (Phase 7)
 * Vanilla JavaScript client logic. Fetches data exclusively from Flask REST API.
 */

(function () {
  "use strict";

  // Configuration constants
  const POLL_INTERVAL_MS = 2000;
  const STREAM_INTERVAL_MS = 200;
  const STALE_THRESHOLD_SECONDS = 15;

  // Chart instances (environmental only)
  let chartTempHum = null;
  let chartLightGas = null;

  // Leaflet map instance
  let map = null;
  let roverMarker = null;

  // Rover motor control state
  let isRoverOnline = false;
  let activeCommand = null;
  let streamTimer = null;
  let activePointerId = null;
  let activePointerTarget = null;

  // DOM Elements cache
  const el = {
    provenanceBadge: document.getElementById("provenanceBadge"),
    provenanceText: document.getElementById("provenanceText"),
    lastUpdated: document.getElementById("lastUpdated"),
    staleAlert: document.getElementById("staleAlert"),
    staleMessage: document.getElementById("staleMessage"),

    // Connectivity
    brokerStatusPill: document.getElementById("brokerStatusPill"),
    backendStatusPill: document.getElementById("backendStatusPill"),
    dbStatus: document.getElementById("dbStatus"),
    deviceStatusPill: document.getElementById("deviceStatusPill"),
    deviceId: document.getElementById("deviceId"),
    deviceRssi: document.getElementById("deviceRssi"),
    deviceUptime: document.getElementById("deviceUptime"),

    // Environmental Metrics
    valTemp: document.getElementById("valTemp"),
    stateTemp: document.getElementById("stateTemp"),
    valHumidity: document.getElementById("valHumidity"),
    stateHumidity: document.getElementById("stateHumidity"),
    valLight: document.getElementById("valLight"),
    stateLight: document.getElementById("stateLight"),
    valGas: document.getElementById("valGas"),

    // Motor Controls
    currentCommandReadout: document.getElementById("currentCommandReadout"),
    controlLockBanner: document.getElementById("controlLockBanner"),
    commandAlert: document.getElementById("commandAlert"),
    commandAlertText: document.getElementById("commandAlertText"),
    btnForward: document.getElementById("btnForward"),
    btnReverse: document.getElementById("btnReverse"),
    btnLeft: document.getElementById("btnLeft"),
    btnRight: document.getElementById("btnRight"),
    btnCenterStop: document.getElementById("btnCenterStop"),
    btnEmergencyStop: document.getElementById("btnEmergencyStop"),

    // GPS
    gpsFixBadge: document.getElementById("gpsFixBadge"),
    gpsLat: document.getElementById("gpsLat"),
    gpsLon: document.getElementById("gpsLon"),
    gpsSats: document.getElementById("gpsSats"),
    gpsAlt: document.getElementById("gpsAlt"),
    gpsSpd: document.getElementById("gpsSpd"),
    noFixOverlay: document.getElementById("noFixOverlay"),

    // Field Advice & Intelligence
    adviceModeBadge: document.getElementById("adviceModeBadge"),
    adviceProviderBadge: document.getElementById("adviceProviderBadge"),
    btnGenerateAdvice: document.getElementById("btnGenerateAdvice"),
    btnGenerateAdviceText: document.getElementById("btnGenerateAdviceText"),
    adviceCooldownIndicator: document.getElementById("adviceCooldownIndicator"),
    adviceCooldownTimer: document.getElementById("adviceCooldownTimer"),
    adviceAlert: document.getElementById("adviceAlert"),
    adviceAlertText: document.getElementById("adviceAlertText"),
    adviceEmptyState: document.getElementById("adviceEmptyState"),
    adviceContent: document.getElementById("adviceContent"),
    adviceTimestamp: document.getElementById("adviceTimestamp"),
    adviceSummaryText: document.getElementById("adviceSummaryText"),
    adviceRecommendationsList: document.getElementById("adviceRecommendationsList"),
    sensorHealthList: document.getElementById("sensorHealthList"),
    weatherContextBody: document.getElementById("weatherContextBody"),
    adviceObservationsList: document.getElementById("adviceObservationsList"),
    adviceLimitationsList: document.getElementById("adviceLimitationsList"),
    adviceStaleTelemetryNotice: document.getElementById("adviceStaleTelemetryNotice"),
    adviceStaleTelemetryMessage: document.getElementById("adviceStaleTelemetryMessage"),
    adviceTelemetryTime: document.getElementById("adviceTelemetryTime"),
    adviceTelemetryFreshnessBadge: document.getElementById("adviceTelemetryFreshnessBadge"),
    adviceGpsProvenanceBadge: document.getElementById("adviceGpsProvenanceBadge"),
  };

  /**
   * Initialize Leaflet Map.
   */
  function initMap() {
    if (typeof L === "undefined") {
      console.warn("Leaflet library not loaded");
      return;
    }
    map = L.map("gpsMap", {
      zoomControl: true,
      attributionControl: true,
    }).setView([20.0, 0.0], 2);

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
  }

  /**
   * Initialize Environmental Chart.js instances.
   */
  function initCharts() {
    if (typeof Chart === "undefined") {
      console.warn("Chart.js library not loaded");
      return;
    }

    const commonOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      plugins: {
        legend: { labels: { color: "#b0aba0", font: { size: 11 } } },
      },
      scales: {
        x: {
          ticks: { color: "#7d857f", maxTicksLimit: 6, font: { size: 10 } },
          grid: { color: "rgba(49, 58, 52, 0.4)" },
        },
      },
    };

    // Chart 1: Temperature & Humidity
    const ctx1 = document.getElementById("chartTempHum").getContext("2d");
    chartTempHum = new Chart(ctx1, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Temperature (°C)",
            data: [],
            borderColor: "#c88214",
            backgroundColor: "rgba(200, 130, 20, 0.12)",
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y",
            fill: true,
          },
          {
            label: "Humidity (%)",
            data: [],
            borderColor: "#5b8db8",
            backgroundColor: "rgba(91, 141, 184, 0.12)",
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y1",
            fill: true,
          },
        ],
      },
      options: {
        ...commonOptions,
        scales: {
          ...commonOptions.scales,
          y: {
            type: "linear",
            position: "left",
            ticks: { color: "#c88214", font: { size: 10 } },
            grid: { color: "rgba(49, 58, 52, 0.4)" },
            title: { display: true, text: "°C", color: "#c88214", font: { size: 10 } },
          },
          y1: {
            type: "linear",
            position: "right",
            ticks: { color: "#5b8db8", font: { size: 10 } },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "%", color: "#5b8db8", font: { size: 10 } },
          },
        },
      },
    });

    // Chart 2: Light & Gas Raw ADC
    const ctx2 = document.getElementById("chartLightGas").getContext("2d");
    chartLightGas = new Chart(ctx2, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Light (lux)",
            data: [],
            borderColor: "#dfb15b",
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y",
          },
          {
            label: "MQ-135 (Raw ADC)",
            data: [],
            borderColor: "#83a55c",
            borderWidth: 2,
            tension: 0.25,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        ...commonOptions,
        scales: {
          ...commonOptions.scales,
          y: {
            type: "linear",
            position: "left",
            ticks: { color: "#dfb15b", font: { size: 10 } },
            grid: { color: "rgba(49, 58, 52, 0.4)" },
            title: { display: true, text: "lux", color: "#dfb15b", font: { size: 10 } },
          },
          y1: {
            type: "linear",
            position: "right",
            ticks: { color: "#83a55c", font: { size: 10 } },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "ADC raw", color: "#83a55c", font: { size: 10 } },
          },
        },
      },
    });
  }

  // =========================================================================
  // Manual Motor Control Implementation
  // =========================================================================

  /**
   * Send a discrete command to POST /api/v1/command.
   */
  async function sendCommand(command) {
    try {
      const res = await fetch("/api/v1/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      });
      const data = await res.json();
      if (!res.ok || data.status !== "success") {
        const errMsg = data.message || `HTTP ${res.status}`;
        showCommandError(`Command '${command}' rejected: ${errMsg}`);
        // If movement command fails, auto-halt to prevent runaway state
        if (command !== "stop") {
          emergencyStop();
        }
      } else {
        clearCommandError();
      }
    } catch (err) {
      showCommandError(`Network error transmitting '${command}'. Rover 500 ms timeout active.`);
      if (command !== "stop") {
        emergencyStop();
      }
    }
  }

  function showCommandError(msg) {
    if (el.commandAlert) {
      el.commandAlert.style.display = "flex";
      el.commandAlertText.textContent = msg;
    }
  }

  function clearCommandError() {
    if (el.commandAlert) {
      el.commandAlert.style.display = "none";
    }
  }

  /**
   * Shared emergency stop routine.
   * Clears timers, resets mutex, updates readout, and dispatches STOP.
   * STOP is ALWAYS available regardless of rover online state or mutex.
   */
  function emergencyStop() {
    if (streamTimer !== null) {
      clearInterval(streamTimer);
      streamTimer = null;
    }
    activeCommand = null;

    if (activePointerTarget && activePointerId !== null) {
      try {
        activePointerTarget.releasePointerCapture(activePointerId);
      } catch (e) {
        // Pointer may have already been released
      }
    }
    activePointerId = null;
    activePointerTarget = null;

    document.querySelectorAll(".btn-dir, .btn-center-stop").forEach(b => {
      b.classList.remove("btn-active");
    });

    if (el.currentCommandReadout) {
      el.currentCommandReadout.className = "badge badge-neutral";
      el.currentCommandReadout.textContent = "IDLE (STOP)";
    }

    sendCommand("stop");
  }

  /**
   * Handle directional button press-and-hold start.
   */
  function startMovement(command, target, pointerId) {
    // STOP is always allowed
    if (command === "stop") {
      emergencyStop();
      return;
    }

    // Directional movement requires rover to be online
    if (!isRoverOnline) {
      showCommandError("Cannot move rover: device is offline or in contact loss.");
      return;
    }

    // Mutex: Prevent multiple simultaneous movement commands
    if (activeCommand !== null) {
      return;
    }

    activeCommand = command;
    activePointerId = pointerId;
    activePointerTarget = target;

    try {
      target.setPointerCapture(pointerId);
    } catch (e) {
      // Ignored if capture unsupported
    }

    target.classList.add("btn-active");

    if (el.currentCommandReadout) {
      el.currentCommandReadout.className = "badge badge-warning";
      el.currentCommandReadout.textContent = `REQUESTED: ${command.toUpperCase()}`;
    }

    // Send first command packet immediately
    sendCommand(command);

    // Stream repeating command packets every 200 ms while held
    streamTimer = setInterval(() => {
      if (activeCommand === command) {
        sendCommand(command);
      }
    }, STREAM_INTERVAL_MS);
  }

  /**
   * Handle directional button release.
   */
  function stopMovement(target, pointerId) {
    if (activePointerId !== null && pointerId !== activePointerId) {
      return; // Ignore events from other pointers
    }
    emergencyStop();
  }

  /**
   * Bind motor control events.
   */
  function bindMotorControls() {
    const dirButtons = [el.btnForward, el.btnReverse, el.btnLeft, el.btnRight];

    dirButtons.forEach(btn => {
      if (!btn) return;
      const cmd = btn.dataset.command;

      btn.addEventListener("pointerdown", e => {
        e.preventDefault();
        startMovement(cmd, btn, e.pointerId);
      });

      btn.addEventListener("pointerup", e => {
        e.preventDefault();
        stopMovement(btn, e.pointerId);
      });

      btn.addEventListener("pointercancel", e => {
        e.preventDefault();
        stopMovement(btn, e.pointerId);
      });

      btn.addEventListener("contextmenu", e => e.preventDefault());
    });

    // Center STOP button
    if (el.btnCenterStop) {
      el.btnCenterStop.addEventListener("click", e => {
        e.preventDefault();
        emergencyStop();
      });
      el.btnCenterStop.addEventListener("pointerdown", e => {
        e.preventDefault();
        emergencyStop();
      });
    }

    // Emergency STOP button
    if (el.btnEmergencyStop) {
      el.btnEmergencyStop.addEventListener("click", e => {
        e.preventDefault();
        emergencyStop();
      });
      el.btnEmergencyStop.addEventListener("pointerdown", e => {
        e.preventDefault();
        emergencyStop();
      });
    }

    // Safety releases: window blur, tab visibility change, pointerleave
    window.addEventListener("blur", emergencyStop);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        emergencyStop();
      }
    });
  }

  /**
   * Update directional controls enabled/disabled state based on rover connectivity.
   */
  function updateControlLock(isOnline) {
    isRoverOnline = isOnline;
    const dirButtons = [el.btnForward, el.btnReverse, el.btnLeft, el.btnRight];

    if (isOnline) {
      if (el.controlLockBanner) el.controlLockBanner.style.display = "none";
      dirButtons.forEach(b => {
        if (b) b.disabled = false;
      });
    } else {
      if (el.controlLockBanner) el.controlLockBanner.style.display = "flex";
      dirButtons.forEach(b => {
        if (b) b.disabled = true;
      });
      // If was moving when disconnected, halt immediately
      if (activeCommand !== null) {
        emergencyStop();
      }
    }
  }

  // =========================================================================
  // Telemetry, Health, and Sensor Polling
  // =========================================================================

  /**
   * Fetch and update backend & broker health.
   */
  async function updateHealth() {
    try {
      const res = await fetch("/api/v1/health");
      const data = await res.json();
      if (res.ok && data.status === "ok") {
        el.backendStatusPill.className = "badge badge-online";
        el.backendStatusPill.textContent = "ONLINE";
        el.dbStatus.textContent = data.database === "connected" ? "SQLite WAL (Connected)" : data.database;

        if (data.broker === "connected") {
          el.brokerStatusPill.className = "badge badge-online";
          el.brokerStatusPill.textContent = "CONNECTED";
        } else {
          el.brokerStatusPill.className = "badge badge-offline";
          el.brokerStatusPill.textContent = "OFFLINE";
        }
      } else {
        el.backendStatusPill.className = "badge badge-warning";
        el.backendStatusPill.textContent = "DEGRADED";
      }
    } catch (err) {
      el.backendStatusPill.className = "badge badge-offline";
      el.backendStatusPill.textContent = "UNREACHABLE";
      el.brokerStatusPill.className = "badge badge-offline";
      el.brokerStatusPill.textContent = "UNKNOWN";
    }
  }

  /**
   * Fetch and update device status.
   */
  async function updateDeviceStatus() {
    try {
      const res = await fetch("/api/v1/status/latest");
      const json = await res.json();
      if (res.ok && json.data) {
        const d = json.data;
        el.deviceId.textContent = d.device_id || "—";
        el.deviceRssi.textContent = d.wifi_rssi_dbm !== null ? `${d.wifi_rssi_dbm} dBm` : "N/A";
        el.deviceUptime.textContent = d.uptime_s !== null ? formatUptime(d.uptime_s) : "N/A";

        if (d.status === "online") {
          el.deviceStatusPill.className = "badge badge-online";
          el.deviceStatusPill.textContent = "ONLINE";
          updateControlLock(true);
        } else if (d.status === "degraded") {
          el.deviceStatusPill.className = "badge badge-warning";
          el.deviceStatusPill.textContent = "DEGRADED";
          updateControlLock(false);
        } else {
          el.deviceStatusPill.className = "badge badge-offline";
          el.deviceStatusPill.textContent = "OFFLINE";
          updateControlLock(false);
        }
      } else {
        el.deviceStatusPill.className = "badge badge-neutral";
        el.deviceStatusPill.textContent = "NO STATUS RECORD";
        updateControlLock(false);
      }
    } catch (err) {
      el.deviceStatusPill.className = "badge badge-neutral";
      el.deviceStatusPill.textContent = "FETCH ERROR";
      updateControlLock(false);
    }
  }

  /**
   * Fetch and update latest telemetry readings.
   */
  async function updateLatestTelemetry() {
    try {
      const res = await fetch("/api/v1/telemetry/latest");
      const json = await res.json();

      if (!res.ok || !json.data) {
        showNoDataState(json.message || "No telemetry recorded yet");
        return;
      }

      const t = json.data;
      renderProvenance(t.source);
      checkStaleness(t.received_at);

      // Render Temperature
      if (t.temperature_c !== null) {
        el.valTemp.textContent = t.temperature_c.toFixed(1);
        el.stateTemp.textContent = "Sensor reading OK";
      } else {
        el.valTemp.textContent = "--";
        el.stateTemp.textContent = "UNAVAILABLE";
      }

      // Render Humidity
      if (t.humidity_pct !== null) {
        el.valHumidity.textContent = t.humidity_pct.toFixed(1);
        el.stateHumidity.textContent = "Sensor reading OK";
      } else {
        el.valHumidity.textContent = "--";
        el.stateHumidity.textContent = "UNAVAILABLE";
      }

      // Render Light
      if (t.light_lux !== null) {
        el.valLight.textContent = t.light_lux.toFixed(0);
        el.stateLight.textContent = "Sensor reading OK";
      } else {
        el.valLight.textContent = "--";
        el.stateLight.textContent = "UNAVAILABLE";
      }

      // Render Gas MQ-135 (Raw ADC only)
      if (t.mq135_raw !== null) {
        el.valGas.textContent = t.mq135_raw;
      } else {
        el.valGas.textContent = "--";
      }

      // Render GPS
      renderGPS(t.gps);

      latestTelemetryData = t;
      checkAdviceTelemetryStaleness();

    } catch (err) {
      console.error("Failed to fetch latest telemetry:", err);
    }
  }

  /**
   * Render provenance badge: SIMULATED vs REAL.
   */
  function renderProvenance(source) {
    if (source === "simulated") {
      el.provenanceBadge.className = "badge badge-simulated";
      el.provenanceText.textContent = "SIMULATED DATA";
    } else if (source === "real") {
      el.provenanceBadge.className = "badge badge-real";
      el.provenanceText.textContent = "REAL SENSOR DATA";
    } else {
      el.provenanceBadge.className = "badge badge-neutral";
      el.provenanceText.textContent = "SOURCE UNKNOWN";
    }
  }

  /**
   * Check if telemetry is stale based on received_at.
   */
  function checkStaleness(receivedAtIso) {
    if (!receivedAtIso) return;
    const receivedTime = new Date(receivedAtIso).getTime();
    const now = Date.now();
    const ageSeconds = Math.max(0, Math.round((now - receivedTime) / 1000));

    el.lastUpdated.textContent = `Updated ${new Date(receivedAtIso).toLocaleTimeString()}`;

    if (ageSeconds > STALE_THRESHOLD_SECONDS) {
      el.staleAlert.style.display = "flex";
      el.staleMessage.textContent = `Telemetry is stale — last received ${ageSeconds}s ago (${new Date(receivedAtIso).toLocaleTimeString()}). Live values unconfirmed.`;
      document.querySelectorAll(".metric-card").forEach(c => c.classList.add("metric-stale"));
    } else {
      el.staleAlert.style.display = "none";
      document.querySelectorAll(".metric-card").forEach(c => c.classList.remove("metric-stale"));
    }
  }

  /**
   * Render GPS state and update Leaflet map.
   */
  function renderGPS(gps) {
    if (gps && gps.fix === true && gps.latitude !== null && gps.longitude !== null) {
      el.gpsFixBadge.className = "badge badge-online";
      el.gpsFixBadge.textContent = "GPS FIX LOCKED";
      el.noFixOverlay.style.display = "none";

      el.gpsLat.textContent = gps.latitude.toFixed(6);
      el.gpsLon.textContent = gps.longitude.toFixed(6);
      el.gpsSats.textContent = gps.satellites !== null ? gps.satellites : "—";
      el.gpsAlt.textContent = gps.altitude_m !== null ? `${gps.altitude_m.toFixed(1)} m` : "—";
      el.gpsSpd.textContent = gps.speed_mps !== null ? `${gps.speed_mps.toFixed(1)} m/s` : "—";

      if (map) {
        const latLng = [gps.latitude, gps.longitude];
        if (!roverMarker) {
          roverMarker = L.marker(latLng).addTo(map);
          roverMarker.bindPopup("<b>ARIA Rover</b><br>Live GPS Coordinates").openPopup();
          map.setView(latLng, 16);
        } else {
          roverMarker.setLatLng(latLng);
        }
      }
    } else {
      el.gpsFixBadge.className = "badge badge-warning";
      el.gpsFixBadge.textContent = "NO GPS FIX";
      el.noFixOverlay.style.display = "flex";

      el.gpsLat.textContent = "--";
      el.gpsLon.textContent = "--";
      el.gpsSats.textContent = gps && gps.satellites !== null ? gps.satellites : "0";
      el.gpsAlt.textContent = "--";
      el.gpsSpd.textContent = "--";

      if (map && roverMarker) {
        map.removeLayer(roverMarker);
        roverMarker = null;
      }
    }
  }

  /**
   * Handle empty database / no telemetry state.
   */
  function showNoDataState(msg) {
    el.provenanceBadge.className = "badge badge-neutral";
    el.provenanceText.textContent = "NO TELEMETRY";
    el.lastUpdated.textContent = "No data recorded";
    el.staleAlert.style.display = "flex";
    el.staleMessage.textContent = msg;

    el.valTemp.textContent = "--";
    el.valHumidity.textContent = "--";
    el.valLight.textContent = "--";
    el.valGas.textContent = "--";
    renderGPS(null);
    latestTelemetryData = null;
    checkAdviceTelemetryStaleness();
  }

  /**
   * Fetch historical telemetry and update Chart.js (environmental only).
   */
  async function updateCharts() {
    try {
      const res = await fetch("/api/v1/telemetry/history?limit=30");
      const json = await res.json();
      if (!res.ok || !json.data || json.data.length === 0) {
        return;
      }

      const points = json.data.slice().reverse();
      const labels = points.map(p => {
        const d = new Date(p.received_at || p.device_timestamp);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      });

      // Update Chart 1: Temp & Humidity
      if (chartTempHum) {
        chartTempHum.data.labels = labels;
        chartTempHum.data.datasets[0].data = points.map(p => p.temperature_c);
        chartTempHum.data.datasets[1].data = points.map(p => p.humidity_pct);
        chartTempHum.update("none");
      }

      // Update Chart 2: Light & Gas Raw ADC
      if (chartLightGas) {
        chartLightGas.data.labels = labels;
        chartLightGas.data.datasets[0].data = points.map(p => p.light_lux);
        chartLightGas.data.datasets[1].data = points.map(p => p.mq135_raw);
        chartLightGas.update("none");
      }
    } catch (err) {
      console.error("Failed to fetch telemetry history for charts:", err);
    }
  }

  function formatUptime(seconds) {
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  // Advice Engine Client State
  let cooldownTimerInterval = null;
  let cooldownRemainingSeconds = 0;
  let isGeneratingAdvice = false;
  let currentRenderedAdvice = null;
  let latestTelemetryData = null;

  /**
   * Safely parse an ISO date string into epoch milliseconds. Returns null if invalid.
   */
  function parseTimestamp(ts) {
    if (!ts) return null;
    try {
      const d = new Date(ts);
      const time = d.getTime();
      return isNaN(time) ? null : time;
    } catch (e) {
      return null;
    }
  }

  /**
   * Check if live telemetry is newer than the snapshot evaluated in the displayed advice.
   * If newer telemetry has arrived, display the stale advice warning banner.
   */
  function checkAdviceTelemetryStaleness() {
    if (!el.adviceStaleTelemetryNotice) return;
    if (!currentRenderedAdvice) {
      el.adviceStaleTelemetryNotice.style.display = "none";
      return;
    }

    if (!latestTelemetryData || !latestTelemetryData.received_at) {
      el.adviceStaleTelemetryNotice.style.display = "none";
      return;
    }

    const liveTime = parseTimestamp(latestTelemetryData.received_at);
    if (liveTime === null) {
      el.adviceStaleTelemetryNotice.style.display = "none";
      return;
    }

    const telemContext = currentRenderedAdvice.telemetry_context;
    const adviceTelemIso = (telemContext && telemContext.telemetry_timestamp) 
      || currentRenderedAdvice.telemetry_timestamp;

    if (!adviceTelemIso) {
      // Advice was evaluated when no telemetry existed in storage, but live telemetry is now present
      el.adviceStaleTelemetryNotice.style.display = "flex";
      if (el.adviceStaleTelemetryMessage) {
        const liveStr = new Date(liveTime).toLocaleTimeString();
        el.adviceStaleTelemetryMessage.textContent = 
          `New live telemetry received at ${liveStr} — displayed advice was generated without telemetry and has not yet been refreshed. Click "Generate Advice" to evaluate live field conditions.`;
      }
      return;
    }

    const adviceTelemTime = parseTimestamp(adviceTelemIso);
    if (adviceTelemTime === null) {
      el.adviceStaleTelemetryNotice.style.display = "none";
      return;
    }

    // Compare parsed epoch milliseconds; require > 1000ms difference to avoid sub-second clock jitter
    if (liveTime - adviceTelemTime > 1000) {
      el.adviceStaleTelemetryNotice.style.display = "flex";
      if (el.adviceStaleTelemetryMessage) {
        const liveStr = new Date(liveTime).toLocaleTimeString();
        const evalStr = new Date(adviceTelemTime).toLocaleTimeString();
        el.adviceStaleTelemetryMessage.textContent = 
          `New live telemetry received at ${liveStr} — displayed advice reflects earlier field readings (${evalStr}) and has not yet been refreshed. Click "Generate Advice" to update.`;
      }
    } else {
      el.adviceStaleTelemetryNotice.style.display = "none";
    }
  }

  /**
   * Escape HTML to prevent injection when rendering text.
   */
  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  /**
   * Start cooldown countdown timer and disable generation button.
   */
  function startCooldownTimer(seconds) {
    if (cooldownTimerInterval) {
      clearInterval(cooldownTimerInterval);
      cooldownTimerInterval = null;
    }

    cooldownRemainingSeconds = Math.ceil(seconds);
    if (cooldownRemainingSeconds <= 0) {
      if (el.adviceCooldownIndicator) el.adviceCooldownIndicator.style.display = "none";
      if (el.btnGenerateAdvice && !isGeneratingAdvice) {
        el.btnGenerateAdvice.disabled = false;
        if (el.btnGenerateAdviceText) el.btnGenerateAdviceText.textContent = "Generate Advice";
      }
      return;
    }

    if (el.adviceCooldownIndicator) el.adviceCooldownIndicator.style.display = "inline-block";
    if (el.btnGenerateAdvice) el.btnGenerateAdvice.disabled = true;

    function tick() {
      if (el.adviceCooldownTimer) {
        el.adviceCooldownTimer.textContent = cooldownRemainingSeconds;
      }
      if (cooldownRemainingSeconds <= 0) {
        clearInterval(cooldownTimerInterval);
        cooldownTimerInterval = null;
        if (el.adviceCooldownIndicator) el.adviceCooldownIndicator.style.display = "none";
        if (el.btnGenerateAdvice && !isGeneratingAdvice) {
          el.btnGenerateAdvice.disabled = false;
          if (el.btnGenerateAdviceText) el.btnGenerateAdviceText.textContent = "Generate Advice";
        }
      } else {
        cooldownRemainingSeconds--;
      }
    }

    tick();
    cooldownTimerInterval = setInterval(tick, 1000);
  }

  /**
   * Render advice payload to the DOM.
   */
  function renderAdvice(advice) {
    if (!advice) return;
    currentRenderedAdvice = advice;

    if (el.adviceEmptyState) el.adviceEmptyState.style.display = "none";
    if (el.adviceContent) el.adviceContent.style.display = "block";
    if (el.adviceAlert) el.adviceAlert.style.display = "none";

    // 1. Advice Mode Badge
    if (el.adviceModeBadge) {
      const mode = advice.advice_mode || "UNKNOWN";
      el.adviceModeBadge.textContent = mode.replace(/_/g, " ");
      if (mode === "FULL_CONTEXT") {
        el.adviceModeBadge.className = "badge badge-online";
      } else if (mode === "LIMITED_CONTEXT") {
        el.adviceModeBadge.className = "badge badge-warning";
      } else {
        el.adviceModeBadge.className = "badge badge-neutral";
      }
    }

    // 2. Advice Provider Badge
    if (el.adviceProviderBadge) {
      const provider = advice.provider || "deterministic_fallback";
      if (provider === "nvidia_nim") {
        el.adviceProviderBadge.textContent = "NVIDIA NIM AI";
        el.adviceProviderBadge.className = "badge badge-accent";
      } else {
        el.adviceProviderBadge.textContent = "Deterministic Fallback";
        el.adviceProviderBadge.className = "badge badge-neutral";
      }
    }

    // 3. Timestamps & Metadata
    if (el.adviceTimestamp && advice.generated_at) {
      try {
        const d = new Date(advice.generated_at);
        el.adviceTimestamp.textContent = isNaN(d.getTime()) ? advice.generated_at : d.toLocaleString();
      } catch (e) {
        el.adviceTimestamp.textContent = advice.generated_at;
      }
    }

    const telemCtx = advice.telemetry_context || {};
    const evalTelemIso = telemCtx.telemetry_timestamp || advice.telemetry_timestamp;
    if (el.adviceTelemetryTime) {
      if (evalTelemIso) {
        try {
          const dt = new Date(evalTelemIso);
          el.adviceTelemetryTime.textContent = isNaN(dt.getTime()) ? evalTelemIso : dt.toLocaleTimeString();
        } catch (e) {
          el.adviceTelemetryTime.textContent = evalTelemIso;
        }
      } else {
        el.adviceTelemetryTime.textContent = "None (No Telemetry)";
      }
    }

    // Telemetry freshness badge (at evaluation time)
    if (el.adviceTelemetryFreshnessBadge) {
      const freshness = telemCtx.telemetry_freshness || advice.telemetry_freshness || "unknown";
      if (freshness === "current") {
        el.adviceTelemetryFreshnessBadge.className = "badge badge-online";
        el.adviceTelemetryFreshnessBadge.textContent = "CURRENT TELEMETRY";
      } else if (freshness === "stale") {
        const ageSec = telemCtx.telemetry_age_seconds !== undefined ? telemCtx.telemetry_age_seconds : advice.telemetry_age_seconds;
        el.adviceTelemetryFreshnessBadge.className = "badge badge-warning";
        el.adviceTelemetryFreshnessBadge.textContent = (ageSec !== null && ageSec !== undefined) 
          ? `STALE TELEMETRY (${Math.round(ageSec)}s)`
          : "STALE TELEMETRY";
      } else if (freshness === "missing") {
        el.adviceTelemetryFreshnessBadge.className = "badge badge-offline";
        el.adviceTelemetryFreshnessBadge.textContent = "NO TELEMETRY";
      } else {
        el.adviceTelemetryFreshnessBadge.className = "badge badge-neutral";
        el.adviceTelemetryFreshnessBadge.textContent = "UNKNOWN FRESHNESS";
      }
    }

    // GPS provenance badge
    if (el.adviceGpsProvenanceBadge) {
      const prov = advice.gps_provenance || "unavailable";
      if (prov === "current_fix") {
        el.adviceGpsProvenanceBadge.className = "badge badge-online";
        el.adviceGpsProvenanceBadge.textContent = "CURRENT GPS FIX";
      } else if (prov === "last_known_fix") {
        el.adviceGpsProvenanceBadge.className = "badge badge-warning";
        el.adviceGpsProvenanceBadge.textContent = "LAST KNOWN GPS";
      } else {
        el.adviceGpsProvenanceBadge.className = "badge badge-neutral";
        el.adviceGpsProvenanceBadge.textContent = "NO GPS FIX";
      }
    }

    // 4. Executive Summary
    if (el.adviceSummaryText) {
      el.adviceSummaryText.textContent = advice.summary || "No summary provided.";
    }

    // 5. Recommendations List
    if (el.adviceRecommendationsList) {
      const recs = advice.recommendations || [];
      if (recs.length === 0) {
        el.adviceRecommendationsList.innerHTML = `<div class="rec-explanation">No specific field actions recommended.</div>`;
      } else {
        el.adviceRecommendationsList.innerHTML = recs.map(r => {
          const conf = (r.confidence || "medium").toLowerCase();
          return `
            <div class="recommendation-item">
              <div class="rec-header">
                <span class="rec-title">${escapeHtml(r.title)}</span>
                <span class="rec-confidence rec-confidence-${conf}">${escapeHtml(conf)} confidence</span>
              </div>
              <p class="rec-explanation">${escapeHtml(r.explanation)}</p>
            </div>
          `;
        }).join("");
      }
    }

    // 6. Sensor Health Audit List
    if (el.sensorHealthList) {
      const health = advice.sensor_health || [];
      if (health.length === 0) {
        el.sensorHealthList.innerHTML = `<div class="sensor-detail">No sensor health data recorded.</div>`;
      } else {
        el.sensorHealthList.innerHTML = health.map(h => {
          const state = (h.state || "unknown").toLowerCase();
          let badgeClass = "badge-neutral";
          if (state === "healthy") badgeClass = "badge-online";
          else if (state === "stale") badgeClass = "badge-warning";
          else if (state === "unresponsive_null") badgeClass = "badge-danger";
          else if (state === "device_offline") badgeClass = "badge-offline";

          return `
            <div class="sensor-health-row">
              <div>
                <span class="sensor-name">${escapeHtml(h.sensor)}</span>
                <div class="sensor-detail">${escapeHtml(h.detail || "")}</div>
              </div>
              <span class="badge ${badgeClass}">${escapeHtml(state.replace(/_/g, " ").toUpperCase())}</span>
            </div>
          `;
        }).join("");
      }
    }

    // 7. Weather & Microclimate Context
    if (el.weatherContextBody) {
      const w = advice.weather || {};
      const status = w.status || "unavailable";
      if (status !== "available") {
        el.weatherContextBody.innerHTML = `
          <div class="weather-metric-row">
            <span class="weather-metric-label">Status:</span>
            <span class="badge badge-neutral">UNAVAILABLE</span>
          </div>
          <div class="sensor-detail">Localized weather forecast could not be loaded. Operating on rover sensors only.</div>
        `;
      } else {
        const curr = w.current || {};
        const fcast = w.forecast || {};
        const locSrc = w.location_source || "LOCATION_UNAVAILABLE";
        let locBadge = "";
        let locDetail = "";

        if (locSrc === "CURRENT_LOCATION") {
          locBadge = `<span class="badge badge-online">CURRENT GPS FIX</span>`;
        } else if (locSrc === "LAST_KNOWN_LOCATION") {
          locBadge = `<span class="badge badge-warning">LAST KNOWN GPS</span>`;
          const locAgeSec = (w.location_age_seconds !== null && w.location_age_seconds !== undefined) 
            ? Math.round(w.location_age_seconds) : null;
          let locTime = null;
          if (w.location_timestamp) {
            try {
              const dt = new Date(w.location_timestamp);
              locTime = isNaN(dt.getTime()) ? null : dt.toLocaleTimeString();
            } catch (e) {
              locTime = null;
            }
          }
          if (locTime || locAgeSec !== null) {
            locDetail = `<div class="sensor-detail">Coordinates recorded at ${locTime || "earlier"}${locAgeSec !== null ? " (" + formatUptime(locAgeSec) + " ago)" : ""}. Live GPS fix unconfirmed.</div>`;
          }
        } else {
          locBadge = `<span class="badge badge-neutral">COORDINATES UNAVAILABLE</span>`;
        }

        const t = curr.temperature_c !== undefined && curr.temperature_c !== null ? `${curr.temperature_c.toFixed(1)}°C` : "N/A";
        const h = curr.humidity_pct !== undefined && curr.humidity_pct !== null ? `${curr.humidity_pct.toFixed(0)}%` : "N/A";
        const rainProb = fcast.precipitation_probability_next_6h_pct !== undefined && fcast.precipitation_probability_next_6h_pct !== null 
          ? `${fcast.precipitation_probability_next_6h_pct}%` : "0%";
        const rainMm = fcast.precipitation_next_6h_mm !== undefined && fcast.precipitation_next_6h_mm !== null
          ? `${fcast.precipitation_next_6h_mm.toFixed(1)} mm` : "0.0 mm";
        const wind = curr.wind_speed_kmh !== undefined && curr.wind_speed_kmh !== null ? `${curr.wind_speed_kmh.toFixed(1)} km/h` : "N/A";

        el.weatherContextBody.innerHTML = `
          <div class="weather-metric-row">
            <span class="weather-metric-label">Location Source:</span>
            ${locBadge}
          </div>
          ${locDetail}
          <div class="weather-metric-row">
            <span class="weather-metric-label">Regional Air Temp:</span>
            <span class="weather-metric-val">${t}</span>
          </div>
          <div class="weather-metric-row">
            <span class="weather-metric-label">Regional Humidity:</span>
            <span class="weather-metric-val">${h}</span>
          </div>
          <div class="weather-metric-row">
            <span class="weather-metric-label">Rain (Next 6h):</span>
            <span class="weather-metric-val">${rainProb} (${rainMm})</span>
          </div>
          <div class="weather-metric-row">
            <span class="weather-metric-label">Wind Speed:</span>
            <span class="weather-metric-val">${wind}</span>
          </div>
        `;
      }
    }

    // 8. Deterministic Environmental Observations
    if (el.adviceObservationsList) {
      const obs = advice.observations || [];
      if (obs.length === 0) {
        el.adviceObservationsList.innerHTML = `<div class="obs-explanation">No anomalous environmental threshold triggers detected. All active sensors operating within nominal bounds.</div>`;
      } else {
        el.adviceObservationsList.innerHTML = obs.map(o => {
          const sev = (o.severity || "info").toLowerCase();
          const badgeClass = sev === "warning" ? "badge-warning" : (sev === "alert" ? "badge-danger" : "badge-neutral");
          return `
            <div class="observation-row">
              <div class="obs-header">
                <span class="badge ${badgeClass}">[${escapeHtml(sev.toUpperCase())}]</span>
                <span class="obs-title">${escapeHtml(o.title)}</span>
              </div>
              <p class="obs-explanation">${escapeHtml(o.explanation)}</p>
            </div>
          `;
        }).join("");
      }
    }

    // 9. Limitations & Operating Notices
    if (el.adviceLimitationsList) {
      const lims = advice.limitations || [];
      if (lims.length === 0) {
        el.adviceLimitationsList.innerHTML = `<li>Rover telemetry and regional weather operating normally.</li>`;
      } else {
        el.adviceLimitationsList.innerHTML = lims.map(l => `<li>${escapeHtml(l)}</li>`).join("");
      }
    }

    // Synchronize stale-advice warning banner
    checkAdviceTelemetryStaleness();
  }

  /**
   * Fetch latest saved advice from backend on initial load.
   */
  async function fetchLatestAdvice() {
    try {
      const res = await fetch("/api/v1/advice/latest");
      if (!res.ok) return;
      const json = await res.json();
      if (json && json.status === "success" && json.data) {
        renderAdvice(json.data);
        if (json.data.cooldown && json.data.cooldown.active) {
          startCooldownTimer(json.data.cooldown.remaining_seconds);
        }
      }
    } catch (err) {
      console.warn("Could not load initial advice from /api/v1/advice/latest:", err);
    }
  }

  /**
   * Handle manual "Generate Advice" button click.
   */
  async function handleGenerateAdviceClick() {
    if (isGeneratingAdvice) return;
    isGeneratingAdvice = true;

    if (el.btnGenerateAdvice) el.btnGenerateAdvice.disabled = true;
    if (el.btnGenerateAdviceText) el.btnGenerateAdviceText.textContent = "Evaluating field conditions...";
    if (el.adviceAlert) el.adviceAlert.style.display = "none";

    try {
      const res = await fetch("/api/v1/advice/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const json = await res.json();

      if (!res.ok || json.status !== "success" || !json.data) {
        const msg = (json && json.message) || `Server returned HTTP ${res.status}`;
        if (el.adviceAlert && el.adviceAlertText) {
          el.adviceAlertText.textContent = `Advice generation error: ${msg}`;
          el.adviceAlert.style.display = "flex";
        }
      } else {
        renderAdvice(json.data);
        if (json.data.cooldown && json.data.cooldown.active) {
          startCooldownTimer(json.data.cooldown.remaining_seconds);
        }
      }
    } catch (err) {
      console.error("Advice generation network failure:", err);
      if (el.adviceAlert && el.adviceAlertText) {
        el.adviceAlertText.textContent = "Network error connecting to backend advice service.";
        el.adviceAlert.style.display = "flex";
      }
    } finally {
      isGeneratingAdvice = false;
      if (cooldownRemainingSeconds <= 0) {
        if (el.btnGenerateAdvice) el.btnGenerateAdvice.disabled = false;
        if (el.btnGenerateAdviceText) el.btnGenerateAdviceText.textContent = "Generate Advice";
      }
    }
  }

  function bindAdviceControls() {
    if (el.btnGenerateAdvice) {
      el.btnGenerateAdvice.addEventListener("click", handleGenerateAdviceClick);
    }
  }

  /**
   * Main polling tick.
   */
  async function poll() {
    await updateHealth();
    await updateDeviceStatus();
    await updateLatestTelemetry();
    await updateCharts();
  }

  // Initialization on DOMContentLoaded
  document.addEventListener("DOMContentLoaded", () => {
    initMap();
    initCharts();
    bindMotorControls();
    bindAdviceControls();
    fetchLatestAdvice();
    poll();
    setInterval(poll, POLL_INTERVAL_MS);
  });

})();
