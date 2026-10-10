# Cinematic Environment Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the low-poly urban and forest demo assets with cinematic, realistic tropical-jungle and war-damaged European-city environments while preserving flight, YOLO overlays, and reports.

**Architecture:** Blender MCP authors two optimized GLB scenes from modular mid-poly geometry, PBR image textures, instances, and baked-looking material variation. The browser adds cinematic fog, color grading, lens contamination, and a quality switch; the existing procedural scene remains failure-only fallback.

**Tech Stack:** Blender 5.2 Python API, glTF/GLB, Three.js r128, HTML/CSS/JavaScript, pytest.

---

### Task 1: Add failing realism and quality contracts

**Files:**
- Modify: `tests/test_mission_demo.py`

- [ ] Add tests for a `quality-cinematic` control, `quality-performance` control, `cinematic-effects` overlay, and `assets/environment-manifest.json`.

```python
def test_cinematic_environment_contract():
    html = (DEMO / "index.html").read_text()
    for token in ("quality-cinematic", "quality-performance", "cinematic-effects"):
        assert f'id="{token}"' in html
    manifest = json.loads((DEMO / "assets" / "environment-manifest.json").read_text())
    assert manifest["forest"]["style"] == "humid-tropical-jungle"
    assert manifest["urban"]["style"] == "war-damaged-european-city"
```

- [ ] Run `uv run pytest tests/test_mission_demo.py::test_cinematic_environment_contract -q`; expect failure because the controls and manifest do not exist.

### Task 2: Rebuild the tropical jungle in Blender

**Files:**
- Replace: `tools/mission_demo/assets/forest.blend`
- Replace: `tools/mission_demo/assets/forest.glb`

- [ ] Remove all cone-pine geometry.
- [ ] Create layered tropical vegetation: irregular buttress-root trees, palm crowns, bamboo clusters, broadleaf plants, ferns, vines, canopy cards, shrubs, and wet fallen logs.
- [ ] Create uneven muddy terrain, a winding wet track, puddles, drainage channels, a ruined concrete outpost, sandbags, rusted gate, and trench.
- [ ] Use named PBR-style materials `WetMud`, `WetLeaf`, `TropicalBark`, `MossConcrete`, `RustMetal`, and `PuddleWater` with roughness/metallic/transmission tuned for export.
- [ ] Export the selected scene to `forest.glb`, keep it below 15 MB, and verify it loads through the existing `THREE.GLTFLoader` path.

### Task 3: Rebuild the ruined city in Blender

**Files:**
- Replace: `tools/mission_demo/assets/urban.blend`
- Replace: `tools/mission_demo/assets/urban.glb`

- [ ] Remove intact box towers and create a long European street from damaged modular facades with cornices, window recesses, exposed floors, broken wall silhouettes, soot and shell damage.
- [ ] Add cobblestone, rubble piles, individual bricks, timber beams, twisted metal, sandbags, broken lamps, barricades, and a burned vehicle.
- [ ] Use named PBR-style materials `WetCobble`, `DamagedPlaster`, `OldBrick`, `SootGlass`, `BurnedMetal`, and `RubbleDust`.
- [ ] Preserve target custom properties for `T-03`, `T-07`, and `T-09`, export to `urban.glb`, and keep it below 15 MB.

### Task 4: Add cinematic rendering and performance mode

**Files:**
- Modify: `tools/mission_demo/index.html`
- Modify: `tools/mission_demo/styles.css`
- Modify: `tools/mission_demo/world.js`
- Modify: `tools/mission_demo/app.js`
- Create: `tools/mission_demo/assets/environment-manifest.json`

- [ ] Add a `CINEMATIC / PERFORMANCE` switch and a non-interactive `cinematic-effects` layer.
- [ ] In cinematic mode use pixel ratio up to 1.65, 1024² soft shadows, denser distance fog, film grain, vignette, lens dirt and scene-specific color filters.
- [ ] In performance mode cap pixel ratio at 1, disable shadows, reduce fog density and hide environment nodes marked `detail_high`.
- [ ] Store the selected quality in `localStorage` and keep YOLO target overlays outside the filtered viewport.
- [ ] Write `environment-manifest.json` with scene style, required features, target IDs, and generated object counts.

### Task 5: Verify visual and functional integrity

**Files:**
- Modify: `README.md`
- Modify: `tests/test_mission_demo.py`

- [ ] Document the quality switch and recommended Lenovo mode.
- [ ] Run `uv run pytest tests/test_mission_demo.py -q`; expect all mission-demo tests to pass.
- [ ] Run `uv run pytest -q`; expect the repository suite to pass.
- [ ] Open both scenarios in Chrome, verify realistic GLBs load, switch quality modes, run AUTO, and confirm reports still cite the visible track IDs.
- [ ] Capture urban and jungle screenshots and inspect browser logs for application-origin errors.
