import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEMO = ROOT / "tools" / "mission_demo"


def test_demo_shell_contract():
    html = (DEMO / "index.html").read_text()
    required_ids = (
        "scenario-urban",
        "scenario-mountain",
        "mode-imu",
        "mode-keyboard",
        "mode-auto",
        "detection-list",
        "report-content",
        "request-analysis",
    )
    for element_id in required_ids:
        assert f'id="{element_id}"' in html


def test_demo_uses_local_three_and_focused_modules():
    html = (DEMO / "index.html").read_text()
    assert "../visualizer/three.min.js" in html
    assert 'src="app.js"' in html
    assert "https://" not in html
    for module in ("app.js", "world.js", "input.js", "analysis.js", "collision.js", "styles.css"):
        assert (DEMO / module).is_file()


def test_demo_assets_are_binary_glb_files():
    for name in ("urban.glb", "mountain.glb"):
        data = (DEMO / "assets" / name).read_bytes()
        assert data[:4] == b"glTF"
        assert len(data) < 18 * 1024 * 1024


def test_urban_camera_starts_facing_scene_center():
    world_source = (DEMO / "world.js").read_text()
    assert 'urban: { sky: 0x768384, fog: 0x65706b, start: [0, 18, 54], heading: 0 }' in world_source


def test_mountain_camera_starts_in_the_valley():
    world_source = (DEMO / "world.js").read_text()
    assert 'mountain: { sky: 0x75818a, fog: 0x5f6a70, start: [-10, 16, 58], heading: -0.2 }' in world_source


def test_cinematic_environment_contract():
    html = (DEMO / "index.html").read_text()
    for element_id in ("quality-cinematic", "quality-performance", "cinematic-effects"):
        assert f'id="{element_id}"' in html

    manifest = json.loads((DEMO / "assets" / "environment-manifest.json").read_text())
    assert manifest["mountain"]["style"] == "border-mountain-recon"
    assert manifest["urban"]["style"] == "war-damaged-european-city"
    assert manifest["mountain"]["generated_objects"] >= 200
    assert manifest["urban"]["generated_objects"] >= 700
    assert manifest["urban"]["forbidden_shapes"] == ["intact-box-tower"]


def test_collision_blocks_forward_motion_and_allows_wall_slide():
    script = """
      import { resolveMovement } from './tools/mission_demo/collision.js';
      const wall = [{ min: [1, -2, -2], max: [2, 2, 2] }];
      const result = resolveMovement([0, 0, 0], [2, 0, 1], 0.7, wall);
      if (!result.blocked || result.position[0] !== 0 || result.position[2] !== 1) process.exit(1);
    """
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
    )


def test_glove_input_supports_main_protocol_usb_and_ble():
    source = (DEMO / "input.js").read_text()
    for token in ("class GloveInput", "navigator.bluetooth", "NUS", "flex0", "buttons", "calibrateFlex"):
        assert token in source


def test_readme_documents_demo_launch_and_controls():
    readme = (ROOT / "README.md").read_text()
    assert "tools/mission_demo" in readme
    assert "python3 -m http.server 8000" in readme
    assert "WASD" in readme
    assert "Web Serial" in readme
