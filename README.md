# WavePilot

A glove controller for drones: hand tilt and rotation, read by an IMU on the back of the hand, fly a quadcopter in the [Colosseum](https://github.com/CodexLabsLLC/Colosseum) simulator (an AirSim fork). Built for the D4D hackathon.

## Hardware

| Part | Notes |
|---|---|
| [NUCODE NU-40 DK](https://nucode.gitbook.io/nuseries/dks/nu-40-dk) | nRF52840 (BLE), USB-C |
| [NUCODE NU-IMU](https://nucode.gitbook.io/nuseries/devices/nu-imu) | ST LSM6DS3TR-C 6-axis IMU, I2C address `0x6A` |

Connect the NU-IMU to the DK's Qwiic port (SDA = P0.26, SCL = P0.27). The firmware also accepts an MPU6050-family IMU at `0x68`/`0x69`.

## Repository layout

```
firmware/glove/       Main glove firmware: IMU -> roll/pitch/yaw rate over USB serial
firmware/i2c_diag/    I2C bring-up tool: scans the bus three ways and reads ID registers
firmware/i2c_scan/    Minimal I2C scanner
tools/visualizer/     Browser 3D visualizer with terrain and a flight preview
```

## Getting started

### 1. Flash the firmware

1. Install the [Arduino IDE](https://www.arduino.cc/en/software).
2. In **File → Preferences → Additional boards manager URLs**, add:
   `https://raw.githubusercontent.com/Nucode01/Adafruit_nRF52_Arduino/refs/heads/master/package_nuduino_index.json`
3. In **Boards Manager**, install **NUBoards nRF52**, then select **NU40DK nRF52840**.
4. Open `firmware/glove/glove.ino` and click **Upload**. If no port appears, double-click the S5 button on the DK.
5. Leave the glove still for about 1 s after it boots while the gyro calibrates.

Compile warnings saying `"USE_TINYUSB" redefined` come from the board package and are harmless.

### 2. Run the visualizer

1. Close the Arduino Serial Monitor. Only one program can use the COM port at a time.
2. Open `tools/visualizer/index.html` in **Chrome or Edge**. It needs Web Serial, which Firefox doesn't support.
3. Click **Connect**, hold your hand level, then click **Zero pose**.
4. To fly over the terrain, tick **Fly with tilt**. Tilt forward to fly forward, tilt sideways to strafe, and rotate your wrist to turn.

## Serial protocol

The glove sends one line per sample at 50 Hz over USB serial at 115200 baud:

```
G,<seq>,<roll_deg>,<pitch_deg>,<yaw_rate_dps>,<flex0>,<flex1>
```

- Signs follow the drone frame: `+roll` = right side down, `+pitch` = nose up, `+yaw rate` = turning right.
- `flex0`/`flex1` are reserved for flex sensors and are `0` when unused.
- Lines starting with `INFO,` or `ERR,` are status messages.

Commands you can send to the glove:

| Key | Action |
|---|---|
| `c` | Zero pose: the current hand orientation becomes level |
| `g` | Re-calibrate the gyro (hold still for about 1 s) |
| `i` | Print which IMU was found and an accelerometer check |

At rest, yaw drift measures about 0.1° per 30 s. While the glove is still, the firmware keeps correcting its gyro zero-point.

## Status

- [x] IMU bring-up, sensor fusion and serial streaming
- [x] 3D visualizer with terrain and flight preview
- [ ] Throttle and arm/land input (flex sensor or buttons)
- [ ] Wireless link over BLE
- [ ] Python bridge to Colosseum

## Third-party code

`tools/visualizer/three.min.js` is [three.js](https://threejs.org) r128, MIT license.
