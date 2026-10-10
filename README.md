# WavePilot

A glove controller for drones: hand tilt and rotation, read by an IMU on the back of the hand, fly a quadcopter in a built-in 3D flight simulator that runs in the browser. Built for the D4D hackathon.

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

1. Open `tools/visualizer/index.html` in **Chrome or Edge**. It needs Web Serial and Web Bluetooth, which Firefox doesn't support.
2. Click **Connect USB** (close the Arduino Serial Monitor first, since only one program can use the COM port) or **Connect Bluetooth** and pick "WavePilot Glove".
3. Hold your hand level, then click **Zero pose**.
4. To fly over the terrain, tick **Fly with tilt**. Tilt forward to fly forward, tilt sideways to strafe, and rotate your wrist to turn.

## Controls

| Input | Hardware | Action in the visualizer |
|---|---|---|
| Hand tilt | NU-IMU | Roll and pitch lean the drone; wrist rotation turns it |
| Finger bend | Flex sensor on P0.03 (A1), divider to 3.3 V | Throttle: 0% straight, 50% hovers, 100% fully bent |
| Thumb button | Switch from P0.21 to GND | Toggle Manual / AI mode (AI is a placeholder that circles) |
| Middle-finger button | Switch from P0.10 to GND | Flash the drone's LEDs while held (placeholder) |

Calibrate the flex sensor with **Set flat** and **Set bent** in the visualizer. For testing without the glove, press `M` for the mode and hold `L` for the LEDs.

## Serial protocol

The glove sends one line per sample at 50 Hz, over USB serial at 115200 baud and over Bluetooth LE (Nordic UART Service, device name "WavePilot Glove"). The lines are identical on both links:

```
G,<seq>,<roll_deg>,<pitch_deg>,<yaw_rate_dps>,<flex0>,<flex1>,<buttons>
```

- Signs follow the drone frame: `+roll` = right side down, `+pitch` = nose up, `+yaw rate` = turning right.
- `flex0`/`flex1` are raw 10-bit ADC readings of the flex sensors (`0` when a pin is unused).
- `buttons` is a bitmask, 1 = pressed: bit 0 = middle-finger button, bit 1 = thumb button.
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
- [x] Flex sensor throttle, thumb and middle-finger buttons, Manual / AI mode switch
- [x] Wireless link over BLE
- [ ] Real AI mode (currently a placeholder that circles)
- [ ] Arm / land gesture

## Third-party code

