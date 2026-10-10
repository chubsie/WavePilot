// Drone glove firmware: 6-axis IMU (I2C) -> roll/pitch (complementary filter) -> USB serial.
// Supported IMUs (auto-detected):
//   - MPU6050 / MPU6500 family at 0x68/0x69 (WHO_AM_I reg 0x75)
//   - ST LSM6DS3 / LSM6DS3TR-C / LSM6DSL / LSM6DSO at 0x6A/0x6B (WHO_AM_I reg 0x0F)
//     -> the NUCODE NU-IMU (LSM6DS3TR-C, Qwiic, default 0x6A) is this one
// Output, 50 Hz, one line per sample:
//   G,<seq>,<roll_deg>,<pitch_deg>,<yaw_rate_dps>,<flex0>,<flex1>,<buttons>
// buttons is a bitmask, 1 = pressed: bit0 = middle finger (P0.10), bit1 = thumb (P0.21).
// Sign convention (drone frame): +roll = right side down, +pitch = nose up, +yaw rate = nose right.
// flex values are raw ADC readings (0 if the pin is disabled).
//
// The same lines go out over USB serial and over Bluetooth LE (Nordic UART Service, device
// name "WavePilot Glove"). Commands are accepted from either: 'c' re-zeros the neutral hand
// pose, 'g' re-calibrates the gyro (hold still ~1 s), 'i' prints IMU info.

#include <Wire.h>
#include <bluefruit.h>

// ---- Board settings: NU-40 DK (nRF52840), BSP "NU40DK nRF52840" ----
// Default Wire pins are SDA=P0.26, SCL=P0.27 (variant.h), which is the Qwiic port.
// Serial is USB CDC (TinyUSB) on the USB-C port.
#define SERIAL_BAUD 115200
#define I2C_SDA     -1      // -1 = board default Wire pins
#define I2C_SCL     -1
#define FLEX0_PIN   A1      // P0.03; flex sensor divider junction. -1 = unused. Avoid A0 (P0.02 = VBAT sense)
#define FLEX1_PIN   -1      // e.g. A2 (P0.04)
#define BTN_MIDDLE_PIN 10   // P0.10 = NFC2 pin; made a normal GPIO by freeNfcPins() below. Switch to GND, internal pull-up
#define BTN_THUMB_PIN  21   // P0.21, switch to GND, internal pull-up
// Mounting: flip these if the glove moves the drone the wrong way (verified with NU-IMU on NU-40 DK).
#define ROLL_SIGN   -1
#define PITCH_SIGN  1
#define YAW_SIGN    -1
// --------------------------------------------------------------

#define SAMPLE_MS   20      // 50 Hz
#define ALPHA       0.96f   // gyro weight in complementary filter

// Gyro bias tracking: while the glove is still, slowly pull the bias toward the raw reading,
// so warm-up drift and a bad power-up calibration don't make yaw creep.
#define STILL_DPS      2.0f   // max raw-gyro jitter around its running mean to count as still
#define STILL_SAMPLES  50     // 1 s of stillness before tracking starts
#define BIAS_ALPHA     0.02f  // ~1 s time constant at 50 Hz
#define BIAS_MAX_STEP  3.0f   // only absorb small offsets, so slow deliberate turns are not eaten

BLEUart bleuart;   // Nordic UART Service: notifications carry the stream, writes carry commands

// Sends one chunk at a time so a line never exceeds the negotiated BLE payload (MTU - 3).
static void sendBle(const char *b, size_t n) {
  if (!Bluefruit.connected()) return;
  BLEConnection *conn = Bluefruit.Connection(Bluefruit.connHandle());
  if (!conn) return;
  size_t chunk = conn->getMtu() - 3;
  for (size_t i = 0; i < n; i += chunk) bleuart.write((const uint8_t *)b + i, min(chunk, n - i));
}

// Everything printed through Out is collected into whole lines and goes to both USB and BLE.
class DualOut : public Print {
  char buf[96];
  size_t n = 0;
  void flushLine() {
    if (!n) return;
    Serial.write((const uint8_t *)buf, n);
    sendBle(buf, n);
    n = 0;
  }
 public:
  using Print::write;
  size_t write(uint8_t c) override {
    buf[n++] = c;
    if (c == '\n' || n >= sizeof(buf)) flushLine();
    return 1;
  }
} Out;

