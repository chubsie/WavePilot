"""run_mission CLI — mock 헤드리스 임무, 인자 검증, browser 구성(가짜 구성 요소) 연결."""

import ast
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from modules.drone import run_mission
from modules.drone.run_scenario import BrowserComponents
from modules.yolo_detection.interface import Frame

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
RUN_MISSION_PATH = WAVEPILOT_ROOT / "modules" / "drone" / "run_mission.py"
LAZY_ONLY_MODULES = frozenset({
    "modules.simulation.browser.bridge",
    "modules.simulation.browser.detection_source",
    "modules.simulation.browser.flight_control",
    "modules.simulation.browser.server",
    "modules.yolo_detection.jpeg_source",
})
SUBPROCESS_TIMEOUT_SEC = 60


# --- mock 임무 ---


def test_mock_mission_exits_zero_and_logs_selections(monkeypatch, caplog):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    exit_code = run_mission.main(["--sim", "mock", "--max-mission-sec", "300"])

    messages = [record.getMessage() for record in caplog.records]
    selection_lines = [message for message in messages if message.startswith("selection")]
    assert exit_code == run_mission.EXIT_OK
    assert len(selection_lines) >= 2
    assert any("mode=search" in line for line in selection_lines)
    assert any("mode=approach" in line for line in selection_lines)
    assert any(
        message.startswith("mode_end") and "mode=return_home outcome=success" in message for message in messages
    )


def test_mock_mission_runs_as_subprocess():
    env = {key: value for key, value in os.environ.items() if key != "WAVEPILOT_SELECTOR"}

    completed = subprocess.run(
        [sys.executable, "-m", "modules.drone.run_mission", "--sim", "mock"],
        cwd=WAVEPILOT_ROOT, env=env, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC,
    )

    assert completed.returncode == 0, completed.stderr
    assert "selection" in completed.stderr


def test_mock_mission_that_cannot_finish_exits_failure(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)

    # 한 틱이 100 s 라 이륙 timeout(30 s)을 넘긴다
    exit_code = run_mission.main(["--sim", "mock", "--tick-hz", "0.01"])

    assert exit_code == run_mission.EXIT_FAILURE


# --- 인자·환경 검증 ---


def test_invalid_selector_env_is_usage_error(monkeypatch):
    monkeypatch.setenv("WAVEPILOT_SELECTOR", "bogus")

    assert run_mission.main(["--sim", "mock"]) == run_mission.EXIT_USAGE


@pytest.mark.parametrize("flag", ["--tick-hz", "--max-mission-sec"])
def test_non_positive_values_exit_with_usage_code(flag):
    with pytest.raises(SystemExit) as error:
        run_mission.main([flag, "0"])

    assert error.value.code == 2


def test_negative_speed_exits_with_usage_code():
    with pytest.raises(SystemExit) as error:
        run_mission.main(["--speed", "-1"])

    assert error.value.code == 2


# --- browser 구성 ---


class FakeBridge:
    def __init__(self, connected: bool = False) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected


class FakeServer:
    def __init__(self) -> None:
        self.shutdown_called = False
        self.server_close_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.server_close_called = True


def test_browser_without_connection_fails_and_closes_server(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    bridge = FakeBridge(connected=False)
    server = FakeServer()
    components = BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: server,
        browser_flight_control=lambda bridge: None,
        browser_detection_source=lambda bridge, yolo_factory: None,
        create_jpeg_yolo_source=lambda items, device="auto": None,
    )
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)

    exit_code = run_mission.main(["--sim", "browser", "--sim-connect-timeout-sec", "0.2"])

    assert exit_code == run_mission.EXIT_FAILURE
    assert server.shutdown_called
    assert server.server_close_called


# --- 보조 클래스 ---


class BlockingSource:
    """read_frame 이 이벤트가 켜질 때까지 막히는 가짜 소스."""

    def __init__(self, frame: Frame) -> None:
        self._frame = frame
        self.release = threading.Event()
        self._returned = False

    def read_frame(self):
        self.release.wait()
        if self._returned:
            return None
        self._returned = True
        return self._frame

    def telemetry_for(self, timestamp_sec):
        return None


def test_latest_frame_reader_hands_out_each_frame_once_without_blocking():
    frame = Frame(frame_id=0, timestamp_sec=0.0, image_width=1280, image_height=720, detections=())
    source = BlockingSource(frame)
    reader = run_mission.LatestFrameReader(source)

    assert reader.read_frame() is None  # 소스가 막혀 있어도 즉시 돌아온다

    source.release.set()
    reader._thread.join(timeout=2.0)

    assert reader.read_frame() == frame
    assert reader.read_frame() is None
    reader.close()


def test_browser_modules_are_not_imported_at_module_level():
    tree = ast.parse(RUN_MISSION_PATH.read_text(encoding="utf-8"))

    imported = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}

    assert not imported & LAZY_ONLY_MODULES
