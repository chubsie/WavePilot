from pathlib import Path


ROOT = Path(__file__).parents[1]
DEMO = ROOT / "tools" / "mission_demo"


def test_demo_shell_contract():
    html = (DEMO / "index.html").read_text()
    required_ids = (
        "scenario-urban",
        "scenario-forest",
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
    for module in ("app.js", "world.js", "input.js", "analysis.js", "styles.css"):
        assert (DEMO / module).is_file()


def test_demo_assets_are_binary_glb_files():
    for name in ("urban.glb", "forest.glb"):
        data = (DEMO / "assets" / name).read_bytes()
        assert data[:4] == b"glTF"
        assert len(data) < 15 * 1024 * 1024


def test_urban_camera_starts_facing_scene_center():
    world_source = (DEMO / "world.js").read_text()
    assert 'urban: { sky: 0x768384, fog: 0x65706b, start: [0, 18, 54], heading: 0 }' in world_source


def test_readme_documents_demo_launch_and_controls():
    readme = (ROOT / "README.md").read_text()
    assert "tools/mission_demo" in readme
    assert "python3 -m http.server 8000" in readme
    assert "WASD" in readme
    assert "Web Serial" in readme
