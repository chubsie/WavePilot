"""run_scenario --sim {mock,browser} 구현 스위치 (SC4, X-11, D-10).

browser 쪽 구현(server, flight_control, jpeg_source)은 가짜로만 끼운다 — 실제 서버·브라우저·YOLO 를 띄우지 않는다.
"""

import ast
import threading
from pathlib import Path

import pytest

from modules.drone import run_scenario
from modules.flight_control.mock import MockFlightControl
from modules.simulation.detection_source import ScenarioDetectionSource

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
RUN_SCENARIO_PATH = WAVEPILOT_ROOT / "modules" / "drone" / "run_scenario.py"
SCENARIO_PATH = "modules/simulation/scenarios/checkpoint.toml"
LAZY_ONLY_MODULES = frozenset({
    "modules.simulation.browser.server",
    "modules.simulation.browser.flight_control",
    "modules.yolo_detection.jpeg_source",
})
WAIT_TIMEOUT_SEC = 2.0


# --- 가짜 browser 구성 요소 ---


class FakeBridge:
    def __init__(self, connected: bool = True) -> None:
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


class FakeFlightControl:
    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge


class FakeDetectionSource:
    def __init__(self, bridge: FakeBridge, yolo_factory) -> None:
        self.bridge = bridge
        self.yolo_factory = yolo_factory
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


