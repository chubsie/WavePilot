"""run_mission CLI — mock 헤드리스 임무, 임무 파일·구조화 로그, 인자 검증, browser 구성(가짜 구성 요소) 연결."""

import ast
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.drone import run_mission
from modules.drone.run_scenario import BrowserComponents
from modules.flight_control.interface import HOME_GPS_FIX, Telemetry
from modules.mission.operator import LinkStatus, ScriptedOperator
from modules.rule_mode.interface import SearchArea
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
EXAMPLE_MISSION_PATH = WAVEPILOT_ROOT / "missions" / "red_heart.toml"

# missions/red_heart.toml 과 같은 값 — write_mission_file 이 섹션별로 덮어쓴다
EXAMPLE_MISSION_SECTIONS = {
    "target": {"class": "person", "upper_color": "red", "action": "heart"},
    "search_area": {"north_m": [20.0, 120.0], "east_m": [-50.0, 50.0]},
    "safety": {"geofence_margin_m": 20.0, "battery_reserve_pct": 15.0, "max_flight_sec": 300.0},
}


@pytest.fixture(autouse=True)
def isolated_log_dir(monkeypatch, tmp_path):
    """main 을 부르는 테스트가 리포의 logs/ 에 임무 로그를 쓰지 않게 한다."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(run_mission, "DEFAULT_MISSION_LOG_DIR", log_dir)

    return log_dir


def write_mission_file(tmp_path: Path, **section_overrides: dict) -> Path:
    sections = {name: dict(values) for name, values in EXAMPLE_MISSION_SECTIONS.items()}
    for name, values in section_overrides.items():
        sections.setdefault(name, {}).update(values)

    lines = []
    for name, values in sections.items():
        lines.append(f"[{name}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
        lines.append("")

    path = tmp_path / "mission.toml"
    path.write_text("\n".join(lines), encoding="utf-8")

    return path


def read_log_records(log_dir: Path) -> list[dict]:
    log_paths = list(log_dir.glob("*.jsonl"))
    assert len(log_paths) == 1, log_paths

    return [json.loads(line) for line in log_paths[0].read_text(encoding="utf-8").splitlines()]


class CapturingRunnerFactory:
    """MissionRunner 대역 — 생성 kwargs 를 기록하고 run() 은 성공 결과를 돌려준다."""

    def __init__(self) -> None:
        self.kwargs_list: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_list.append(kwargs)
        return self

    @property
    def kwargs(self) -> dict:
        assert len(self.kwargs_list) == 1
        return self.kwargs_list[0]

    def run(self):
        return SimpleNamespace(is_success=True)


class CapturingSafetyMonitor:
    def __init__(self, config) -> None:
        self.config = config


# --- mock 임무 ---


def test_mock_mission_exits_zero_and_logs_selections(monkeypatch, caplog):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    exit_code = run_mission.main(["--sim", "mock", "--max-mission-sec", "600"])

    messages = [record.getMessage() for record in caplog.records]
    selection_lines = [message for message in messages if message.startswith("selection")]
    assert exit_code == run_mission.EXIT_OK
    assert len(selection_lines) >= 2
    assert any("mode=search" in line for line in selection_lines)
    assert any("mode=approach" in line for line in selection_lines)
    assert any(
        message.startswith("mode_end") and "mode=aim_and_fire outcome=success" in message for message in messages
    )
    assert any(message.startswith("heart ") for message in messages)
    assert any(
        message.startswith("mode_end") and "mode=return_home outcome=success" in message for message in messages
    )


def test_mock_mission_without_target_color_exits_zero(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)

    exit_code = run_mission.main(["--sim", "mock", "--target-color", "none", "--max-mission-sec", "600"])

    assert exit_code == run_mission.EXIT_OK


def test_mock_mission_runs_as_subprocess(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "WAVEPILOT_SELECTOR"}

    completed = subprocess.run(
        [sys.executable, "-m", "modules.drone.run_mission", "--sim", "mock", "--log-dir", str(tmp_path)],
        cwd=WAVEPILOT_ROOT, env=env, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC,
    )

    assert completed.returncode == 0, completed.stderr
    assert "selection" in completed.stderr


def test_mock_mission_that_cannot_finish_exits_failure(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)

    # 한 틱이 100 s 라 이륙 timeout(30 s)을 넘긴다
    exit_code = run_mission.main(["--sim", "mock", "--tick-hz", "0.01"])

    assert exit_code == run_mission.EXIT_FAILURE


# --- 임무 파일 (--mission) ---


def test_example_mission_file_lands_and_exits_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)

    exit_code = run_mission.main(
        ["--sim", "mock", "--mission", str(EXAMPLE_MISSION_PATH), "--log-dir", str(tmp_path)]
    )

    assert exit_code == run_mission.EXIT_OK


@pytest.mark.parametrize(
    ("section_overrides", "expected_fragments"),
    [
        ({"target": {"upper_color": "blue"}}, ("target.upper_color",)),
        ({"safety": {"geofence_margin": 20.0}}, ("safety.geofence_margin", "not allowed")),
    ],
)
def test_invalid_mission_file_is_usage_error_with_field_name(
    monkeypatch, caplog, tmp_path, section_overrides, expected_fragments
):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    mission_path = write_mission_file(tmp_path, **section_overrides)
    log_dir = tmp_path / "logs-invalid"

    exit_code = run_mission.main(["--sim", "mock", "--mission", str(mission_path), "--log-dir", str(log_dir)])

    error_lines = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert exit_code == run_mission.EXIT_USAGE
    assert any(all(fragment in line for fragment in expected_fragments) for line in error_lines), error_lines
    assert not list(log_dir.glob("*.jsonl"))


def test_missing_mission_file_is_usage_error(monkeypatch, tmp_path):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    log_dir = tmp_path / "logs-missing"

    exit_code = run_mission.main(
        ["--sim", "mock", "--mission", str(tmp_path / "nope.toml"), "--log-dir", str(log_dir)]
    )

    assert exit_code == run_mission.EXIT_USAGE
    assert not list(log_dir.glob("*.jsonl"))


def test_mission_file_overrides_area_color_and_safety_limits(monkeypatch, caplog, tmp_path):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    caplog.set_level(logging.INFO, logger="modules.drone.run_mission")
    mission_path = write_mission_file(
        tmp_path,
        target={"upper_color": "any"},
        search_area={"north_m": [30.0, 90.0], "east_m": [-20.0, 20.0]},
        safety={"geofence_margin_m": 10.0, "battery_reserve_pct": 25.0, "max_flight_sec": 200.0},
    )
    runner_factory = CapturingRunnerFactory()
    monkeypatch.setattr(run_mission, "MissionRunner", runner_factory)
    monkeypatch.setattr(run_mission, "SafetyMonitor", CapturingSafetyMonitor)

    exit_code = run_mission.main(
        ["--sim", "mock", "--mission", str(mission_path), "--target-color", "red", "--log-dir", str(tmp_path)]
    )

    kwargs = runner_factory.kwargs
    expected_area = SearchArea(north_min_m=30.0, north_max_m=90.0, east_min_m=-20.0, east_max_m=20.0)
    assert exit_code == run_mission.EXIT_OK
    assert kwargs["settings"].search_area == expected_area
    assert kwargs["settings"].target_upper_color is None  # 파일(any)이 --target-color red 보다 우선
    assert kwargs["param_defaults"].search_area_north_min_m == 30.0
    assert kwargs["config"].max_mission_sec == 200.0

    safety_config = kwargs["safety"].config
    assert safety_config.search_area == expected_area
    assert safety_config.geofence_margin_m == 10.0
    assert safety_config.battery_reserve_pct == 25.0
    assert safety_config.max_flight_sec == 200.0
    assert isinstance(kwargs["operator"], ScriptedOperator)

    messages = [record.getMessage() for record in caplog.records]
    assert any("overrides --target-color red" in message for message in messages)


@pytest.mark.parametrize(("color_args", "expected_color"), [([], "red"), (["--target-color", "none"], None)])
def test_without_mission_file_uses_defaults_and_cli(monkeypatch, tmp_path, color_args, expected_color):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    runner_factory = CapturingRunnerFactory()
    monkeypatch.setattr(run_mission, "MissionRunner", runner_factory)

    exit_code = run_mission.main(["--sim", "mock", "--max-mission-sec", "450", *color_args])

    kwargs = runner_factory.kwargs
    assert exit_code == run_mission.EXIT_OK
    assert kwargs["safety"] is None  # MissionRunner 기본 안전 감시
    assert kwargs["settings"].target_upper_color == expected_color
    assert kwargs["config"].max_mission_sec == 450.0


# --- 구조화 임무 로그 (--log-dir) ---


def test_mission_log_file_has_start_selection_and_end(monkeypatch, tmp_path):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    log_dir = tmp_path / "missions"

    exit_code = run_mission.main(
        ["--sim", "mock", "--mission", str(EXAMPLE_MISSION_PATH), "--log-dir", str(log_dir)]
    )

    records = read_log_records(log_dir)
    start = records[0]
    assert exit_code == run_mission.EXIT_OK
    assert start["kind"] == "mission"
    assert start["event"] == "start"
    assert start["mission_file"] == str(EXAMPLE_MISSION_PATH)
    assert start["mission"]["target_class"] == "person"
    assert start["selector"] == "rule"
    assert set(start["home_gps"]) == {"latitude_deg", "longitude_deg", "altitude_m"}
    assert any(record["kind"] == "selection" for record in records)
    assert any(record["kind"] == "mission" and record.get("event") == "end" for record in records)


def test_mission_log_without_mission_file_has_null_mission(monkeypatch, isolated_log_dir):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    monkeypatch.setattr(run_mission, "MissionRunner", CapturingRunnerFactory())

    run_mission.main(["--sim", "mock"])

    start = read_log_records(isolated_log_dir)[0]
    assert start["event"] == "start"
    assert start["mission_file"] is None
    assert start["mission"] is None


# --- 인자·환경 검증 ---


def test_invalid_selector_env_is_usage_error(monkeypatch):
    monkeypatch.setenv("WAVEPILOT_SELECTOR", "bogus")

    assert run_mission.main(["--sim", "mock"]) == run_mission.EXIT_USAGE


@pytest.mark.parametrize("flag", ["--tick-hz", "--max-mission-sec"])
def test_non_positive_values_exit_with_usage_code(flag):
    with pytest.raises(SystemExit) as error:
        run_mission.main([flag, "0"])

    assert error.value.code == 2


def test_target_color_defaults_to_red_and_accepts_none():
    parser = run_mission.build_arg_parser()

    assert parser.parse_args([]).target_color is None  # 미지정 — 임무 파일과 함께 줬는지 구분용
    assert parser.parse_args(["--target-color", "none"]).target_color == "none"
    assert run_mission.resolve_target_upper_color(None, None) == "red"
    assert run_mission.resolve_target_upper_color("red", None) == "red"
    assert run_mission.resolve_target_upper_color("none", None) is None


def test_mission_file_color_wins_over_target_color_arg():
    spec_any = SimpleNamespace(target_upper_color=None)
    spec_red = SimpleNamespace(target_upper_color="red")

    assert run_mission.resolve_target_upper_color("red", spec_any) is None
    assert run_mission.resolve_target_upper_color(None, spec_red) == "red"
    assert run_mission.resolve_target_upper_color("none", spec_red) == "red"


def test_no_fault_injection_cli_options():
    option_strings = set(run_mission.build_arg_parser()._option_string_actions)

    assert not {option for option in option_strings if any(word in option for word in ("gps", "battery", "link"))}


def test_unknown_target_color_exits_with_usage_code():
    with pytest.raises(SystemExit) as error:
        run_mission.main(["--target-color", "bogus"])

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
    )
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)

    exit_code = run_mission.main(["--sim", "browser", "--sim-connect-timeout-sec", "0.2"])

    assert exit_code == run_mission.EXIT_FAILURE
    assert server.shutdown_called
    assert server.server_close_called


class UnfinishedRunner:
    def run(self):
        return None


class FakeConnectedBridge:
    ai_mode = True

    def is_connected(self) -> bool:
        return True

    def operator_link_status(self):
        return (True, None)

    def set_mission_status(self, status: dict) -> None:
        pass


def test_browser_orin_args_reach_yolo_factory(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    factory_calls: list[tuple[str, str, str]] = []
    yolo_factories: list = []
    components = BrowserComponents(
        sim_bridge=FakeConnectedBridge,
        start_sim_server=lambda bridge, host, port: FakeServer(),
        browser_flight_control=lambda bridge: None,
        browser_detection_source=lambda bridge, yolo_factory: yolo_factories.append(yolo_factory),
    )

    def fake_create_yolo_factory(kind, device, url):
        factory_calls.append((kind, device, url))
        return "orin-factory"

    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_mission, "create_yolo_factory", fake_create_yolo_factory)
    monkeypatch.setattr(run_mission, "LatestFrameReader", lambda source: None)
    monkeypatch.setattr(run_mission, "_build_runner", lambda *args, **kwargs: UnfinishedRunner())

    exit_code = run_mission.main(
        ["--sim", "browser", "--yolo", "orin", "--yolo-url", "http://10.0.0.5:8082", "--device", "cpu"]
    )

    assert exit_code == run_mission.EXIT_FAILURE
    assert factory_calls == [("orin", "cpu", "http://10.0.0.5:8082")]
    assert yolo_factories == ["orin-factory"]


# --- 브라우저 운용자 링크 (BridgeOperatorInput, 상태 push) ---


class FakeOperatorBridge:
    def __init__(self, ai_mode: bool = True, link_status_value=(True, None)) -> None:
        self.ai_mode = ai_mode
        self.link_status_value = link_status_value
        self.connected = True
        self.status_list: list[dict] = []

    def is_connected(self) -> bool:
        return self.connected

    def operator_link_status(self):
        return self.link_status_value

    def set_mission_status(self, status: dict) -> None:
        self.status_list.append(status)


class FakeTelemetryFlightControl:
    def get_telemetry(self) -> Telemetry:
        return Telemetry(
            time_sec=0.0, north_m=0.0, east_m=0.0, down_m=0.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
            gimbal_pitch_deg=0.0, gimbal_yaw_deg=0.0, battery_pct=100.0, flight_mode="manual", gps=HOME_GPS_FIX,
        )


class FakeDetectionSource:
    def close(self) -> None:
        pass


def test_bridge_operator_input_follows_ai_mode_and_link_status():
    bridge = FakeOperatorBridge(ai_mode=True, link_status_value=(False, 12.5))
    operator = run_mission.BridgeOperatorInput(bridge)

    assert operator.is_ai_requested() is True
    assert operator.link_status(42.0) == LinkStatus(False, 12.5)

    bridge.ai_mode = False
    bridge.link_status_value = (True, None)

    assert operator.is_ai_requested() is False
    assert operator.link_status(42.0) == LinkStatus(True, None)


def test_browser_mission_wires_operator_status_and_abort(monkeypatch, tmp_path):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    bridge = FakeOperatorBridge()
    components = BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: FakeServer(),
        browser_flight_control=lambda bridge: FakeTelemetryFlightControl(),
        browser_detection_source=lambda bridge, yolo_factory: FakeDetectionSource(),
    )
    runner_factory = CapturingRunnerFactory()
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_mission, "create_yolo_factory", lambda kind, device, url: None)
    monkeypatch.setattr(run_mission, "LatestFrameReader", lambda source: None)
    monkeypatch.setattr(run_mission, "MissionRunner", runner_factory)

    exit_code = run_mission.main(["--sim", "browser", "--log-dir", str(tmp_path)])

    kwargs = runner_factory.kwargs
    assert exit_code == run_mission.EXIT_OK
    assert isinstance(kwargs["operator"], run_mission.BridgeOperatorInput)
    assert kwargs["operator"]._bridge is bridge

    kwargs["on_status"]({"mode": "AI", "reason": "operator"})
    assert bridge.status_list == [{"mode": "AI", "reason": "operator"}]

    assert kwargs["should_abort"]() is False
    bridge.connected = False
    assert kwargs["should_abort"]() is True

    start = read_log_records(tmp_path)[0]
    assert start["home_gps"] == HOME_GPS_FIX.to_dict()


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


# --- README run_mission 예시 ---

README_PYTHON_SECTION_HEADING = "## Python: LLM overwatch pipeline"
README_FLAG_PATTERN = re.compile(r"(?<!\S)--[a-z][a-z0-9-]*")
README_MISSION_PATH_PATTERN = re.compile(r"missions/[A-Za-z0-9_]+\.toml")
README_RUN_MISSION_MODULE = "python -m modules.drone.run_mission"


def _read_readme_run_mission_lines() -> list[str]:
    readme_text = (WAVEPILOT_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme_text.index(README_PYTHON_SECTION_HEADING)
    end = readme_text.find("\n## ", start + len(README_PYTHON_SECTION_HEADING))
    section = readme_text[start:] if end < 0 else readme_text[start:end]

    joined_text = section.replace("\\\n", " ")  # 백슬래시 줄이음은 한 명령이다
    # 모듈 이름 뒤만 남긴다 — 앞쪽 플래그(uv run --group yolo)는 run_mission 인자가 아니다
    return [
        line.split(README_RUN_MISSION_MODULE, 1)[1]
        for line in joined_text.splitlines()
        if README_RUN_MISSION_MODULE in line
    ]


def test_readme_run_mission_flags_and_paths_exist():
    lines = _read_readme_run_mission_lines()
    option_strings = set(run_mission.build_arg_parser()._option_string_actions)

    unknown_flags = sorted({flag for line in lines for flag in README_FLAG_PATTERN.findall(line)} - option_strings)
    mission_paths = {path for line in lines for path in README_MISSION_PATH_PATTERN.findall(line)}
    missing_paths = sorted(path for path in mission_paths if not (WAVEPILOT_ROOT / path).is_file())

    assert lines, "README has no `python -m modules.drone.run_mission` command line"
    assert not unknown_flags, f"README run_mission flags not defined: {unknown_flags}"
    assert mission_paths, "README run_mission lines mention no mission file"
    assert not missing_paths, f"README mentions mission files that do not exist: {missing_paths}"
