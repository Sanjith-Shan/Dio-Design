/*
 * Dio Controller — Arduino Sketch for UNO Q MCU (STM32U585 / Zephyr)
 *
 * Reads:
 *   - MPU-6500 IMU via I2C (orientation for pointing)
 *   - KY-023 joystick via analog (proportional scaling)
 *   - 3x momentary buttons (pick, undo, redo)
 *   - 4x LEDs (mode indicators)
 *
 * Outputs sensor data as JSON over Serial to the Linux side,
 * which forwards it via UDP to the Hub server.
 *
 * Wiring:
 *   MPU-6500:  SDA → A4,  SCL → A5,  VCC → 3.3V,  GND → GND
 *   Joystick:  VRx → A0,  VRy → A1,  SW → D2,     +5V → 5V,  GND → GND
 *   Buttons:   Pick → D4,  Undo → D5,  Redo → D6   (INPUT_PULLUP, wire to GND)
 *   LEDs:      D8 (red), D9 (green), D10 (blue), D11 (yellow)  (via 220Ω to GND)
 */

#include <Wire.h>

// ─── Pin Definitions ─────────────────────────────────────────────────

#define JOY_X_PIN   A0
#define JOY_Y_PIN   A1
#define JOY_BTN_PIN 2

#define BTN_PICK    4
#define BTN_UNDO    5
#define BTN_REDO    6

#define LED_RED     8
#define LED_GREEN   9
#define LED_BLUE    10
#define LED_YELLOW  11

// ─── MPU-6500 Registers ──────────────────────────────────────────────

#define MPU_ADDR    0x68
#define REG_PWR_MGMT_1  0x6B
#define REG_ACCEL_XOUT  0x3B
#define REG_GYRO_XOUT   0x43

// ─── Config ──────────────────────────────────────────────────────────

#define SERIAL_BAUD     115200
#define SEND_INTERVAL   20      // ms between sends (50 Hz)
#define JOY_DEADZONE    30      // analog units from center (512)
#define DEBOUNCE_MS     50      // button debounce

// ─── State ───────────────────────────────────────────────────────────

float accel[3] = {0, 0, 0};    // ax, ay, az (g)
float gyro[3]  = {0, 0, 0};    // gx, gy, gz (deg/s)
float joyX = 0, joyY = 0;      // -1.0 to 1.0
bool joyBtn = false;
bool pickBtn = false, undoBtn = false, redoBtn = false;

unsigned long lastSendTime = 0;
unsigned long lastPickTime = 0, lastUndoTime = 0, lastRedoTime = 0;
bool lastPick = false, lastUndo = false, lastRedo = false;

// ─── Setup ───────────────────────────────────────────────────────────

void setup() {
  Serial.begin(SERIAL_BAUD);

  // I2C for IMU
  Wire.begin();

  // Initialize MPU-6500
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR_MGMT_1);
  Wire.write(0x00);  // Wake up
  Wire.endTransmission(true);
  delay(100);

  // Joystick button
  pinMode(JOY_BTN_PIN, INPUT_PULLUP);

  // Action buttons (pull-up, active LOW)
  pinMode(BTN_PICK, INPUT_PULLUP);
  pinMode(BTN_UNDO, INPUT_PULLUP);
  pinMode(BTN_REDO, INPUT_PULLUP);

  // LEDs
  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  pinMode(LED_YELLOW, OUTPUT);

  // Startup indicator
  digitalWrite(LED_GREEN, HIGH);
  delay(200);
  digitalWrite(LED_GREEN, LOW);

  Serial.println("{\"type\":\"boot\",\"device\":\"dio-controller\"}");
}

// ─── Read MPU-6500 ───────────────────────────────────────────────────

void readIMU() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)14, (uint8_t)true);

  int16_t rawAx = (Wire.read() << 8) | Wire.read();
  int16_t rawAy = (Wire.read() << 8) | Wire.read();
  int16_t rawAz = (Wire.read() << 8) | Wire.read();
  Wire.read(); Wire.read(); // Temperature (skip)
  int16_t rawGx = (Wire.read() << 8) | Wire.read();
  int16_t rawGy = (Wire.read() << 8) | Wire.read();
  int16_t rawGz = (Wire.read() << 8) | Wire.read();

  // Convert to g and deg/s (default sensitivity)
  accel[0] = rawAx / 16384.0;
  accel[1] = rawAy / 16384.0;
  accel[2] = rawAz / 16384.0;
  gyro[0]  = rawGx / 131.0;
  gyro[1]  = rawGy / 131.0;
  gyro[2]  = rawGz / 131.0;
}

