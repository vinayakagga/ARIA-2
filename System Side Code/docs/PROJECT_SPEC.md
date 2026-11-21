# ARIA --- Project Description and Source of Truth

**Document status:** Working project specification\
**Last updated:** 14 November 2026\
**Purpose:** Keep the project scope, confirmed decisions, assumptions,
and unfinished work explicit so future development does not rely on
guesses.

------------------------------------------------------------------------

## 1. Project overview

ARIA is a manually operated, 2WD agricultural environmental-monitoring
rover. It is intended to collect and transmit environmental readings
while an operator drives it from a PC dashboard over a local Wi-Fi
network.

The PC is the control station and server. The ESP32 on the rover handles
device-side sensing, communication, and motor control. The initial
software development will use an ESP32-C3 Mini as a temporary MQTT
publisher. The planned final controller is a standard ESP32 DevKit,
subject to physical identification and verification before wiring or
firmware migration.

ARIA is a prototype and monitoring platform. The currently selected
sensors do **not** establish soil health, crop disease, or calibrated
pollutant/CO₂ measurements.

## 2. Scope and operating mode

### In scope

-   Manual rover control from a browser-based dashboard hosted on the
    PC.
-   Environmental sensor telemetry from the rover to the PC.
-   Local Wi-Fi communication through the user's existing router.
-   MQTT messaging between the rover and PC.
-   Local data storage and historical visualisation.
-   GPS position reporting when a valid outdoor fix is available.
-   Offline operation on the local network, without a cloud service
    dependency.

### Explicitly out of scope for the current build

-   Autonomous navigation or autonomous obstacle avoidance.
-   HC-SR04 ultrasonic sensor (removed from hardware build).
-   Servo mechanisms.
-   Soil-moisture sensing.
-   Cloud hosting or cloud data dependency.
-   Crop disease prediction or unsupported agricultural diagnosis.
-   Treating MQ-135 raw readings as calibrated CO₂ or air-quality
    concentrations.

Note: The HC-SR04 ultrasonic sensor has been completely removed from the hardware inventory. Front obstacle distance measurement and forward safety inhibit sensors are excluded. Rover motion relies on manual control with a 500 ms device-side command watchdog timeout.

## 3. Hardware inventory and status

The following hardware has been identified in the project discussion.
Exact board revisions, module variants, electrical ratings, and wiring
still need physical verification.

  -----------------------------------------------------------------------
  Component               Intended role           Status / caveat
  ----------------------- ----------------------- -----------------------
  ESP32-C3 Mini           Temporary development   User has one; exact
                          publisher               board pinout must be
                                                  checked

  Standard ESP32 DevKit   Planned final rover     User expects to
                          controller              obtain/find one; not
                                                  yet verified

  DHT22                   Temperature and         Module/bare-sensor
                          relative humidity       variant and wiring to
                                                  verify

  BH1750                  Ambient light in lux    I²C module variant and
                                                  address to verify

  MQ-135 module           Gas-sensitive analogue  Raw reading only until
                          reading                 properly calibrated;
                                                  heater power required

  NEO-6M GPS              GPS fix and coordinates Exact breakout power
                                                  requirements to verify;
                                                  best tested outdoors

  HC-SR04                 Front distance          REMOVED from hardware;
                          measurement             no longer used or
                                                  mounted on rover

  L298N                   Dual DC motor driver    Exact board, jumper
                                                  arrangement, motor
                                                  supply and current
                                                  limits to verify

  2WD chassis and DC      Rover movement          Motor voltage/current
  motors                                          ratings not yet
                                                  confirmed

  Battery and buck        Rover power             A 2-cell 7.4 V nominal
  converter                                       battery and buck
                                                  arrangement were
                                                  discussed, but exact
                                                  ratings and protection
                                                  are unverified
  -----------------------------------------------------------------------

Other previously mentioned modules (including MPU9250, flame, PIR, sound
sensor, Tiny RTC and servo) are **not part of the current selected
build**. Do not add them unless the user explicitly changes scope.

## 4. Software architecture

### PC / control station

The user's main Windows 11 PC will host:

1.  **Mosquitto** --- local MQTT broker.
2.  **Python backend** --- MQTT subscriber/publisher and application
    logic.
3.  **Flask** --- HTTP server and API for the dashboard.
4.  **SQLite** --- local persistence for telemetry/history.
5.  **Browser dashboard** --- HTML, CSS and JavaScript.
6.  **Chart.js** --- telemetry charts.
7.  **Leaflet** --- GPS map display.

The dashboard is planned to be available at `http://localhost:5000`.
Access from another device on the same LAN may be added using the PC's
LAN IP and suitable firewall configuration.

Map tiles may require internet access unless an offline tile source is
configured. Therefore, do not claim that the map itself is fully offline
until tested.

### Rover / device

The device-side firmware will eventually handle:

-   Wi-Fi connection and recovery.
-   MQTT connection and message handling.
-   Sensor sampling.
-   Telemetry publishing.
-   GPS parsing.
-   Motor driver outputs.
-   Command validation and safety logic.
-   Device status and command acknowledgements.

The temporary C3 publisher is a development aid. The PC backend must not
depend on C3-specific features or pin assignments.

## 5. Development environment

  Area                        Selected choice
  --------------------------- -------------------------
  PC operating system         Windows 11
  Device development IDE      Arduino IDE
  Device framework/language   Arduino framework / C++
  Backend language            Python
  Backend web framework       Flask
  MQTT broker                 Eclipse Mosquitto
  Python MQTT client          Paho MQTT
  Local database              SQLite
  Dashboard                   HTML / CSS / JavaScript
  Charts                      Chart.js
  Map                         Leaflet

PlatformIO was considered earlier but was explicitly dropped in favour
of Arduino IDE to keep the initial workflow simple.

## 6. MQTT topic contract

Use the following versioned topic names unless a deliberate, documented
change is agreed:

  Topic                 Direction     Purpose
  --------------------- ------------- --------------------------------
  `aria/v1/telemetry`   Device → PC   Sensor and GPS telemetry
  `aria/v1/status`      Device → PC   Device availability and health
  `aria/v1/command`     PC → Device   Manual movement/stop commands
  `aria/v1/ack`         Device → PC   Command acknowledgement

The PC backend should subscribe to the `aria/v1/#` topic tree as needed.
Do not use retained movement commands. A stale movement command must
never cause the rover to move after reconnecting.

### Telemetry message shape

The following is an initial example/schema direction, not a claim that
all values are already available or measured:

``` json
{
  "device_id": "aria-c3",
  "source": "simulated",
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

Rules:

-   `source` must distinguish simulated/test data from real sensor data.
-   The C3 may publish clearly labelled simulated values during software
    development.
-   The final ESP32 should publish the same agreed message structure
    with real readings.
-   Missing/unavailable sensor values should be represented explicitly
    (for example, `null`) rather than fabricated.
-   The schema may be refined before implementation, but changes must be
    documented and kept consistent between firmware and backend.
-   Add a schema/version field if the message contract evolves.

## 7. Manual control and safety requirements

The rover is manual-only in the current scope.

Planned dashboard interaction:

-   Direction controls should be press-and-hold.
-   Releasing a control should request stop.
-   Commands should carry an identifier and validity/expiry information.
-   The dashboard/backend should maintain a command heartbeat while a
    movement control is held.
-   The device should stop on command timeout, Wi-Fi/MQTT loss, invalid
    command, or emergency stop.
-   The front distance sensor should inhibit forward motion when an
    agreed safety threshold is crossed.
-   Stop/emergency-stop behaviour must take priority over movement
    commands.
-   A physical motor-power disconnect is recommended.

A timeout around 500 ms was discussed as an initial design target only.
It must be validated and tuned during testing; it is not yet a final
specification.

No motor-control firmware or wiring is authorised by this document
alone. First verify the controller, driver, motors, battery, buck
converter, and power arrangement.

## 8. Electrical safety and verification gates

Before connecting or powering the final hardware:

-   Identify the exact ESP32 board and verify its pinout.
-   Verify battery chemistry, cell count, protection/BMS, voltage range
    and discharge capability.
-   Verify motor rated voltage and stall/running current.
-   Verify L298N board details and motor-driver thermal/current
    limitations.
-   Verify buck converter input range, output voltage and current
    capability.
-   Confirm that all ESP32 GPIO signals remain within safe voltage
    limits.
-   Use a divider/level shifter for the HC-SR04 Echo signal if it is 5
    V.
-   Measure/scale MQ-135 analogue output so it cannot exceed the ESP32
    ADC input limit.
-   Confirm GPS module supply requirements from its actual breakout.
-   Provide common ground between modules and supplies.
-   Do not power motors from the ESP32 board.
-   Keep motor power/wiring separated from sensitive sensor wiring where
    practical.
-   Verify power rails with a multimeter before connecting the ESP32.
-   Do not assume a module's pin labels or voltage tolerance from its
    product name alone.

The previously discussed ESP32 DevKit pin map is provisional and must
not be used until the exact board and module variants are confirmed. It
does not apply to the ESP32-C3 Mini.

## 9. Current development status

### Completed

-   [x] Project scope narrowed to a manually controlled
    environmental-monitoring rover.
-   [x] Servo and soil-moisture sensor removed from current scope.
-   [x] Arduino IDE selected instead of PlatformIO.
-   [x] Mosquitto installed on the Windows PC.
-   [x] Local Mosquitto publisher/subscriber test passed using
    `aria/test`.
-   [x] Temporary ESP32-C3 Mini publisher approach selected for early
    software development.

### Not yet completed

-   [ ] Create the Python virtual environment.
-   [ ] Install and verify Paho MQTT in the project environment.
-   [ ] Run the Python MQTT receiver test.
-   [ ] Define and freeze the telemetry schema.
-   [ ] Implement the permanent MQTT backend.
-   [ ] Implement SQLite storage.
-   [ ] Implement Flask routes/API.
-   [ ] Build the browser dashboard and charts.
-   [ ] Build and test temporary C3 firmware publisher.
-   [ ] Test PC-to-device command path using safe test messages.
-   [ ] Identify and verify the standard ESP32 DevKit.
-   [ ] Complete final hardware audit, wiring and power design.
-   [ ] Test individual sensors on the final controller.
-   [ ] Integrate L298N and motors with safety checks.
-   [ ] Run end-to-end LAN/offline and disconnect tests.

### Exact current checkpoint

Mosquitto is installed and running sufficiently for a local test. The
user successfully ran a subscriber and publisher in separate PowerShell
windows and received the test message. The Python virtual environment
and MQTT receiver have **not yet been confirmed working**. Do not assume
they are installed or running.

## 10. Implementation sequence

1.  **PC MQTT receiver:** Set up Python virtual environment, install
    Paho MQTT, and verify Python receives a test publication.
2.  **Telemetry contract:** Agree on required fields, types, units,
    device identity, source label and schema version.
3.  **Backend foundation:** Build MQTT receive/publish service and
    validate malformed/unavailable values.
4.  **Persistence:** Store telemetry and timestamps in SQLite.
5.  **Flask API:** Expose recent readings and historical data.
6.  **Dashboard:** Show connection status, latest readings, charts and
    GPS state.
7.  **C3 publisher:** Connect the ESP32-C3 Mini to Wi-Fi and publish
    clearly labelled simulated telemetry using the agreed schema.
8.  **Command-path prototype:** Test command messages and
    acknowledgements without connecting motors.
9.  **Final hardware audit:** Verify standard ESP32 board, sensors,
    driver, motors and power.
10. **Sensor integration:** Test each sensor individually before
    combining them.
11. **Motor and safety integration:** Validate stop, timeout, disconnect
    and proximity-inhibit behaviour before rover driving.
12. **End-to-end validation:** Test LAN operation, reconnects,
    persistence, sensor faults and safe shutdown.

Do not skip ahead to motor wiring or final firmware while the hardware
and power details remain unverified.

## 11. Repository structure (proposed)

``` text
ARIA/
├── firmware/
│   ├── ARIA.ino
│   ├── config.h
│   ├── sensor_manager.h
│   ├── sensor_manager.cpp
│   ├── gps_manager.h
│   ├── gps_manager.cpp
│   ├── motor_controller.h
│   ├── motor_controller.cpp
│   ├── mqtt_manager.h
│   ├── mqtt_manager.cpp
│   ├── safety_manager.h
│   └── safety_manager.cpp
├── server/
│   ├── app.py
│   ├── mqtt_client.py
│   ├── database.py
│   ├── telemetry.py
│   ├── command_handler.py
│   └── requirements.txt
├── dashboard/
│   ├── templates/
│   └── static/
│       ├── css/
│       ├── js/
│       └── images/
├── config/
├── data/
├── docs/
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   ├── PIN_MAP.md
│   ├── WIRING.md
│   └── TEST_RESULTS.md
├── WORKDONE.md
└── README.md
```

This is a proposed structure, not an existing repository state. Create
only the directories/files needed for the current phase rather than
generating empty scaffolding for everything.

## 12. Rules for future contributors and AI assistants

1.  Treat this document and the latest `WORKDONE.md` as the project
    context.
2.  Do not invent hardware inventory, board revisions, pin mappings,
    voltage ratings, sensor calibration, test results, or completed
    features.
3.  Clearly label information as **confirmed**, **provisional**,
    **proposed**, or **not yet verified**.
4.  If a required fact is missing, ask the user or propose a safe
    verification step. Do not silently guess.
5.  Do not change the selected scope (manual-only, no servo, no
    soil-moisture sensor) without the user's approval.
6.  Keep the MQTT contract consistent between firmware, backend and
    dashboard.
7.  Keep simulated data visibly labelled; never present it as live
    sensor data.
8.  Explain meaningful code and hardware decisions so the user can
    understand and maintain the project.
9.  Make small changes, test them, and record the result before
    proceeding.
10. Update `WORKDONE.md` after each meaningful work session with files
    changed, commands run, test results, issues and the exact next step.
11. Do not claim a test passed unless it was actually run and its result
    observed.
12. Prefer simple, maintainable implementations over unnecessary
    frameworks or features.

------------------------------------------------------------------------

**Immediate next task:** Set up the Python virtual environment in
`C:\ARIA`, install Paho MQTT, and verify that a Python subscriber
receives a message from the already-tested local Mosquitto broker.