class FakeYoloFactoryRecorder:
    """create_yolo_factory 대역. 받은 (kind, device, url) 을 기록하고 장치만 담은 팩토리를 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, kind: str, device: str, url: str):
        self.calls.append((kind, device, url))
        return lambda jpeg_items: ("yolo", device)


class FakeComponents:
    """_load_browser_components 대역. 만든 객체를 기록해 테스트가 확인한다."""

    def __init__(self, connected: bool = True) -> None:
        self.bridge = FakeBridge(connected)
        self.server = FakeServer()
        self.server_address: tuple[str, int] | None = None
        self.yolo_factory_recorder = FakeYoloFactoryRecorder()

    def build(self) -> run_scenario.BrowserComponents:
        return run_scenario.BrowserComponents(
            sim_bridge=lambda: self.bridge,
            start_sim_server=self._start_sim_server,
            browser_flight_control=FakeFlightControl,
            browser_detection_source=FakeDetectionSource,
        )

    def _start_sim_server(self, bridge: FakeBridge, host: str, port: int) -> FakeServer:
        self.server_address = (host, port)
        return self.server


def _parse_browser_args(*extra: str):
    return run_scenario.build_arg_parser().parse_args(["--sim", "browser", "--operator-port", "1", *extra])


def _install_components(monkeypatch: pytest.MonkeyPatch, components: FakeComponents) -> None:
    monkeypatch.setattr(run_scenario, "_load_browser_components", components.build)
    monkeypatch.setattr(run_scenario, "create_yolo_factory", components.yolo_factory_recorder)


# --- 파서·검증 ---


def test_sim_flag_defaults_to_mock_and_accepts_browser() -> None:
    parser = run_scenario.build_arg_parser()

    default_args = parser.parse_args(["--scenario", SCENARIO_PATH, "--operator-port", "1"])
    browser_args = parser.parse_args(["--sim", "browser", "--operator-port", "1"])

    assert default_args.sim == "mock"
    assert browser_args.sim == "browser"
    assert browser_args.scenario is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--sim", "gazebo", "--operator-port", "1"])


def test_mock_without_scenario_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as error:
        run_scenario.main(["--operator-port", "1"])

    assert error.value.code == run_scenario.EXIT_USAGE


@pytest.mark.parametrize("flag", ["--sim-connect-timeout-sec", "--duration-sec"])
def test_browser_rejects_non_positive_timeouts(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        run_scenario.main(["--sim", "browser", "--operator-port", "1", flag, "0"])

    assert error.value.code == run_scenario.EXIT_USAGE


# --- main 분기 ---


def test_main_browser_runs_browser_sim_without_loading_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    def fail_load_scenario(path):
        raise AssertionError("browser mode must not load a scenario")

    monkeypatch.setattr(run_scenario, "load_scenario", fail_load_scenario)
    monkeypatch.setattr(run_scenario, "_run_browser_sim", lambda args: calls.append(args) or 7)

    exit_code = run_scenario.main(["--sim", "browser", "--operator-port", "1"])

    assert exit_code == 7
    assert calls[0].sim == "browser"


def test_main_default_mock_assembles_scenario_components(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def capture_pipeline(args, detection_source, flight_control, pace, run_reports_in_background, label):
        captured.update(
            detection_source=detection_source, flight_control=flight_control, pace=pace,
            run_reports_in_background=run_reports_in_background, label=label,
        )
        return run_scenario.EXIT_OK

    monkeypatch.setattr(run_scenario, "_run_pipeline", capture_pipeline)

    exit_code = run_scenario.main(["--scenario", SCENARIO_PATH, "--operator-port", "1", "--speed", "0"])

    assert exit_code == run_scenario.EXIT_OK
    assert isinstance(captured["flight_control"], MockFlightControl)
    assert isinstance(captured["detection_source"], ScenarioDetectionSource)
    assert captured["pace"] is None
    assert captured["run_reports_in_background"] is False
    assert captured["label"] == "checkpoint"


# --- _run_browser_sim ---


def test_browser_sim_fails_when_page_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    components = FakeComponents(connected=False)
    _install_components(monkeypatch, components)
    monkeypatch.setattr(run_scenario, "_run_pipeline", lambda *args, **kwargs: pytest.fail("pipeline must not run"))

    exit_code = run_scenario._run_browser_sim(_parse_browser_args("--sim-connect-timeout-sec", "0.2"))

    assert exit_code == run_scenario.EXIT_FAILURE
    assert components.server.shutdown_called
    assert components.server.server_close_called


def test_browser_sim_wires_browser_components_into_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    components = FakeComponents()
    captured: dict = {}

    def capture_pipeline(args, detection_source, flight_control, pace, run_reports_in_background, label):
        captured.update(detection_source=detection_source, flight_control=flight_control, pace=pace, label=label)
        return run_scenario.EXIT_OK

    _install_components(monkeypatch, components)
    monkeypatch.setattr(run_scenario, "_run_pipeline", capture_pipeline)

    exit_code = run_scenario._run_browser_sim(_parse_browser_args("--sim-port", "9001", "--device", "cpu"))

    detection_source = captured["detection_source"]
    assert exit_code == run_scenario.EXIT_OK
    assert components.server_address == ("127.0.0.1", 9001)
    assert isinstance(captured["flight_control"], FakeFlightControl)
    assert captured["flight_control"].bridge is components.bridge
    assert isinstance(detection_source, FakeDetectionSource)
    assert detection_source.bridge is components.bridge
    assert detection_source.yolo_factory([]) == ("yolo", "cpu")
    assert components.yolo_factory_recorder.calls == [("local", "cpu", "http://127.0.0.1:8082")]
    assert captured["pace"] is None
    assert detection_source.closed.is_set()
    assert components.server.shutdown_called
    assert components.server.server_close_called


def test_browser_sim_passes_orin_yolo_args_to_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    components = FakeComponents()
    _install_components(monkeypatch, components)
    monkeypatch.setattr(run_scenario, "_run_pipeline", lambda *args, **kwargs: run_scenario.EXIT_OK)

    run_scenario._run_browser_sim(
        _parse_browser_args("--yolo", "orin", "--yolo-url", "http://10.0.0.5:8082", "--device", "cpu")
    )

    assert components.yolo_factory_recorder.calls == [("orin", "cpu", "http://10.0.0.5:8082")]


def test_browser_sim_cleans_up_when_pipeline_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    components = FakeComponents()
    built: list[FakeDetectionSource] = []

    def raise_pipeline(args, detection_source, *rest, **kwargs):
        built.append(detection_source)
        raise RuntimeError("boom")

    _install_components(monkeypatch, components)
    monkeypatch.setattr(run_scenario, "_run_pipeline", raise_pipeline)

    with pytest.raises(RuntimeError):
        run_scenario._run_browser_sim(_parse_browser_args())

    assert built[0].closed.is_set()
    assert components.server.shutdown_called
    assert components.server.server_close_called


def test_browser_sim_duration_closes_detection_source(monkeypatch: pytest.MonkeyPatch) -> None:
    components = FakeComponents()
    closed_by_timer: list[bool] = []

    def wait_for_timer(args, detection_source, *rest, **kwargs):
        closed_by_timer.append(detection_source.closed.wait(WAIT_TIMEOUT_SEC))
        return run_scenario.EXIT_OK

    _install_components(monkeypatch, components)
    monkeypatch.setattr(run_scenario, "_run_pipeline", wait_for_timer)

    run_scenario._run_browser_sim(_parse_browser_args("--duration-sec", "0.1"))

    assert closed_by_timer == [True]


# --- 지연 import ---


def test_browser_implementations_are_imported_lazily() -> None:
    tree = ast.parse(RUN_SCENARIO_PATH.read_text(encoding="utf-8"))

    module_level_imports = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }

    assert not module_level_imports & LAZY_ONLY_MODULES