enum ImuType { IMU_NONE, IMU_MPU6050, IMU_LSM6DS };
ImuType imu = IMU_NONE;
uint8_t imuAddr = 0, imuId = 0;

float roll = 0, pitch = 0;
float rollOff = 0, pitchOff = 0;
float gyroBiasX = 0, gyroBiasY = 0, gyroBiasZ = 0;
uint32_t lastUs = 0, lastSampleMs = 0, seq = 0;
uint8_t buttons = 0, buttonsRaw = 0;   // debounced / last raw reading

static int readReg(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

static bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg); Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool readBurst(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

// Finds a supported IMU, configures it, and reports what it found.
static bool detectIMU() {
  for (uint8_t a = 0x68; a <= 0x69; a++) {
    int id = readReg(a, 0x75);
    if (id < 0) continue;
    if (!writeReg(a, 0x6B, 0x00)) continue;   // wake from sleep
    writeReg(a, 0x1A, 0x03);                  // DLPF ~44 Hz
    writeReg(a, 0x1B, 0x08);                  // GYRO_CONFIG: 500 dps; accel stays +-2 g
    imu = IMU_MPU6050; imuAddr = a; imuId = id;
    return true;
  }
  for (uint8_t a = 0x6A; a <= 0x6B; a++) {
    int id = readReg(a, 0x0F);
    if (id != 0x69 && id != 0x6A && id != 0x6B && id != 0x6C) continue;
    writeReg(a, 0x12, 0x44);                  // CTRL3_C: BDU, register auto-increment
    writeReg(a, 0x10, 0x40);                  // CTRL1_XL: 104 Hz, +-2 g
    writeReg(a, 0x11, 0x44);                  // CTRL2_G: 104 Hz, 500 dps (wrist flicks exceed 250)
    imu = IMU_LSM6DS; imuAddr = a; imuId = id;
    return true;
  }
  return false;
}

// Accel in g, gyro in deg/s, in the IMU's own axes.
static bool readIMU(float &ax, float &ay, float &az, float &gx, float &gy, float &gz) {
  uint8_t b[14];
  if (imu == IMU_MPU6050) {
    if (!readBurst(imuAddr, 0x3B, b, 14)) return false;   // big-endian: accel, temp, gyro
    auto be = [&](int i) { return (int16_t)((b[i] << 8) | b[i + 1]); };
    ax = be(0) / 16384.0f;  ay = be(2) / 16384.0f;  az = be(4) / 16384.0f;
    gx = be(8) / 65.5f;     gy = be(10) / 65.5f;    gz = be(12) / 65.5f;
    return true;
  }
  if (imu == IMU_LSM6DS) {
    if (!readBurst(imuAddr, 0x22, b, 12)) return false;   // little-endian: gyro, accel
    auto le = [&](int i) { return (int16_t)((b[i + 1] << 8) | b[i]); };
    gx = le(0) * 0.0175f;   gy = le(2) * 0.0175f;   gz = le(4) * 0.0175f;
    ax = le(6) * 0.000061f; ay = le(8) * 0.000061f; az = le(10) * 0.000061f;
    return true;
  }
  return false;
}

// Prints which IMU is in use and the current accel magnitude (~1.00 g at rest).
static void printInfo() {
  Out.print(imu == IMU_MPU6050 ? "INFO,IMU MPU6050-family" : "INFO,IMU LSM6DS-family");
  Out.print(" at 0x"); Out.print(imuAddr, HEX);
  Out.print(" WHO_AM_I=0x"); Out.println(imuId, HEX);
  float ax, ay, az, gx, gy, gz;
  if (readIMU(ax, ay, az, gx, gy, gz)) {
    Out.print("INFO,accel |a|="); Out.print(sqrtf(ax * ax + ay * ay + az * az), 2);
    Out.println(" g (expect ~1.00 at rest)");
  }
}

// Lists every address that ACKs, so a wrong address or a dead bus is obvious.
static void reportMissingIMU() {
  Out.print("ERR,no supported IMU found, I2C devices:");
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Out.print(" 0x"); Out.print(a, HEX); n++; }
  }
  Out.println(n ? "" : " none");
}

