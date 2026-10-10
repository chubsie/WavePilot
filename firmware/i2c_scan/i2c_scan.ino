// I2C scanner for NU-40 DK: tries the board's I2C pin pairs and prints every device found.
// MPU6050 should show up at 0x68 (or 0x69). Arduino pin number = P0.x -> x, P1.x -> 32+x.
#include <Wire.h>

struct PinPair { uint8_t sda, scl; const char *name; };
const PinPair pairs[] = {
  {26, 27, "P0.26/P0.27 (default Wire)"},
  {20, 21, "P0.20/P0.21"},
  {45, 46, "P1.13/P1.14"},
};

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);
}

void loop() {
  Serial.println("---- scan ----");
  for (const PinPair &p : pairs) {
    Wire.end();
    Wire.setPins(p.sda, p.scl);
    Wire.begin();
    Serial.print(p.name); Serial.print(": ");
    int found = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) {
        Serial.print("0x"); Serial.print(addr, HEX); Serial.print(' ');
        found++;
      }
    }
    Serial.println(found ? "" : "nothing");
  }
  delay(2000);
}
