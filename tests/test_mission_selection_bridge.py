"""선택·모드 종료 → 브라우저 — mission_loop 콜백 payload, bridge selection·mode_end 칸, run_mission browser 연결, urban_demo.toml."""

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from modules.decision.interface import SOURCE_RULE
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.drone import run_mission
from modules.drone.mission_loop import (
    SOURCE_FORCED,
    MissionConfig,
    MissionRunner,
    build_rule_mode_settings,
)
from modules.drone.run_mission import MOCK_DETECTION_RANGE_M, RangeLimitedTargetSource
from modules.drone.run_scenario import BrowserComponents
from modules.mission.mission_file import load_mission_file
from modules.mission.operator import ScriptedOperator
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.simulation.browser.bridge import PIPELINE_MESSAGE_KINDS, PIPELINE_MESSAGE_TTL_SEC, SimBridge
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
URBAN_DEMO_PATH = WAVEPILOT_ROOT / "missions" / "urban_demo.toml"
MISSION_DEMO_MAP_HALF_M = 68.0

SELECTION_KEYS = {
    "time_sec", "event_kind", "rule_mode", "params", "source", "confidence", "probabilities",
    "fallback_reason", "options", "latency_ms", "mode_result",
}
MODE_RESULT_KEYS = {"rule_mode", "outcome", "target_track"}
PERSON = ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0)


@pytest.fixture(autouse=True)
def isolated_log_dir(monkeypatch, tmp_path):
    """main 을 부르는 테스트가 리포의 logs/ 에 임무 로그를 쓰지 않게 한다."""
    monkeypatch.setattr(run_mission, "DEFAULT_MISSION_LOG_DIR", tmp_path / "logs")


def run_rule_mission(max_mission_sec: float, **callbacks):
    flight = KinematicFlightControl()
    defaults = ParamDefaults()
    source = RangeLimitedTargetSource(ScriptedTargetSource(flight, [PERSON]), flight, MOCK_DETECTION_RANGE_M)
    runner = MissionRunner(
        flight_control=flight,
        frame_source=source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=RuleSelector(defaults),
        param_defaults=defaults,
        settings=build_rule_mode_settings(defaults),
        clock=lambda: flight.now_sec,
        advance=flight.advance,
        config=MissionConfig(max_mission_sec=max_mission_sec),
        operator=ScriptedOperator(),
        **callbacks,
    )

    return runner.run()


# --- mission_loop 콜백 ---


def test_rule_mission_reports_every_selection_and_mode_end():
    selection_list: list[dict] = []
    mode_end_list: list[dict] = []

    result = run_rule_mission(300.0, on_selection=selection_list.append, on_mode_end=mode_end_list.append)

    assert len(selection_list) == len(result.selections) > 0
    assert len(mode_end_list) == len(result.mode_results) > 0
    assert [payload["rule_mode"] for payload in selection_list] == [record.rule_mode for record in result.selections]
    assert [payload["rule_mode"] for payload in mode_end_list] == [mode.rule_mode for mode in result.mode_results]
    json.dumps(selection_list)
    json.dumps(mode_end_list)


def test_selection_payload_shape_from_rule_selector():
    selection_list: list[dict] = []

    run_rule_mission(300.0, on_selection=selection_list.append)

    first = selection_list[0]
    assert set(first) == SELECTION_KEYS
    assert first["rule_mode"] == "search"
    assert first["source"] == SOURCE_RULE
    assert isinstance(first["confidence"], float)
    assert isinstance(first["probabilities"], dict)
    assert isinstance(first["options"], list) and "search" in first["options"]
    assert isinstance(first["latency_ms"], float)
    assert first["mode_result"] is None  # 첫 선택은 mode_ended 가 부른 것이 아니다

    after_mode_end = next(payload for payload in selection_list if payload["mode_result"] is not None)
    assert set(after_mode_end["mode_result"]) == MODE_RESULT_KEYS


def test_mode_end_payload_shape():
    mode_end_list: list[dict] = []

    result = run_rule_mission(300.0, on_mode_end=mode_end_list.append)

    assert all(set(payload) == {"time_sec"} | MODE_RESULT_KEYS for payload in mode_end_list)
    assert mode_end_list[-1]["rule_mode"] == result.mode_results[-1].rule_mode
    assert mode_end_list[-1]["outcome"] == result.mode_results[-1].outcome
    assert isinstance(mode_end_list[-1]["time_sec"], float)


def test_forced_return_selection_has_no_selector_fields():
    selection_list: list[dict] = []

    result = run_rule_mission(20.0, on_selection=selection_list.append)

    forced = selection_list[-1]
    assert result.forced_return
    assert forced["source"] == SOURCE_FORCED
    assert forced["rule_mode"] == "return_home"
    assert forced["confidence"] is None
    assert forced["probabilities"] == {}
    assert forced["options"] == []
    assert forced["latency_ms"] is None
    assert forced["mode_result"] is None
    json.dumps(forced)


