# Mountain Scenario, Glove Input, and Collision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the jungle with a realistic border-mountain scenario, integrate main's USB/BLE glove controls, and prevent the drone from passing through major obstacles.

**Architecture:** Blender exports a new `mountain.glb` with visible environment meshes and hidden `COLLIDER_` proxies. A pure collision module resolves sphere-vs-AABB movement. `GloveInput` owns both USB and Nordic UART BLE links and converts the extended glove line to the same `FlightInput` consumed by the existing world.

**Tech Stack:** Blender MCP, Three.js r128, Web Serial, Web Bluetooth, JavaScript ES modules, pytest and Node.

---

### Task 1: Lock scenario and collision contracts

**Files:** `tests/test_mission_demo.py`, `tools/mission_demo/collision.js`

- [ ] Add failing tests for `scenario-mountain`, `assets/mountain.glb`, absence of forest UI references, and pure collision behavior.
- [ ] Implement `sphereIntersectsAabb(position, radius, box)` and `resolveMovement(position, desired, radius, boxes)` so blocked movement stops or slides on a free axis.
- [ ] Run `uv run pytest tests/test_mission_demo.py -q`; expect all demo tests to pass.

### Task 2: Replace the jungle with border mountains

**Files:** create `tools/mission_demo/assets/mountain.blend`, `tools/mission_demo/assets/mountain.glb`; remove `forest.blend`, `forest.glb`; modify `environment-manifest.json`, `analysis.js`, `input.js`, `world.js`, `index.html`.

- [ ] Build steep terrain, wet rock ridges, a winding military road, bunker, watch post, tunnel entrance, wire and sparse real pine assets.
- [ ] Add `COLLIDER_Cliff_*`, `COLLIDER_Bunker`, `COLLIDER_Tunnel`, and `COLLIDER_Ground_*` box proxies.
- [ ] Rename scenario keys and UI from forest to mountain while preserving T-11 and T-12 report grounding.
- [ ] Export GLB under 18 MB and verify `glTF` magic bytes.

### Task 3: Integrate main glove input

**Files:** modify `tools/mission_demo/input.js`, `app.js`, `index.html`, `styles.css`; test `tests/test_mission_demo.py`.

- [ ] Replace `SerialImuInput` with `GloveInput` supporting USB and Nordic UART BLE through one line feeder.
- [ ] Parse `G,seq,roll,pitch,yaw_rate,flex0,flex1,buttons`, retain compatibility with shorter lines, integrate yaw by sequence gap, and return normalized roll/pitch/yaw/throttle.
- [ ] Persist flex flat/bent calibration, expose USB/BLE connect and `c/g/i` commands, and emit thumb-button mode toggles on rising edges.
- [ ] Add compact USB, BLE, flat, bent and zero controls to the Field Command dock.

### Task 4: Wire runtime collision and HUD feedback

**Files:** modify `world.js`, `app.js`, `styles.css`.

- [ ] Build AABBs from hidden `COLLIDER_` nodes and urban `Ruin_` wall pieces after GLB load.
- [ ] Resolve every proposed camera movement through `resolveMovement` with radius 0.7m.
- [ ] Show `OBSTACLE / FLIGHT BLOCKED` for 600ms after blocked movement.
- [ ] Verify AUTO routes do not intersect colliders and manual flight cannot pass through walls, bunkers or cliffs.

### Task 5: Verify and document

**Files:** modify `README.md` and tests.

- [ ] Document mountain controls, USB/BLE links, flex calibration, button mapping and collision behavior.
- [ ] Run mission tests, full pytest, JS syntax checks and browser smoke tests.
- [ ] Verify mountain GLB, USB/BLE UI, AUTO, report grounding and collision HUD in Chrome with no application-origin errors.
