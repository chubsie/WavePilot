import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEMO = ROOT / "tools" / "mission_demo"


def test_demo_shell_contract():
    html = (DEMO / "index.html").read_text()
    required_ids = (
        "flight-mode-badge",
        "connect-ble",
        "zero-imu",
        "observation-toggle",
        "detection-list",
        "report-content",
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
    for name in ("urban.glb",):
        data = (DEMO / "assets" / name).read_bytes()
        assert data[:4] == b"glTF"
        assert len(data) < 18 * 1024 * 1024


def test_demo_contains_only_the_urban_scenario():
    assert not (DEMO / "assets" / "mountain.glb").exists()
    assert not (DEMO / "assets" / "mountain.blend").exists()
    for name in ("index.html", "app.js", "world.js", "input.js", "analysis.js", "styles.css"):
        assert "mountain" not in (DEMO / name).read_text().lower()


def test_actual_flight_and_glove_behavior():
    subprocess.run(["node", "--test", "tests/mission_flight.test.mjs", "tests/mission_main_parity.test.mjs"], cwd=ROOT, check=True)


def test_cinematic_environment_contract():
    html = (DEMO / "index.html").read_text()
    assert 'id="cinematic-effects"' in html
    assert 'data-quality=' not in html
    assert 'world.setQuality("cinematic")' in (DEMO / "app.js").read_text()
    for element_id in ("flex-flat", "flex-bent"):
        assert f'id="{element_id}"' in html.split('<details id="advanced-controls">')[0]
    assert '>FLAT</button>' in html
    assert '>BENT</button>' in html

    manifest = json.loads((DEMO / "assets" / "environment-manifest.json").read_text())
    assert set(manifest) == {"urban"}
    assert manifest["urban"]["style"] == "war-damaged-european-city"
    assert manifest["urban"]["generated_objects"] >= 700
    assert manifest["urban"]["colliders"] == 14
    assert "explicit-collision-proxies" in manifest["urban"]["features"]


def test_collision_blocks_forward_motion_and_allows_wall_slide():
    script = """
      import { resolveMovement } from './tools/mission_demo/collision.js';
      const wall = [{ min: [1, -2, -2], max: [2, 2, 2] }];
      const result = resolveMovement([0, 0, 0], [2, 0, 1], 0.7, wall);
      if (!result.blocked || result.position[0] > 0.3 || Math.abs(result.position[2] - 1) > 1e-8) process.exit(1);
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
