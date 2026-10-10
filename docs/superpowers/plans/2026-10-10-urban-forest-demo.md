# Urban / Forest Drone Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a browser demo with switchable urban and forest 3D drone environments, manual/automatic flight, YOLO-style live detections, and event-driven Nemotron reports.

**Architecture:** A static browser application under `tools/mission_demo/` uses the repository's local Three.js build. Focused JavaScript modules own flight input, world state, and report state. Blender MCP creates optimized GLB scene assets; the browser keeps a procedural fallback so the demo still runs when an asset or Orin endpoint is unavailable.

**Tech Stack:** HTML, CSS, JavaScript, Three.js r128, Blender Python/glTF export, pytest contract tests.

---

## File structure

- `tools/mission_demo/index.html`: accessible application shell and control surface.
- `tools/mission_demo/styles.css`: Field Command visual system and responsive 55:45 layout.
- `tools/mission_demo/app.js`: application bootstrap, render loop, scenario and mode coordination.
- `tools/mission_demo/world.js`: Three.js renderer, drone camera, urban/forest world construction, target projection.
- `tools/mission_demo/input.js`: keyboard, Web Serial IMU, and automatic waypoint input adapters.
- `tools/mission_demo/analysis.js`: mock YOLO tracks, event detection, asynchronous report state, Orin-ready boundary.
- `tools/mission_demo/assets/urban.glb`: Blender-authored urban environment.
- `tools/mission_demo/assets/forest.glb`: Blender-authored forest environment.
- `tests/test_mission_demo.py`: static asset and UI contract tests.
- `README.md`: local demo launch instructions.

### Task 1: Lock the browser contract with failing tests

**Files:**
- Create: `tests/test_mission_demo.py`

- [ ] Write tests that assert the page, module files, local Three.js dependency, scenario controls, input-mode controls, live detections, report panel, and Blender assets exist.

```python
from pathlib import Path

ROOT = Path(__file__).parents[1]
DEMO = ROOT / "tools" / "mission_demo"


def test_demo_shell_contract():
    html = (DEMO / "index.html").read_text()
    for token in ("scenario-urban", "scenario-forest", "mode-imu", "mode-keyboard", "mode-auto",
                  "detection-list", "report-content", "request-analysis"):
        assert f'id="{token}"' in html


def test_demo_assets_are_glb_files():
    for name in ("urban.glb", "forest.glb"):
        data = (DEMO / "assets" / name).read_bytes()
        assert data[:4] == b"glTF"
```

- [ ] Run `uv run pytest tests/test_mission_demo.py -q`; expect failure because the demo files do not exist.

### Task 2: Build the Field Command shell

**Files:**
- Create: `tools/mission_demo/index.html`
- Create: `tools/mission_demo/styles.css`

- [ ] Add a semantic shell with top status bar, 55:45 viewport/report grid, scenario and flight-mode buttons, HUD, `LIVE DETECTION`, `SITUATION REPORT`, loading/error states, and keyboard help.
- [ ] Implement the selected charcoal/olive/lime visual system with amber warnings, red failures, scanline/noise texture, tabular telemetry, and a responsive stacked layout below 900 px.
- [ ] Run `uv run pytest tests/test_mission_demo.py::test_demo_shell_contract -q`; expect pass.

### Task 3: Implement flight and input adapters

**Files:**
- Create: `tools/mission_demo/input.js`

- [ ] Implement `KeyboardInput`, `SerialImuInput`, and `AutoPilotInput`, each returning the same object:

```javascript
{ roll: 0, pitch: 0, yaw: 0, throttle: 0, source: "keyboard", timestamp: performance.now() }
```

- [ ] Parse `G,<seq>,<roll>,<pitch>,<yaw_rate>,<flex0>,<flex1>` serial lines, clamp values, and mark the source stale after 500 ms.
- [ ] Let WASD move, QE yaw, RF climb/descend, and any keyboard flight input take over from AUTO.
- [ ] Implement scenario-specific looping waypoints for AUTO mode.

### Task 4: Create Blender scene assets

**Files:**
- Create: `tools/mission_demo/assets/urban.glb`
- Create: `tools/mission_demo/assets/forest.glb`

- [ ] Use Blender MCP Python in small calls to clear the scene, create shared low-poly materials, and build named collections `UrbanEnvironment` and `ForestEnvironment`.
- [ ] Build the urban asset from a two-block street grid, 6–10 varied buildings, alley, barriers, lamps, road markings, two vehicles, and two human silhouettes.
- [ ] Build the forest asset from rolling terrain, a winding dirt road, instanced low-poly trees, rocks, shrubs, one vehicle, and one partially concealed human silhouette.
- [ ] Export each collection with `bpy.ops.export_scene.gltf(..., export_format='GLB', use_selection=True)` to the exact asset paths.
- [ ] Verify each file begins with `glTF` and is under 15 MB.

### Task 5: Render the drone world

**Files:**
- Create: `tools/mission_demo/world.js`
- Create: `tools/mission_demo/app.js`

- [ ] Initialize Three.js with a perspective drone camera, capped pixel ratio, hemisphere/directional lights, fog, and shadow quality toggle.
- [ ] Load the selected GLB when available and fall back to procedural box/cone geometry on load failure.
- [ ] Apply roll, pitch, yaw, and throttle to a damped drone velocity model with altitude and world bounds.
- [ ] Project target world positions into screen coordinates for stable YOLO-style boxes and hide boxes behind the camera.
- [ ] Switch scenarios by resetting flight, tracks, report state, fog, and environment atomically.

### Task 6: Implement detections and Nemotron-style reports

**Files:**
- Create: `tools/mission_demo/analysis.js`
- Modify: `tools/mission_demo/app.js`

- [ ] Emit stable demo tracks (`T-03`, `T-07`, `T-11`) from scenario target positions and update visible boxes every animation frame.
- [ ] Trigger a report only for a new stable track, risk-zone entry, meaningful relationship change, or the `다시 분석` button.
- [ ] Keep one report request active, retain the latest pending state, and enforce an 8-second cooldown.
- [ ] Render Korean observation/relationship/re-observation sentences with cited track IDs and grounded status.
- [ ] Expose the boundary `requestReport(state)` so the mock implementation can later be replaced by the Orin endpoint without UI changes.

### Task 7: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `tests/test_mission_demo.py`

- [ ] Document `python3 -m http.server 8000` and `http://localhost:8000/tools/mission_demo/`, keyboard controls, IMU connection, AUTO fallback, and mock/Orin status meanings.
- [ ] Add tests that reject remote CDN scripts, check required modules, validate GLB magic bytes, and confirm README commands reference the real path.
- [ ] Run `uv run pytest tests/test_mission_demo.py -q`; expect all demo tests to pass.
- [ ] Run `uv run pytest -q`; expect the non-live repository suite to pass.
- [ ] Open the demo in a browser, switch both scenarios and all three flight modes, request a report, and capture a visual verification screenshot.