// ─── Read Joystick ───────────────────────────────────────────────────

void readJoystick() {
  int rawX = analogRead(JOY_X_PIN);
  int rawY = analogRead(JOY_Y_PIN);

  // Map to -1.0 to 1.0 with dead zone
  int dx = rawX - 512;
  int dy = rawY - 512;

  joyX = (abs(dx) > JOY_DEADZONE) ? (dx / 512.0) : 0.0;
  joyY = (abs(dy) > JOY_DEADZONE) ? (dy / 512.0) : 0.0;

  // Clamp
  joyX = constrain(joyX, -1.0, 1.0);
  joyY = constrain(joyY, -1.0, 1.0);

  joyBtn = !digitalRead(JOY_BTN_PIN);  // Active LOW
}

// ─── Read Buttons (with debounce) ────────────────────────────────────

void readButtons() {
  unsigned long now = millis();

  bool rawPick = !digitalRead(BTN_PICK);
  bool rawUndo = !digitalRead(BTN_UNDO);
  bool rawRedo = !digitalRead(BTN_REDO);

  // Debounce: only register on rising edge
  if (rawPick && !lastPick && (now - lastPickTime > DEBOUNCE_MS)) {
    pickBtn = true;
    lastPickTime = now;
  } else {
    pickBtn = false;
  }

  if (rawUndo && !lastUndo && (now - lastUndoTime > DEBOUNCE_MS)) {
    undoBtn = true;
    lastUndoTime = now;
  } else {
    undoBtn = false;
  }

  if (rawRedo && !lastRedo && (now - lastRedoTime > DEBOUNCE_MS)) {
    redoBtn = true;
    lastRedoTime = now;
  } else {
    redoBtn = false;
  }

  lastPick = rawPick;
  lastUndo = rawUndo;
  lastRedo = rawRedo;
}

// ─── Update LEDs ─────────────────────────────────────────────────────

void updateLEDs() {
  // Green = connected / active
  digitalWrite(LED_GREEN, HIGH);

  // Red = pick mode active (button held)
  digitalWrite(LED_RED, !digitalRead(BTN_PICK) ? HIGH : LOW);

  // Blue = joystick active
  digitalWrite(LED_BLUE, (abs(joyX) > 0.1 || abs(joyY) > 0.1) ? HIGH : LOW);

  // Yellow = undo/redo flash
  digitalWrite(LED_YELLOW, (undoBtn || redoBtn) ? HIGH : LOW);
}

// ─── Send Data ───────────────────────────────────────────────────────

void sendData() {
  // JSON format for the Linux-side UDP sender to parse
  Serial.print("{\"type\":\"controller\"");

  // Joystick
  Serial.print(",\"joy_x\":");
  Serial.print(joyX, 3);
  Serial.print(",\"joy_y\":");
  Serial.print(joyY, 3);

  // IMU
  Serial.print(",\"imu\":[");
  Serial.print(accel[0], 3); Serial.print(",");
  Serial.print(accel[1], 3); Serial.print(",");
  Serial.print(accel[2], 3); Serial.print(",");
  Serial.print(gyro[0], 2); Serial.print(",");
  Serial.print(gyro[1], 2); Serial.print(",");
  Serial.print(gyro[2], 2);
  Serial.print("]");

  // Buttons (only send on press events)
  Serial.print(",\"buttons\":{");
  Serial.print("\"pick\":");    Serial.print(pickBtn ? "true" : "false");
  Serial.print(",\"undo\":");   Serial.print(undoBtn ? "true" : "false");
  Serial.print(",\"redo\":");   Serial.print(redoBtn ? "true" : "false");
  Serial.print(",\"joy_btn\":"); Serial.print(joyBtn ? "true" : "false");
  Serial.print("}");

  Serial.println("}");
}

// ─── Loop ────────────────────────────────────────────────────────────

void loop() {
  unsigned long now = millis();

  readIMU();
  readJoystick();
  readButtons();
  updateLEDs();

  if (now - lastSendTime >= SEND_INTERVAL) {
    sendData();
    lastSendTime = now;
  }
}
