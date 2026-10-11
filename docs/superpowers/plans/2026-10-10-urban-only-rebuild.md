# Urban-only Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver one reliable, realistic ruined-city scenario with explicit collision, main-equivalent acceleration physics, AUTO target approach, and visible USB/BLE device selection.

**Architecture:** Blender exports a new urban GLB with render meshes separated from named collision proxies. The browser loads one scenario, applies explicit AABBs, integrates acceleration physics, and drives AUTO from stable visible tracks.

**Tech Stack:** Blender MCP, Three.js r128, Web Serial, Web Bluetooth, JavaScript, Node tests, pytest.

---

### Task 1: Rebuild the city asset

- Replace `urban.blend` and `urban.glb` with a coherent street, solid facades, explicit `COLLIDER_` proxies and open target positions.
- Remove mountain assets and all scenario switching code.
- Verify GLB magic, size, proxy count and safe spawn.

### Task 2: Fix collision and acceleration

- Use only named proxy nodes for runtime collision.
- Keep substep movement, wall sliding and retreat.
- Port main's thrust, gravity and drag integration for IMU; use acceleration and drag for keyboard/AUTO.

### Task 3: Finish AUTO observation and device connection

- Select stable visible tracks, approach to 14m, stop on occlusion/collision/loss.
- Open USB/BLE pickers directly from visible buttons and surface connection state.

### Task 4: Verify and deliver

- Run GLB-based path simulation, collision, AUTO and device picker tests.
- Run full pytest and browser smoke verification with zero application errors.
