// Drone glove firmware: 6-axis IMU (I2C) -> roll/pitch (complementary filter) -> USB serial.
// Supported IMUs (auto-detected):
//   - MPU6050 / MPU6500 family at 0x68/0x69 (WHO_AM_I reg 0x75)
//   - ST LSM6DS3 / LSM6DS3TR-C / LSM6DSL / LSM6DSO at 0x6A/0x6B (WHO_AM_I reg 0x0F)
//     -> the NUCODE NU-IMU (LSM6DS3TR-C, Qwiic, default 0x6A) is this one
// Output, 50 Hz, one line per sample:
//   G,<seq>,<roll_deg>,<pitch_deg>,<yaw_rate_dps>,<flex0>,<flex1>
// Sign convention (drone frame): +roll = right side down, +pitch = nose up, +yaw rate = nose right.
// flex values are raw ADC readings (0 if the pin is disabled).
//
// Serial commands: 'c' re-zeros the neutral hand pose, 'g' re-calibrates the gyro
// (hold still ~1 s), 'i' prints IMU info.

#include <Wire.h>

// ---- Board settings: NU-40 DK (nRF52840), BSP "NU40DK nRF52840" ----
// Default Wire pins are SDA=P0.26, SCL=P0.27 (variant.h), which is the Qwiic port.
// Serial is USB CDC (TinyUSB) on the USB-C port.
#define SERIAL_BAUD 115200
#define I2C_SDA     -1      // -1 = board default Wire pins
#define I2C_SCL     -1
#define FLEX0_PIN   -1      // e.g. A0 (P0.02), A1 (P0.03); -1 = unused
#define FLEX1_PIN   -1
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

enum ImuType { IMU_NONE, IMU_MPU6050, IMU_LSM6DS };
ImuType imu = IMU_NONE;
uint8_t imuAddr = 0, imuId = 0;

float roll = 0, pitch = 0;
float rollOff = 0, pitchOff = 0;
float gyroBiasX = 0, gyroBiasY = 0, gyroBiasZ = 0;
uint32_t lastUs = 0, lastSampleMs = 0, seq = 0;

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
  Serial.print(imu == IMU_MPU6050 ? "INFO,IMU MPU6050-family" : "INFO,IMU LSM6DS-family");
  Serial.print(" at 0x"); Serial.print(imuAddr, HEX);
  Serial.print(" WHO_AM_I=0x"); Serial.println(imuId, HEX);
  float ax, ay, az, gx, gy, gz;
  if (readIMU(ax, ay, az, gx, gy, gz)) {
    Serial.print("INFO,accel |a|="); Serial.print(sqrtf(ax * ax + ay * ay + az * az), 2);
    Serial.println(" g (expect ~1.00 at rest)");
  }
}

// Lists every address that ACKs, so a wrong address or a dead bus is obvious.
static void reportMissingIMU() {
  Serial.print("ERR,no supported IMU found, I2C devices:");
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.print(" 0x"); Serial.print(a, HEX); n++; }
  }
  Serial.println(n ? "" : " none");
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
    if (spread < 3.0f) { Serial.println("INFO,gyro calibrated"); return; }
    Serial.println("INFO,gyro calibration: hold the glove still");
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

void setup() {
  Serial.begin(SERIAL_BAUD);
#if I2C_SDA >= 0 && I2C_SCL >= 0
  Wire.setPins(I2C_SDA, I2C_SCL);   // Adafruit nRF52 core API (Wire.begin has no pin args)
  Wire.begin();
#else
  Wire.begin();
#endif
  Wire.setClock(400000);

  delay(100);                         // IMUs need up to ~100 ms after power-up
  while (!detectIMU()) { reportMissingIMU(); delay(1000); }   // retry until a sensor answers
  delay(100);                         // let the first samples settle

  calibrateGyro();                    // keep the glove still at power-up
  printInfo();
  lastUs = micros();
  Serial.println("INFO,glove ready");
}

void loop() {
  if (Serial.available()) {
    char cmd = Serial.read();
    if (cmd == 'c') { rollOff = roll; pitchOff = pitch; }   // re-zero neutral pose
    else if (cmd == 'g') { calibrateGyro(); lastUs = micros(); }   // hold still ~1 s
    else if (cmd == 'i') printInfo();                       // which IMU, accel sanity check
  }

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

  int f0 = FLEX0_PIN >= 0 ? analogRead(FLEX0_PIN) : 0;
  int f1 = FLEX1_PIN >= 0 ? analogRead(FLEX1_PIN) : 0;

  Serial.print("G,"); Serial.print(seq++);
  Serial.print(',');  Serial.print(ROLL_SIGN * (roll - rollOff), 1);
  Serial.print(',');  Serial.print(PITCH_SIGN * (pitch - pitchOff), 1);
  Serial.print(',');  Serial.print(YAW_SIGN * gz, 1);
  Serial.print(',');  Serial.print(f0);
  Serial.print(',');  Serial.println(f1);
}