// Averages the gyro over ~1 s. Retries if the glove moved during the window;
// after 10 tries it keeps the last average and lets trackGyroBias() refine it.
static void calibrateGyro() {
  for (int attempt = 0; attempt < 10; attempt++) {
    float sum[3] = {0, 0, 0}, lo[3] = {1e9f, 1e9f, 1e9f}, hi[3] = {-1e9f, -1e9f, -1e9f};
    int n = 0;
    for (int i = 0; i < 100; i++) {
      float a[3], g[3];
      if (readIMU(a[0], a[1], a[2], g[0], g[1], g[2])) {
        for (int k = 0; k < 3; k++) { sum[k] += g[k]; lo[k] = min(lo[k], g[k]); hi[k] = max(hi[k], g[k]); }
        n++;
      }
      delay(10);
    }
    if (!n) continue;
    gyroBiasX = sum[0] / n; gyroBiasY = sum[1] / n; gyroBiasZ = sum[2] / n;
    float spread = max(hi[0] - lo[0], max(hi[1] - lo[1], hi[2] - lo[2]));
    if (spread < 3.0f) { Out.println("INFO,gyro calibrated"); return; }
    Out.println("INFO,gyro calibration: hold the glove still");
  }
}

// Call with raw (not bias-corrected) gyro every sample.
static void trackGyroBias(float ax, float ay, float az, float gx, float gy, float gz) {
  static float mx = 0, my = 0, mz = 0;
  static int stillCount = 0;
  float amag = sqrtf(ax * ax + ay * ay + az * az);
  bool still = fabsf(gx - mx) < STILL_DPS && fabsf(gy - my) < STILL_DPS && fabsf(gz - mz) < STILL_DPS
            && fabsf(amag - 1.0f) < 0.15f;
  mx += 0.1f * (gx - mx); my += 0.1f * (gy - my); mz += 0.1f * (gz - mz);
  stillCount = still ? stillCount + 1 : 0;
  if (stillCount < STILL_SAMPLES) return;
  if (fabsf(gx - gyroBiasX) > BIAS_MAX_STEP || fabsf(gy - gyroBiasY) > BIAS_MAX_STEP
      || fabsf(gz - gyroBiasZ) > BIAS_MAX_STEP) return;
  gyroBiasX += BIAS_ALPHA * (gx - gyroBiasX);
  gyroBiasY += BIAS_ALPHA * (gy - gyroBiasY);
  gyroBiasZ += BIAS_ALPHA * (gz - gyroBiasZ);
}

// P0.09/P0.10 are NFC antenna pins until the chip's UICR NFCPINS register says otherwise.
// This clears that bit once; the setting is stored in UICR, survives power cycles and reflashing,
// and can only be undone by a full chip erase with a debugger (e.g. J-Link). The check below makes
// every boot after the first a no-op. The chip must reset once for the change to take effect.
static void freeNfcPins() {
  if ((NRF_UICR->NFCPINS & UICR_NFCPINS_PROTECT_Msk) != (UICR_NFCPINS_PROTECT_NFC << UICR_NFCPINS_PROTECT_Pos)) return;
  NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Wen << NVMC_CONFIG_WEN_Pos;
  while (NRF_NVMC->READY == NVMC_READY_READY_Busy);
  NRF_UICR->NFCPINS &= ~UICR_NFCPINS_PROTECT_Msk;
  while (NRF_NVMC->READY == NVMC_READY_READY_Busy);
  NRF_NVMC->CONFIG = NVMC_CONFIG_WEN_Ren << NVMC_CONFIG_WEN_Pos;
  while (NRF_NVMC->READY == NVMC_READY_READY_Busy);
  delay(100);
  NVIC_SystemReset();
}

// Debounce: a button state only changes after two identical samples in a row (>= 20 ms apart).
static void readButtons() {
  uint8_t raw = 0;
  if (digitalRead(BTN_MIDDLE_PIN) == LOW) raw |= 1;
  if (digitalRead(BTN_THUMB_PIN) == LOW)  raw |= 2;
  if (raw == buttonsRaw) buttons = raw;
  buttonsRaw = raw;
}

// Average 4 reads to knock down ADC noise on the high-impedance divider.
static int readFlex(int pin) {
  int sum = 0;
  for (int i = 0; i < 4; i++) sum += analogRead(pin);
  return sum / 4;
}