def test_without_callbacks_mission_is_unchanged():
    with_callbacks = run_rule_mission(300.0, on_selection=lambda payload: None, on_mode_end=lambda payload: None)
    without_callbacks = run_rule_mission(300.0)

    assert without_callbacks == with_callbacks


# --- bridge pipeline 칸 ---


def test_bridge_has_selection_and_mode_end_kinds():
    bridge = SimBridge()

    assert set(PIPELINE_MESSAGE_KINDS) == {"state", "report", "selection", "mode_end"}
    assert bridge.pipeline_messages() == {"state": None, "report": None, "selection": None, "mode_end": None}


def test_bridge_selection_and_mode_end_keep_latest_and_expire_after_ttl():
    now_sec = [100.0]
    bridge = SimBridge(clock=lambda: now_sec[0])
    bridge.publish_pipeline_message("selection", {"seq": 1})
    bridge.publish_pipeline_message("selection", {"seq": 2})
    bridge.publish_pipeline_message("mode_end", {"seq": 1})

    messages = bridge.pipeline_messages()
    assert messages["selection"] == {"seq": 2}
    assert messages["mode_end"] == {"seq": 1}

    now_sec[0] += PIPELINE_MESSAGE_TTL_SEC + 0.1
    assert bridge.pipeline_messages()["selection"] is None
    assert bridge.pipeline_messages()["mode_end"] is None


# --- run_mission browser 연결 ---


class RecordingPipelineBridge:
    ai_mode = True

    def __init__(self) -> None:
        self.published_list: list[tuple[str, dict]] = []

    def is_connected(self) -> bool:
        return True

    def publish_pipeline_message(self, kind: str, entry: dict) -> None:
        self.published_list.append((kind, entry))

    def drain_heart_shots(self) -> list[dict]:
        return []


class FakeServer:
    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class CallbackFiringRunner:
    """_build_runner 대역 — 받은 콜백을 한 번씩 불러 브리지로 가는지 본다. run() 은 None(실패 종료)."""

    def __init__(self, on_selection, on_mode_end) -> None:
        self._on_selection = on_selection
        self._on_mode_end = on_mode_end

    def run(self):
        self._on_selection({"rule_mode": "search"})
        self._on_selection({"rule_mode": "approach"})
        self._on_mode_end({"rule_mode": "search"})


def test_browser_mission_publishes_selection_and_mode_end_to_bridge(monkeypatch, caplog):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    bridge = RecordingPipelineBridge()
    components = BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: FakeServer(),
        browser_flight_control=lambda bridge: None,
        browser_detection_source=lambda bridge, yolo_factory: None,
    )
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_mission, "create_yolo_factory", lambda kind, device, url: None)
    monkeypatch.setattr(run_mission, "LatestFrameReader", lambda source: None)
    monkeypatch.setattr(
        run_mission, "_build_runner",
        lambda *args, on_selection=None, on_mode_end=None, **kwargs: CallbackFiringRunner(on_selection, on_mode_end),
    )
    caplog.set_level(logging.INFO, logger="modules.drone.run_mission")

    exit_code = run_mission.main(["--sim", "browser", "--sim-host", "127.0.0.1", "--sim-port", "8766"])

    assert exit_code == run_mission.EXIT_FAILURE
    assert [(kind, entry["seq"], entry["payload"]) for kind, entry in bridge.published_list] == [
        ("selection", 1, {"rule_mode": "search"}),
        ("selection", 2, {"rule_mode": "approach"}),
        ("mode_end", 1, {"rule_mode": "search"}),
    ]
    for _, entry in bridge.published_list:
        assert set(entry) == {"seq", "sent_at", "payload"}
        assert datetime.fromisoformat(entry["sent_at"]).tzinfo is not None
    assert "http://127.0.0.1:8766/mission_demo/" in caplog.text
    assert "http://127.0.0.1:8766/drone_camera.html" in caplog.text


def test_publisher_into_real_bridge_reaches_pipeline_messages():
    bridge = SimBridge()
    publisher = run_mission.BridgePipelinePublisher(bridge)

    publisher.on_selection({"rule_mode": "search"})
    publisher.on_mode_end({"rule_mode": "search", "outcome": "success"})

    messages = bridge.pipeline_messages()
    assert messages["selection"]["seq"] == 1
    assert messages["selection"]["payload"] == {"rule_mode": "search"}
    assert messages["mode_end"]["payload"] == {"rule_mode": "search", "outcome": "success"}


# --- urban_demo.toml ---


def test_urban_demo_mission_loads_inside_mission_demo_map():
    spec = load_mission_file(URBAN_DEMO_PATH)

    assert spec.target_class == "person"
    assert spec.target_upper_color is None  # any
    for bound_m in (spec.search_north_min_m, spec.search_north_max_m, spec.search_east_min_m, spec.search_east_max_m):
        assert abs(bound_m) < MISSION_DEMO_MAP_HALF_M
    assert spec.search_north_min_m - spec.geofence_margin_m >= -MISSION_DEMO_MAP_HALF_M
    assert spec.search_north_max_m + spec.geofence_margin_m <= MISSION_DEMO_MAP_HALF_M
    assert spec.max_flight_sec == 300.0
