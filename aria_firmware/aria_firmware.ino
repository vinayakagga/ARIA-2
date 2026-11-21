
/*
  ARIA - Complete Rover Firmware
  Board: ESP32 NodeMCU-32S
  Arduino ESP32 Core: 3.x

  Sensors:
  DHT22, BH1750, MQ-135, NEO-6M GPS

  Motor driver:
  L298N

  Communication:
  MQTT / PubSubClient
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <DHT.h>
#include <BH1750.h>
#include <TinyGPSPlus.h>
#include <time.h>

// ==========================================
// 1. NETWORK CONFIGURATION
// ==========================================

const char* WIFI_SSID = "ssid";
const char* WIFI_PASSWORD = "pass";

// Change this to your PC's current local IPv4 address10.112.69.7
IPAddress MQTT_BROKER(10, 112, 69, 198);

const int MQTT_PORT = 1883;

const char* DEVICE_ID = "aria-esp32";

const char* TELEMETRY_TOPIC = "aria/v1/telemetry";
const char* STATUS_TOPIC = "aria/v1/status";
const char* COMMAND_TOPIC = "aria/v1/command";

// ==========================================
// 2. SENSOR PIN CONFIGURATION
// ==========================================

#define DHT_PIN 4
#define DHT_TYPE DHT22

#define MQ135_PIN 34

#define GPS_RX 16
#define GPS_TX 17

DHT dht(DHT_PIN, DHT_TYPE);
BH1750 lightSensor;
TinyGPSPlus gps;

HardwareSerial gpsSerial(2);

// ==========================================
// 3. MOTOR PIN CONFIGURATION
// ==========================================

#define ENA 32
#define IN1 26
#define IN2 25

#define ENB 33
#define IN3 27
#define IN4 14

#define PWM_FREQ 1000
#define PWM_RESOLUTION 8

// Calibrated motor speeds
#define SPEED_ENA 145
#define SPEED_ENB 200

// Stop if no fresh movement command arrives
#define MOTOR_TIMEOUT 500

bool pwmReady = false;
bool motorActive = false;

unsigned long lastMotorCommand = 0;

// ==========================================
// 4. MQTT AND WIFI OBJECTS
// ==========================================

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long lastWiFiAttempt = 0;
unsigned long lastMQTTAttempt = 0;
unsigned long lastTelemetry = 0;
unsigned long lastStatus = 0;

const unsigned long WIFI_RETRY_INTERVAL = 5000;
const unsigned long MQTT_RETRY_INTERVAL = 3000;
const unsigned long TELEMETRY_INTERVAL = 2000;
const unsigned long STATUS_INTERVAL = 10000;

// ==========================================
// 5. SENSOR VALUES
// ==========================================

float temperature = NAN;
float humidity = NAN;
float lux = NAN;

int mq135Raw = 0;

// ==========================================
// 6. MOTOR CONTROL FUNCTIONS
// ==========================================

void stopMotors() {

  motorActive = false;

  digitalWrite(IN1, LOW);
  digitalWrite(IN2, LOW);

  digitalWrite(IN3, LOW);
  digitalWrite(IN4, LOW);

  if (pwmReady) {
    ledcWrite(ENA, 0);
    ledcWrite(ENB, 0);
  }
}

void setMotors(int speedA, int speedB) {

  if (!pwmReady) {
    stopMotors();
    return;
  }

  speedA = constrain(speedA, -255, 255);
  speedB = constrain(speedB, -255, 255);

  // Motor A direction
  digitalWrite(IN1, speedA > 0 ? HIGH : LOW);
  digitalWrite(IN2, speedA < 0 ? HIGH : LOW);

  // Motor B direction
  digitalWrite(IN3, speedB > 0 ? HIGH : LOW);
  digitalWrite(IN4, speedB < 0 ? HIGH : LOW);

  ledcWrite(ENA, abs(speedA));
  ledcWrite(ENB, abs(speedB));

  motorActive = (speedA != 0 || speedB != 0);

  // Refresh watchdog timer
  lastMotorCommand = millis();
}

// ==========================================
// 7. MQTT COMMAND HANDLER
// ==========================================

void mqttCallback(char* topic, byte* payload, unsigned int length) {

  if (String(topic) != COMMAND_TOPIC) {
    return;
  }

  // Reject oversized commands
  if (length == 0 || length > 128) {
    stopMotors();
    return;
  }

  char message[129];

  memcpy(message, payload, length);
  message[length] = '\0';

  StaticJsonDocument<128> doc;

  DeserializationError error = deserializeJson(doc, message);

  if (error) {
    stopMotors();
    Serial.println("Invalid MQTT command JSON");
    return;
  }

  const char* command = doc["command"];

  if (command == nullptr) {
    stopMotors();
    return;
  }

  Serial.print("MQTT command: ");
  Serial.println(command);

  if (strcmp(command, "stop") == 0) {

    stopMotors();
    return;
  }

  // Do not accept movement without a ready driver
  if (!pwmReady || WiFi.status() != WL_CONNECTED) {
    stopMotors();
    return;
  }

  if (strcmp(command, "forward") == 0) {

    setMotors(SPEED_ENA, SPEED_ENB);

  }
  else if (strcmp(command, "reverse") == 0) {

    setMotors(-SPEED_ENA, -SPEED_ENB);

  }
  else if (strcmp(command, "left") == 0) {

    setMotors(-SPEED_ENA, SPEED_ENB);

  }
  else if (strcmp(command, "right") == 0) {

    setMotors(SPEED_ENA, -SPEED_ENB);

  }
  else {

    stopMotors();
    Serial.println("Unknown command - motors stopped");
  }
}

// ==========================================
// 8. WIFI CONNECTION
// ==========================================

void connectWiFi() {

  Serial.println("Connecting to WiFi...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  lastWiFiAttempt = millis();
}

// ==========================================
// 9. MQTT CONNECTION
// ==========================================

void connectMQTT() {

  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) {
    return;
  }

  Serial.println("Connecting to MQTT broker...");

  StaticJsonDocument<192> willDoc;

  willDoc["schema_version"] = 1;
  willDoc["device_id"] = DEVICE_ID;
  willDoc["status"] = "offline";

  char willPayload[192];

  serializeJson(willDoc, willPayload, sizeof(willPayload));

  // Retained offline Last Will
  bool connected = mqtt.connect(
    DEVICE_ID,
    STATUS_TOPIC,
    0,
    true,
    willPayload
  );

  if (connected) {

    Serial.println("MQTT connected");

    // Always stop after a new MQTT connection
    stopMotors();

    mqtt.subscribe(COMMAND_TOPIC);

    publishStatus();

  } else {

    Serial.print("MQTT connection failed: ");
    Serial.println(mqtt.state());
  }

  lastMQTTAttempt = millis();
}

// ==========================================
// 10. STATUS PUBLISHING
// ==========================================

void publishStatus() {

  if (!mqtt.connected()) {
    return;
  }

  StaticJsonDocument<256> doc;

  doc["schema_version"] = 1;
  doc["device_id"] = DEVICE_ID;
  doc["status"] = "online";
  doc["ip_address"] = WiFi.localIP().toString();
  doc["wifi_rssi_dbm"] = WiFi.RSSI();
  doc["uptime_s"] = millis() / 1000;

  char payload[256];

  serializeJson(doc, payload, sizeof(payload));

  // Retained device status
  mqtt.publish(STATUS_TOPIC, payload, true);
}

// ==========================================
// 11. TIMESTAMP
// ==========================================

void addTimestamp(JsonDocument& doc) {

  time_t now = time(nullptr);

  // Only publish timestamp when NTP has synchronized
  if (now > 1700000000) {

    struct tm timeInfo;
    gmtime_r(&now, &timeInfo);

    char timestamp[25];

    strftime(
      timestamp,
      sizeof(timestamp),
      "%Y-%m-%dT%H:%M:%SZ",
      &timeInfo
    );

    doc["timestamp"] = timestamp;
  }
}

// ==========================================
// 12. TELEMETRY PUBLISHING
// ==========================================

void publishTelemetry() {

  if (!mqtt.connected()) {
    return;
  }

  StaticJsonDocument<512> doc;

  doc["schema_version"] = 1;
  doc["device_id"] = DEVICE_ID;
  doc["source"] = "real";

  addTimestamp(doc);

  if (isnan(temperature)) {
    doc["temperature_c"] = nullptr;
  } else {
    doc["temperature_c"] = temperature;
  }

  if (isnan(humidity)) {
    doc["humidity_pct"] = nullptr;
  } else {
    doc["humidity_pct"] = humidity;
  }

  if (isnan(lux) || lux < 0) {
    doc["light_lux"] = nullptr;
  } else {
    doc["light_lux"] = lux;
  }

  doc["mq135_raw"] = mq135Raw;

  bool gpsFix =
    gps.location.isValid() &&
    gps.location.age() < 5000;

  JsonObject gpsData = doc.createNestedObject("gps");

  gpsData["fix"] = gpsFix;

  if (gpsFix) {
    gpsData["latitude"] = gps.location.lat();
    gpsData["longitude"] = gps.location.lng();
  } else {
    gpsData["latitude"] = nullptr;
    gpsData["longitude"] = nullptr;
  }

  if (gps.satellites.isValid()) {
    gpsData["satellites"] = gps.satellites.value();
  } else {
    gpsData["satellites"] = nullptr;
  }

  char payload[512];

  serializeJson(doc, payload, sizeof(payload));

  bool success = mqtt.publish(
    TELEMETRY_TOPIC,
    payload,
    false
  );

  if (success) {
    Serial.println("Telemetry published");
  } else {
    Serial.println("Telemetry publish failed");
  }
}

// ==========================================
// 13. SENSOR READING
// ==========================================

void readSensors() {

  temperature = dht.readTemperature();
  humidity = dht.readHumidity();

  lux = lightSensor.readLightLevel();

  mq135Raw = analogRead(MQ135_PIN);

  Serial.println("\n---------- SENSOR READINGS ----------");

  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("DHT22: ERROR");
  } else {
    Serial.printf("Temperature: %.1f C\n", temperature);
    Serial.printf("Humidity: %.1f %%\n", humidity);
  }

  if (isnan(lux) || lux < 0) {
    Serial.println("BH1750: ERROR");
  } else {
    Serial.printf("Light: %.1f lux\n", lux);
  }

  Serial.printf("MQ135 raw ADC: %d / 4095\n", mq135Raw);

  bool gpsFix =
    gps.location.isValid() &&
    gps.location.age() < 5000;

  if (gpsFix) {

    Serial.printf(
      "GPS Latitude: %.6f\n",
      gps.location.lat()
    );

    Serial.printf(
      "GPS Longitude: %.6f\n",
      gps.location.lng()
    );

  } else {

    Serial.println("GPS: Waiting for location fix");
  }

  Serial.printf(
    "GPS Satellites: %lu\n",
    gps.satellites.isValid()
      ? gps.satellites.value()
      : 0
  );

  Serial.println("-------------------------------------");
}

// ==========================================
// 14. SETUP
// ==========================================

void setup() {

  Serial.begin(115200);

  // Initialize motor pins
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);

  digitalWrite(IN1, LOW);
  digitalWrite(IN2, LOW);
  digitalWrite(IN3, LOW);
  digitalWrite(IN4, LOW);

  bool pwmA = ledcAttach(
    ENA,
    PWM_FREQ,
    PWM_RESOLUTION
  );

  bool pwmB = ledcAttach(
    ENB,
    PWM_FREQ,
    PWM_RESOLUTION
  );

  pwmReady = pwmA && pwmB;

  stopMotors();

  if (!pwmReady) {
    Serial.println("PWM initialization failed!");
  }

  // Initialize sensors
  dht.begin();

  Wire.begin(21, 22);

  if (lightSensor.begin(BH1750::CONTINUOUS_HIGH_RES_MODE)) {
    Serial.println("BH1750 detected");
  } else {
    Serial.println("BH1750 NOT detected");
  }

  // GPS - confirmed working baud rate
  gpsSerial.begin(
    115200,
    SERIAL_8N1,
    GPS_RX,
    GPS_TX
  );

  // MQ135
  analogReadResolution(12);
  analogSetPinAttenuation(MQ135_PIN, ADC_11db);

  // MQTT configuration
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(5);

  // WiFi
  connectWiFi();

  // NTP time synchronization
  configTime(0, 0, "pool.ntp.org", "time.google.com");

  Serial.println("\n========== ARIA ROVER ==========");
  Serial.println("Sensors initialized");
  Serial.println("Motors stopped at startup");
  Serial.println("Waiting for network...");
}

// ==========================================
// 15. MAIN LOOP
// ==========================================

void loop() {

  unsigned long now = millis();

  // Continuously process GPS
  while (gpsSerial.available()) {
    gps.encode(gpsSerial.read());
  }

  // WiFi connection management
  if (WiFi.status() != WL_CONNECTED) {

    stopMotors();

    if (now - lastWiFiAttempt >= WIFI_RETRY_INTERVAL) {
      connectWiFi();
    }

  } else {

    // MQTT connection management
    if (!mqtt.connected()) {

      stopMotors();

      if (now - lastMQTTAttempt >= MQTT_RETRY_INTERVAL) {
        connectMQTT();
      }

    } else {

      mqtt.loop();
    }
  }

  // Motor safety timeout
  // Use a fresh timestamp AFTER mqtt.loop()
  // because the MQTT callback may have updated lastMotorCommand.
  unsigned long motorNow = millis();

  if (
    motorActive &&
    motorNow - lastMotorCommand > MOTOR_TIMEOUT
  ) {
    stopMotors();
    Serial.println("Motor timeout - STOP");
  }

  // Sensor reading and telemetry
  if (now - lastTelemetry >= TELEMETRY_INTERVAL) {

    lastTelemetry = now;

    readSensors();
    publishTelemetry();
  }

  // Periodic status
  if (
    mqtt.connected() &&
    now - lastStatus >= STATUS_INTERVAL
  ) {

    lastStatus = now;
    publishStatus();
  }
}