static void startAdv() {
  // Connectable + scannable. Windows lists a device by the name in the advertising packet itself,
  // so the name goes there; the 128-bit service UUID (18 bytes) doesn't fit beside it and goes in
  // the scan response.
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addName();
  Bluefruit.ScanResponse.addService(bleuart);
  Bluefruit.Advertising.restartOnDisconnect(true);  // reconnect any time
  Bluefruit.Advertising.setInterval(32, 244);       // 20 ms fast / 152.5 ms slow, units of 0.625 ms
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);                   // advertise until connected
}

void setup() {
  freeNfcPins();                      // one-time: P0.10 as normal GPIO (resets the chip the first time)
  Serial.begin(SERIAL_BAUD);
#if I2C_SDA >= 0 && I2C_SCL >= 0
  Wire.setPins(I2C_SDA, I2C_SCL);   // Adafruit nRF52 core API (Wire.begin has no pin args)
  Wire.begin();
#else
  Wire.begin();
#endif
  Wire.setClock(400000);

  // Bluetooth LE. The blue LED (LED_CONN) lights while connected. Config calls come before begin().
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);     // bigger buffers/MTU for the 50 Hz stream
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName("WavePilot Glove");
  Bluefruit.Periph.setConnIntervalMS(10, 20);       // ask for a fast connection interval
  bleuart.begin();
  startAdv();

  pinMode(BTN_MIDDLE_PIN, INPUT_PULLUP);
  pinMode(BTN_THUMB_PIN, INPUT_PULLUP);

  // Flex sensor divider has a high source impedance (tens of kohm): give the ADC time to settle.
  analogReference(AR_INTERNAL);       // 0..3.6 V range, fits the 3.3 V divider
  analogReadResolution(10);
  analogSampleTime(40);

  delay(100);                         // IMUs need up to ~100 ms after power-up
  while (!detectIMU()) { reportMissingIMU(); delay(1000); }   // retry until a sensor answers
  delay(100);                         // let the first samples settle

  calibrateGyro();                    // keep the glove still at power-up
  printInfo();
  lastUs = micros();
  Out.println("INFO,glove ready");
}

static void handleCommand(int cmd) {
  if (cmd == 'c') { rollOff = roll; pitchOff = pitch; }          // re-zero neutral pose
  else if (cmd == 'g') { calibrateGyro(); lastUs = micros(); }   // hold still ~1 s
  else if (cmd == 'i') printInfo();                              // which IMU, accel sanity check
}

void loop() {
  while (Serial.available()) handleCommand(Serial.read());
  while (bleuart.available()) handleCommand(bleuart.read());

  uint32_t now = millis();
  if (now - lastSampleMs < SAMPLE_MS) return;
  lastSampleMs = now;

  float ax, ay, az, gx, gy, gz;
  if (!readIMU(ax, ay, az, gx, gy, gz)) return;

  uint32_t us = micros();
  float dt = (us - lastUs) * 1e-6f;
  lastUs = us;

  trackGyroBias(ax, ay, az, gx, gy, gz);
  gx -= gyroBiasX; gy -= gyroBiasY; gz -= gyroBiasZ;
  float accRoll  = atan2f(ay, az) * 57.29578f;
  float accPitch = atan2f(-ax, sqrtf(ay * ay + az * az)) * 57.29578f;
  roll  = ALPHA * (roll  + gx * dt) + (1 - ALPHA) * accRoll;
  pitch = ALPHA * (pitch + gy * dt) + (1 - ALPHA) * accPitch;

  readButtons();

  int f0 = FLEX0_PIN >= 0 ? readFlex(FLEX0_PIN) : 0;
  int f1 = FLEX1_PIN >= 0 ? readFlex(FLEX1_PIN) : 0;

  Out.print("G,"); Out.print(seq++);
  Out.print(',');  Out.print(ROLL_SIGN * (roll - rollOff), 1);
  Out.print(',');  Out.print(PITCH_SIGN * (pitch - pitchOff), 1);
  Out.print(',');  Out.print(YAW_SIGN * gz, 1);
  Out.print(',');  Out.print(f0);
  Out.print(',');  Out.print(f1);
  Out.print(',');  Out.println(buttons);
}