`tools/visualizer/three.min.js` is [three.js](https://threejs.org) r128, MIT license.

## Python: LLM overwatch pipeline

The same repository also holds a set of Python packages for an LLM overwatch drone. A mock detection source and a mock flight controller feed perception tracks; the LLM module turns them into a state JSON, a rule-based decision and an English situation report in which every sentence cites the track IDs it is based on. The drone side sends all of this over a TCP link to an operator console that runs as a separate process.

These packages are independent of `firmware/` and `tools/`. They need Python 3.12 and [uv](https://docs.astral.sh/uv/), and have no runtime dependencies (standard library only; pytest is a dev dependency).

### Package layout

```
modules/
  llm_report/          LLM layer: state JSON, decision model, report (import only the top-level facade)
    interface/         LLMBackend protocol, Completion, backend errors
    deploy/jetson/     llama-server container for Jetson Orin Nano + runbook
    scripts/           Local llama-server launcher and HTTP smoke test
    examples/          Using modules.llm_report on its own
  yolo_detection/      YOLO DetectionSource adapter and run_yolo CLI
    interface/         DetectionSource protocol, Frame, Detection
    deploy/jetson/     YOLO TensorRT runbook for Jetson
  perception/          Pixel -> ground projection, track aggregation
    interface/         Track, camera geometry
  flight_control/      Hovering mock flight controller
    interface/         FlightControl protocol, Telemetry, commands
  comm/                TCP JSON-lines CommLink
    interface/         CommLink protocol, Message envelope
  simulation/          Scenario-driven detection source, sim clock, scenarios/*.toml
  drone/               Drone-side runner that wires everything together
  operator_console/    Operator CLI
scripts/               setup.sh, run_demo.sh
tests/                 pytest
```

The operator package is `modules/operator_console/`, not `operator/`, because `operator` would shadow the Python standard library module of the same name.

### Setup

Run every command in this section from the repository root (the folder that holds `pyproject.toml`).

```bash
uv sync
```

### Tests

| Command | What it runs |
|---|---|
| `uv run pytest` | Unit tests. `e2e` and `llm` tests are excluded by default |
| `uv run pytest -m e2e` | Starts the operator console and the drone runner as two processes, with the fake LLM backend |
| `LLAMA_SERVER_URL=http://127.0.0.1:8080 uv run pytest -m llm` | Tests against a real model; needs a running llama-server (see below) |

### Run the two processes

Start the operator console first. It listens; the drone connects to it.

```bash
# Terminal 1: operator console
uv run python -m modules.operator_console --host 127.0.0.1 --port 8765

# Terminal 2: drone side
uv run python -m modules.drone.run_scenario --scenario modules/simulation/scenarios/checkpoint.toml --operator-host 127.0.0.1 --operator-port 8765 --backend fake
```

The port must be the same in both commands (8765 is the console's default). If the console is not up, the drone retries for `--connect-timeout-sec` seconds and then exits with code 1. Add `--exit-on-disconnect` to the console to make it exit when the drone finishes.

Other scenarios: `modules/simulation/scenarios/road_crossing.toml`, `modules/simulation/scenarios/crowd_near_truck.toml`.

`--speed` sets sim seconds per wall second: `1` (default) is real time, `2` is twice as fast, and `0` runs as fast as possible. With `--speed 0` reports are generated synchronously, so the same scenario always gives the same telemetry, states and reports.

While the console runs, you can type commands into it. Each one is sent to the drone:

| Command | Action |
|---|---|
| `rth` | Return to home |
| `land` | Land |
| `takeoff <alt_m>` | Take off to an altitude in metres |
| `goto <north_m> <east_m> <alt_m>` | Fly to a position (NED metres from home, altitude in metres) |
| `quit` | Close the console |

### Console output

Every console line starts with the local time in ISO 8601 with milliseconds, then a space and a tag:

```
2026-10-10T19:00:20.478+09:00 LISTEN host=127.0.0.1 port=8765
2026-10-10T19:00:20.730+09:00 LINK connected peer=127.0.0.1:64022
2026-10-10T19:00:20.741+09:00 TELEMETRY t=8.0s mode=hover pos_ned=(0.0,0.0,-40.0) alt=40.0m att_deg=(0.0,0.0,0.0) gimbal_deg=(-55.0,0.0) battery=99.8%
2026-10-10T19:00:20.787+09:00 STATE t=40.0s alt=40.0m battery=99.2% tracks=2 ids=T1,T2 counts=car:1,person:1 omitted=0
2026-10-10T19:00:20.787+09:00 DECISION t=40.0s verdict=escalate reason=urgency 2 from T2; ambiguous: person-vehicle distance 15.4 m is within 15+-3 m answers=urgency=2@0.50; next_action=alert_operator@0.50; report_needed=True@0.50
2026-10-10T19:00:20.787+09:00 REPORT t=40.0s sentences=2 grounded=true flags=-
2026-10-10T19:00:20.787+09:00 REPORT   [T1] (observation) 1 car track: T1 at 27.7 m N, 19.3 m E.
2026-10-10T19:00:20.787+09:00 REPORT   [T2] (observation) 1 person track: T2 at 21.8 m N, 5.1 m E.
2026-10-10T19:00:20.787+09:00 LINK disconnected peer=127.0.0.1:64022 last_received_at=2026-10-10T10:00:20.787+00:00
```

| Tag | Meaning |
|---|---|
| `LISTEN` | The console is listening; shows the host and the actual port |
| `LINK` | The drone connected or disconnected |
| `TELEMETRY` | Drone sim time, flight mode, NED position, altitude, attitude, gimbal angles and battery |
| `STATE` | Summary of the state JSON: track count, track IDs, counts by class, tracks left out |
| `DECISION` | Rule-based verdict (`act` or `escalate`), the reason, and each typed answer as `question=value@confidence` |
| `REPORT` | First a header line (sentence count, whether every sentence is grounded, flags), then one line per report sentence, or a line with `error=` if the report failed |
| `COMMAND` | A command the console sent to the drone |
| `WARN` | A malformed message, an unknown command or a send failure |

A report sentence line looks like `REPORT   [T1,T3] (observation) <text>`. The brackets list the track IDs the sentence is based on, and `(observation)` or `(suggestion)` is the sentence kind. A track label such as `T3` means tracker ID 3.

### Real LLM on the Mac (llama-server)

The report LLM is Nemotron-3-Nano-4B (GGUF, Q4_K_M) served by llama.cpp's `llama-server`.

```bash
brew install llama.cpp
modules/llm_report/scripts/run_llama_server_mac.sh
```

The script serves the model on `127.0.0.1:8080`; the first run downloads the model from Hugging Face. Once `http://127.0.0.1:8080/health` returns 200, run the drone side with the real backend:

```bash
uv run python -m modules.drone.run_scenario --scenario modules/simulation/scenarios/checkpoint.toml --operator-host 127.0.0.1 --operator-port 8765 --backend llama_server --llm-url http://127.0.0.1:8080
```

To start the console and the drone together with one command, use `scripts/run_demo.sh --backend fake` or `scripts/run_demo.sh --backend llama_server`. For its other options, see the comment at the top of the script.

Model files are not committed (`*.gguf` and `models/` are in `.gitignore`).

### Run a rule_mode mission

`modules.drone.run_mission` flies a whole mission: take off, search, approach and heart the target, return home and land. A TOML mission file sets the search area, the target's upper-body color and the safety limits; without `--mission` the built-in defaults are used. Every mission writes a JSON Lines log to `logs/missions/<start time>.jsonl` and the same events, one line each, to stderr.

```bash
uv run python -m modules.drone.run_mission --sim mock --mission missions/red_heart.toml
uv run python -m modules.drone.run_mission --sim mock --mission missions/red_heart.toml --log-dir logs/missions
uv run --group yolo python -m modules.drone.run_mission --sim browser --mission missions/red_heart.toml
```

With `--sim browser`, open `http://127.0.0.1:8766/drone_camera.html` and press M to switch to AI mode. The panel shows the AI mission state, and its Fault injection buttons cut the operator link, drop or shift GPS, and drain the battery.

### Plugging in a simulator

A simulator such as Colosseum only needs three things from this code:

1. `modules.yolo_detection.interface.DetectionSource`: implement `read_frame() -> Frame | None`. A `Frame` holds `Detection`s whose fields map 1:1 to Ultralytics tracking output: class id and name, confidence, pixel `xyxy` box and track id.
2. `modules.flight_control.interface.FlightControl`: implement `get_telemetry() -> Telemetry` and `send_command(command) -> CommandAck`. Positions are NED metres and angles are degrees; the sign conventions are in `modules/perception/interface/geometry.py` and match the glove's drone frame described above.
3. The top-level `modules.llm_report` facade:

   ```python
   from modules.llm_report import LLMModuleConfig, create_llm_module

   module = create_llm_module(LLMModuleConfig(backend="fake"))
   output = module.process(tracks, telemetry)  # or module.process_state(state_json_dict)
   payload = output.to_report_payload()        # state, decision and report
   ```

   `create_llm_module` returns an `LLMModule`. `process` takes perception tracks and telemetry (`modules.perception.tracking.TrackAggregator` turns frames and telemetry into tracks); `process_state` takes a state JSON directly.

Do not import anything else: no `modules.llm_report` submodules and no implementations from `modules/simulation/` or `modules/comm/`. `tests/test_import_boundaries.py` enforces this.

A standalone example that uses only the facade:

```bash
PYTHONPATH=. uv run python modules/llm_report/examples/use_llm_module.py
```

### Jetson Orin Nano

On the Jetson Orin Nano only `llama-server` runs, in a container; `modules.llm_report` stays on the Mac and talks to it over HTTP at `http://<orin-ip>:8080`. The server has no authentication, so limit access with the bind address and a firewall rule as the runbook describes. You build and run the container yourself; the steps are in [modules/llm_report/deploy/jetson/RUNBOOK.md](modules/llm_report/deploy/jetson/RUNBOOK.md). Switching the drone runner to the remote backend by configuration is Phase 3 scope.
