// I2C diagnostics for the NU-40 DK Qwiic bus (SDA=P0.26, SCL=P0.27).
// Every 3 s prints:
//   1) hardware (TWIM) scan at 100 kHz and 400 kHz, with ID registers of each device found
//      (reg 0x75 = MPU6050-family WHO_AM_I, reg 0x0F = ST LSM6DS-family WHO_AM_I)
//      and the raw error code of a 1-byte write to 0x68/0x69 (0 = ACK, 2 = address NACK)
//   2) a bit-banged scan at ~10 kHz that does not use the Wire driver at all,
//      plus the idle level of SDA/SCL (both should be 1)
// Run it once with the sensor plugged in and once unplugged and compare.
#include <Wire.h>

const uint8_t SDA_PIN = PIN_WIRE_SDA;
const uint8_t SCL_PIN = PIN_WIRE_SCL;

static int readReg(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

static void printHex(int v) {
  if (v < 0) { Serial.print("--"); return; }
  if (v < 0x10) Serial.print('0');
  Serial.print(v, HEX);
}

static void twimScan(uint32_t hz) {
  Wire.begin();
  Wire.setClock(hz);
  Serial.print("TWIM "); Serial.print(hz / 1000); Serial.print(" kHz:");
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() != 0) continue;
    Serial.print(" 0x"); Serial.print(a, HEX);
    Serial.print("[75="); printHex(readReg(a, 0x75));
    Serial.print(",0F="); printHex(readReg(a, 0x0F)); Serial.print(']');
    n++;
  }
  if (!n) Serial.print(" none");
  for (uint8_t a = 0x68; a <= 0x69; a++) {
    Wire.beginTransmission(a);
    Wire.write(0x75);
    Serial.print(" | 0x"); Serial.print(a, HEX);
    Serial.print(" err="); Serial.print(Wire.endTransmission());
  }
  Serial.println();
  Wire.end();
}

// ---- bit-banged open-drain I2C: drive low or release to the external pull-ups ----
static void halfBit() { delayMicroseconds(50); }
static void release(uint8_t p) { pinMode(p, INPUT); }
static void pullLow(uint8_t p) { digitalWrite(p, LOW); pinMode(p, OUTPUT); }

static void sclHigh() {
  release(SCL_PIN);
  for (int i = 0; i < 1000 && !digitalRead(SCL_PIN); i++) delayMicroseconds(10);  // clock stretching
  halfBit();
}

static void bbStart() { release(SDA_PIN); sclHigh(); pullLow(SDA_PIN); halfBit(); pullLow(SCL_PIN); halfBit(); }
static void bbStop()  { pullLow(SDA_PIN); halfBit(); sclHigh(); release(SDA_PIN); halfBit(); }

static bool bbWriteByte(uint8_t b) {
  for (int i = 7; i >= 0; i--) {
    if (b & (1 << i)) release(SDA_PIN); else pullLow(SDA_PIN);
    halfBit();
    sclHigh();
    pullLow(SCL_PIN);
  }
  release(SDA_PIN);
  halfBit();
  sclHigh();
  bool ack = digitalRead(SDA_PIN) == LOW;
  pullLow(SCL_PIN);
  halfBit();
  return ack;
}

static void bitbangScan() {
  release(SDA_PIN); release(SCL_PIN);
  delay(1);
  Serial.print("bit-bang 10 kHz: idle SDA="); Serial.print(digitalRead(SDA_PIN));
  Serial.print(" SCL="); Serial.print(digitalRead(SCL_PIN));
  Serial.print(", found:");
  int n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) {
    bbStart();
    bool ack = bbWriteByte(a << 1);
    bbStop();
    if (ack) { Serial.print(" 0x"); Serial.print(a, HEX); n++; }
  }
  Serial.println(n ? "" : " none");
  release(SDA_PIN); release(SCL_PIN);
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);
}

void loop() {
  Serial.println("---- I2C diag ----");
  twimScan(100000);
  twimScan(400000);
  bitbangScan();
  delay(3000);
}
