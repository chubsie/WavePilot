"""데모 안정화 — 단일 세션(최신 탭 우선)·링크 끊김 일시정지·sim 시계 보정·서버 상주.

- SimBridge/서버: 두 session_id 가 번갈아 POST 하면 최신 세션만 반영되고 응답에 session_active 가 실린다.
- MissionRunner: 시뮬레이터 링크가 아무리 오래 끊겨도 aborted 로 끝내지 않고, 재연결되면 이어서 착륙한다.
- 역행 시계: 새로고침으로 페이지 시계가 0 으로 돌아가도 임무 시계·안전 감시 시각은 뒤로 가지 않는다.
- run_mission --sim browser 상주: 임무가 끝나도 종료하지 않고 MANUAL→AI 재전환에 새 임무(새 로그 파일)를 시작한다.
"""

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.drone import run_mission
from modules.drone.mission_loop import (
    END_ABORTED,
    END_LANDED,
    MissionConfig,
    MissionRunner,
    MissionRunResult,
    build_rule_mode_settings,
)
from modules.drone.run_scenario import BrowserComponents
from modules.flight_control.interface import GoTo, Telemetry, ned_to_gps
from modules.mission.mission_log import KIND_MISSION, KIND_SAFETY, MissionLog
from modules.mission.operator import ScriptedOperator
from modules.mission.safety import SafetyConfig, SafetyMonitor
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.simulation.browser.bridge import LINK_TIMEOUT_SEC, BrowserFrame, SimBridge
from modules.simulation.browser.server import start_sim_server

JPEG_BYTES = b"\xff\xd8fake"
GOTO_COMMAND = GoTo(north_m=20.0, east_m=0.0, altitude_m=15.0)


def make_telemetry(time_sec: float = 1.5, north_m: float = 10.0) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=north_m, east_m=-5.0, down_m=-12.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0, battery_pct=90.0, flight_mode="hover",
        gps=ned_to_gps(north_m, -5.0, -12.0),
    )


class FakeClock:
    def __init__(self) -> None:
        self.now_sec = 0.0

    def __call__(self) -> float:
        return self.now_sec


# --- A. 단일 세션 (SimBridge) ---


def test_newest_session_wins_and_old_session_is_ignored() -> None:
    bridge = SimBridge()

    assert bridge.update_from_browser(make_telemetry(10.0, north_m=1.0), True, [], session_id="tab-a") is True
    assert bridge.update_from_browser(make_telemetry(3.0, north_m=2.0), False, [], session_id="tab-b") is True
    # 밀려난 tab-a 가 계속 보내도 상태는 tab-b 그대로다
    assert bridge.update_from_browser(make_telemetry(11.0, north_m=1.0), True, [], session_id="tab-a") is False

    assert bridge.latest_telemetry().north_m == 2.0
    assert bridge.ai_mode is False


def test_inactive_session_events_do_not_reach_bridge() -> None:
    bridge = SimBridge()
    shot = {"kind": "heart_shot", "track_id": 3, "time_sec": 1.0, "actor_id": "red_1"}

    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-a")
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-b")
    bridge.update_from_browser(make_telemetry(1.1), True, [shot], session_id="tab-a")

    assert bridge.drain_heart_shots() == []


def test_without_session_id_is_one_anonymous_session() -> None:
    bridge = SimBridge()

    assert bridge.update_from_browser(make_telemetry(1.0), True, []) is True
    assert bridge.update_from_browser(make_telemetry(1.1), False, []) is True
    assert bridge.ai_mode is False


def test_displaced_session_takes_over_only_after_active_goes_silent() -> None:
    clock = FakeClock()
    bridge = SimBridge(clock=clock)
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-a")
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-b")

    clock.now_sec = LINK_TIMEOUT_SEC / 2
    assert bridge.update_from_browser(make_telemetry(1.5), True, [], session_id="tab-a") is False

    # 새 탭(tab-b)을 닫으면 옛 탭이 이어받는다
    clock.now_sec = LINK_TIMEOUT_SEC * 2
    assert bridge.update_from_browser(make_telemetry(2.0), True, [], session_id="tab-a") is True


def test_session_switch_drops_commands_queued_for_old_tab() -> None:
    bridge = SimBridge()
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-a")
    bridge.enqueue_command(GOTO_COMMAND)

    bridge.update_from_browser(make_telemetry(0.1), True, [], session_id="tab-b")

    assert bridge.drain_commands() == []


def test_inactive_session_frame_is_dropped() -> None:
    bridge = SimBridge()
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-a")
    bridge.update_from_browser(make_telemetry(1.0), True, [], session_id="tab-b")
    frame = BrowserFrame(seq=1, timestamp_sec=1.0, jpeg_bytes=JPEG_BYTES, telemetry=make_telemetry(1.0), received_at_sec=0.0)

    assert bridge.put_frame(frame, session_id="tab-a") is False
    assert bridge.wait_frame(None, timeout_sec=0.01) is None
    assert bridge.put_frame(frame, session_id="tab-b") is True


# --- A. 단일 세션 (HTTP 와이어) ---


@pytest.fixture
def sim_server() -> Iterator[tuple[SimBridge, int]]:
    bridge = SimBridge()
    server = start_sim_server(bridge, port=0)

    yield bridge, server.server_address[1]

    bridge.close()
    server.shutdown()
    server.server_close()


def request(port: int, method: str, path: str, body: bytes, headers: dict):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def post_telemetry(port: int, session_id: str | None, time_sec: float, north_m: float = 10.0) -> tuple[int, dict]:
    body = {"telemetry": make_telemetry(time_sec, north_m).to_dict(), "ai_mode": True, "events": []}
    if session_id is not None:
        body["session_id"] = session_id
    status, _, raw = request(
        port, "POST", "/sim/telemetry", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    return status, json.loads(raw) if status == 200 else {}


def test_interleaved_sessions_over_http_only_latest_is_applied(sim_server) -> None:
    bridge, port = sim_server

    _, first = post_telemetry(port, "tab-a", 125.0, north_m=1.0)
    _, second = post_telemetry(port, "tab-b", 4.0, north_m=2.0)
    bridge.enqueue_command(GOTO_COMMAND)
    _, stale = post_telemetry(port, "tab-a", 125.1, north_m=1.0)
    _, active = post_telemetry(port, "tab-b", 4.1, north_m=2.0)

    assert first["session_active"] is True
    assert second["session_active"] is True
    assert stale["session_active"] is False
    assert stale["commands"] == []  # 비활성 탭은 명령을 꺼내 가지 못한다
    assert active["session_active"] is True
    assert [command["kind"] for command in active["commands"]] == [GOTO_COMMAND.KIND]
    assert bridge.latest_telemetry().north_m == 2.0


def test_telemetry_without_session_id_stays_backward_compatible(sim_server) -> None:
    _, port = sim_server

    status, response = post_telemetry(port, None, 1.0)

    assert status == 200
    assert response["session_active"] is True


def test_invalid_session_id_is_rejected(sim_server) -> None:
    bridge, port = sim_server
    body = {"telemetry": make_telemetry().to_dict(), "ai_mode": True, "events": [], "session_id": 7}

    status, _, _ = request(port, "POST", "/sim/telemetry", json.dumps(body).encode(), {"Content-Type": "application/json"})

    assert status == 400
    assert bridge.latest_telemetry() is None


def test_frame_session_header_follows_same_rule(sim_server) -> None:
    bridge, port = sim_server
    post_telemetry(port, "tab-a", 1.0)
    post_telemetry(port, "tab-b", 1.0)
    headers = {"Content-Type": "image/jpeg", "X-Telemetry": json.dumps(make_telemetry().to_dict())}

    stale_status, stale_headers, _ = request(port, "POST", "/sim/frame?seq=1", JPEG_BYTES, {**headers, "X-Session-Id": "tab-a"})
    active_status, active_headers, _ = request(port, "POST", "/sim/frame?seq=2", JPEG_BYTES, {**headers, "X-Session-Id": "tab-b"})

    assert (stale_status, stale_headers["X-Session-Active"]) == (204, "false")
    assert (active_status, active_headers["X-Session-Active"]) == (204, "true")
    assert bridge.wait_frame(None, timeout_sec=0.5).seq == 2


# --- B. sim 시계 보정 (새로고침하면 페이지 시계가 0 부터) ---


def test_session_switch_keeps_sim_clock_from_going_backward() -> None:
    bridge = SimBridge()
    shot = {"kind": "heart_shot", "track_id": 3, "time_sec": 1.2, "actor_id": None}

    bridge.update_from_browser(make_telemetry(125.0), True, [], session_id="tab-a")
    bridge.update_from_browser(make_telemetry(0.2), True, [], session_id="tab-b")
    after_switch_sec = bridge.latest_telemetry().time_sec
    bridge.update_from_browser(make_telemetry(1.2), True, [shot], session_id="tab-b")

    assert after_switch_sec == pytest.approx(125.0)
    assert bridge.latest_telemetry().time_sec == pytest.approx(126.0)
    assert bridge.drain_heart_shots()[0]["time_sec"] == pytest.approx(126.0)
    assert bridge.operator_link_status()[1] == pytest.approx(126.0)


def test_frame_timestamps_use_corrected_clock() -> None:
    bridge = SimBridge()
    bridge.update_from_browser(make_telemetry(50.0), True, [], session_id="tab-a")
    bridge.update_from_browser(make_telemetry(0.0), True, [], session_id="tab-b")
    frame = BrowserFrame(seq=0, timestamp_sec=0.5, jpeg_bytes=JPEG_BYTES, telemetry=make_telemetry(0.5), received_at_sec=0.0)

    bridge.put_frame(frame, session_id="tab-b")
    stored = bridge.wait_frame(None, timeout_sec=0.1)

    assert stored.timestamp_sec == pytest.approx(50.5)
    assert stored.telemetry.time_sec == pytest.approx(50.5)


def test_same_session_clock_restart_is_also_corrected() -> None:
    bridge = SimBridge()
    bridge.update_from_browser(make_telemetry(80.0), True, [])
    bridge.update_from_browser(make_telemetry(0.0), True, [])  # 같은 익명 세션의 새로고침

    assert bridge.latest_telemetry().time_sec == pytest.approx(80.0)


def test_small_reordering_within_session_is_not_treated_as_restart() -> None:
    bridge = SimBridge()
    bridge.update_from_browser(make_telemetry(10.1), True, [])
    bridge.update_from_browser(make_telemetry(10.05), True, [])  # 프레임·텔레메트리 POST 의 작은 뒤섞임

    assert bridge.latest_telemetry().time_sec == pytest.approx(10.05)


# --- B. MissionRunner 링크 일시정지 ---


class LinkScript:
    """sim 시각 disconnect_at_sec 에 링크를 끊고, 벽시계 틱 down_ticks 동안 페이지를 멈췄다가 다시 붙인다."""

    def __init__(self, flight, disconnect_at_sec: float, down_ticks: int) -> None:
        self._flight = flight
        self._disconnect_at_sec = disconnect_at_sec
        self._down_ticks = down_ticks
        self.is_up = True
        self.waited_ticks = 0
        self.commands_at_disconnect: int | None = None
        self.commands_at_reconnect: int | None = None

    def advance(self, dt_sec: float) -> None:
        if not self.is_up:
            # 끊긴 동안 페이지 물리도 멈춘다 — 텔레메트리(=sim 시계)가 오지 않는다
            self.waited_ticks += 1
            if self.waited_ticks >= self._down_ticks:
                self.is_up = True
                self.commands_at_reconnect = len(self._flight.commands_received)
            return

        self._flight.advance(dt_sec)
        if self.commands_at_disconnect is None and self._flight.now_sec >= self._disconnect_at_sec:
            self.is_up = False
            self.commands_at_disconnect = len(self._flight.commands_received)


def run_mock_mission(clock_wrapper=None, link_script_args=None, safety=None, mission_log=None):
    defaults = ParamDefaults()
    settings = build_rule_mode_settings(defaults, target_upper_color="red")
    flight, frame_source = run_mission.build_mock_world()
    link = None if link_script_args is None else LinkScript(flight, *link_script_args)

    def clock() -> float:
        return flight.now_sec if clock_wrapper is None else clock_wrapper(flight.now_sec)

    runner = MissionRunner(
        flight_control=flight,
        frame_source=frame_source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=RuleSelector(defaults),
        param_defaults=defaults,
        settings=settings,
        clock=clock,
        advance=flight.advance if link is None else link.advance,
        config=MissionConfig(max_mission_sec=600.0),
        operator=ScriptedOperator(),
        safety=safety,
        mission_log=mission_log or MissionLog(),
        is_link_up=None if link is None else (lambda: link.is_up),
    )

    return runner.run(), flight, link


def get_mission_events(mission_log: MissionLog) -> list[str]:
    return [record["event"] for record in mission_log.records if record["kind"] == KIND_MISSION]


@pytest.mark.parametrize("down_ticks", [10, 20_000], ids=["one_second", "over_half_an_hour"])
def test_link_loss_pauses_mission_and_resumes_without_abort(down_ticks: int) -> None:
    mission_log = MissionLog()

    result, _, link = run_mock_mission(link_script_args=(20.0, down_ticks), mission_log=mission_log)

    assert result.end_reason == END_LANDED
    assert result.end_reason != END_ABORTED
    assert link.waited_ticks == down_ticks
    # 끊긴 동안 명령을 하나도 보내지 않았다
    assert link.commands_at_reconnect == link.commands_at_disconnect
    assert get_mission_events(mission_log) == ["paused", "resumed", "end"]  # start 는 run_mission 이 남긴다


def test_link_pause_logs_waiting_line_periodically(caplog) -> None:
    caplog.set_level("INFO", logger="modules.drone.mission_loop")

    run_mock_mission(link_script_args=(20.0, 250))  # 25 s 끊김, 10 s 마다 한 줄

    waiting_lines = [record for record in caplog.records if "waiting for simulator reconnect" in record.getMessage()]
    assert len(waiting_lines) == 2


def test_long_link_loss_does_not_count_against_max_flight_time() -> None:
    """끊긴 동안 sim 시계가 멈추므로 max_flight_sec 도 흐르지 않는다 — 재연결 직후 강제 복귀하지 않는다."""
    mission_log = MissionLog()

    result, _, _ = run_mock_mission(link_script_args=(20.0, 20_000), mission_log=mission_log)

    safety_reasons = [record["reason"] for record in mission_log.records if record["kind"] == KIND_SAFETY]
    assert "max_flight_time" not in safety_reasons
    assert result.forced_return is False


def test_should_abort_still_ends_a_paused_mission() -> None:
    """외부 중단 훅은 그대로 — 끊김 대기 중에도 should_abort 가 참이면 aborted."""
    defaults = ParamDefaults()
    flight, frame_source = run_mission.build_mock_world()
    ticks = []

    runner = MissionRunner(
        flight_control=flight, frame_source=frame_source, aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=RuleSelector(defaults), param_defaults=defaults,
        settings=build_rule_mode_settings(defaults), clock=lambda: flight.now_sec,
        advance=lambda dt_sec: ticks.append(dt_sec), operator=ScriptedOperator(),
        should_abort=lambda: len(ticks) >= 5, is_link_up=lambda: False,
    )

    assert runner.run().end_reason == END_ABORTED


# --- B. 역행 시계 ---


class RecordingSafety(SafetyMonitor):
    def __init__(self, config: SafetyConfig) -> None:
        super().__init__(config)
        self.now_list: list[float] = []
        self.elapsed_list: list[float] = []

    def check(self, now_sec, telemetry, flight_elapsed_sec, link):
        self.now_list.append(now_sec)
        self.elapsed_list.append(flight_elapsed_sec)
        return super().check(now_sec, telemetry, flight_elapsed_sec, link)


def test_backward_clock_never_reaches_safety_or_mission_time() -> None:
    defaults = ParamDefaults()
    settings = build_rule_mode_settings(defaults, target_upper_color="red")
    safety = RecordingSafety(SafetyConfig(search_area=settings.search_area, max_flight_sec=600.0))

    # sim 30 s 에 시계가 0.5 s 로 되감긴다(새로고침 흉내) — 그 뒤로는 되감긴 축으로 흐른다
    def rewinding_clock(sim_sec: float) -> float:
        return sim_sec if sim_sec < 30.0 else sim_sec - 29.5

    mission_log = MissionLog()
    result, _, _ = run_mock_mission(clock_wrapper=rewinding_clock, safety=safety, mission_log=mission_log)

    assert safety.now_list == sorted(safety.now_list)
    assert safety.elapsed_list == sorted(safety.elapsed_list)
    assert result.elapsed_sec >= 30.0
    log_times = [record["t"] for record in mission_log.records]
    assert log_times == sorted(log_times)
    safety_reasons = [record["reason"] for record in mission_log.records if record["kind"] == KIND_SAFETY]
    assert "gps_jump" not in safety_reasons


# --- C. 서버 상주 (run_mission --sim browser) ---


HOME_GPS = ned_to_gps(0.0, 0.0, 0.0)


class ResidentBridge:
    """임무가 끝나면 페이지가 MANUAL 을 몇 번 보이다가 다시 AI 로 바꾸는 가짜 브리지."""

    def __init__(self) -> None:
        self.manual_reads_left = 0
        self.status_list: list[dict] = []

    @property
    def ai_mode(self) -> bool:
        if self.manual_reads_left > 0:
            self.manual_reads_left -= 1
            return False
        return True

    def is_connected(self) -> bool:
        return True

    def operator_link_status(self):
        return (True, None)

    def set_mission_status(self, status: dict) -> None:
        self.status_list.append(status)

    def drain_heart_shots(self) -> list[dict]:
        return []

    def publish_pipeline_message(self, kind: str, entry: dict) -> None:
        pass


class FakeServer:
    def __init__(self) -> None:
        self.closed = False

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        self.closed = True


class FakeFlightControl:
    def get_telemetry(self) -> Telemetry:
        return Telemetry(
            time_sec=0.0, north_m=0.0, east_m=0.0, down_m=0.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
            gimbal_pitch_deg=0.0, gimbal_yaw_deg=0.0, battery_pct=100.0, flight_mode="landed", gps=HOME_GPS,
        )


class FakeDetectionSource:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TwoMissionRunnerFactory:
    """첫 임무는 착륙으로 끝내고 페이지가 MANUAL 로 갔다 오게 한다. 두 번째 임무에서 Ctrl-C."""

    def __init__(self, bridge: ResidentBridge) -> None:
        self._bridge = bridge
        self.kwargs_list: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_list.append(kwargs)
        return self

    def run(self) -> MissionRunResult:
        if len(self.kwargs_list) >= 2:
            raise KeyboardInterrupt

        self._bridge.manual_reads_left = 3
        return MissionRunResult(
            end_reason=END_LANDED, selections=(), mode_results=(), final_flight_mode="landed",
            elapsed_sec=1.0, forced_return=False,
        )


def test_browser_stays_up_and_starts_new_mission_on_second_ai_switch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    bridge = ResidentBridge()
    server = FakeServer()
    detection_source = FakeDetectionSource()
    components = BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: server,
        browser_flight_control=lambda bridge: FakeFlightControl(),
        browser_detection_source=lambda bridge, yolo_factory: detection_source,
    )
    yolo_factory_calls: list[str] = []
    runner_factory = TwoMissionRunnerFactory(bridge)
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_mission, "create_yolo_factory", lambda kind, device, url: yolo_factory_calls.append(kind))
    monkeypatch.setattr(run_mission, "LatestFrameReader", lambda source: None)
    monkeypatch.setattr(run_mission, "MissionRunner", runner_factory)
    monkeypatch.setattr(run_mission, "AI_MODE_POLL_SEC", 0.0)

    exit_code = run_mission.main(["--sim", "browser", "--log-dir", str(tmp_path)])

    assert exit_code == run_mission.EXIT_INTERRUPTED
    assert len(runner_factory.kwargs_list) == 2
    assert bridge.manual_reads_left == 0  # MANUAL 을 본 뒤에야 두 번째 임무가 시작됐다
    assert yolo_factory_calls == ["local"]  # YOLO 는 임무 사이에 재사용
    assert runner_factory.kwargs_list[0]["decision_model"] is not runner_factory.kwargs_list[1]["decision_model"]
    log_files = sorted(tmp_path.glob("*.jsonl"))
    assert len(log_files) == 2  # 임무마다 새 로그 파일
    for log_file in log_files:
        first_record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        assert (first_record["kind"], first_record["event"]) == ("mission", "start")
    assert server.closed
    assert detection_source.closed


def test_browser_once_flag_keeps_single_run_exit_code(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    bridge = ResidentBridge()
    components = BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: FakeServer(),
        browser_flight_control=lambda bridge: FakeFlightControl(),
        browser_detection_source=lambda bridge, yolo_factory: FakeDetectionSource(),
    )
    runner_factory = TwoMissionRunnerFactory(bridge)
    monkeypatch.setattr(run_mission, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_mission, "create_yolo_factory", lambda kind, device, url: None)
    monkeypatch.setattr(run_mission, "LatestFrameReader", lambda source: None)
    monkeypatch.setattr(run_mission, "MissionRunner", runner_factory)

    exit_code = run_mission.main(["--sim", "browser", "--once", "--log-dir", str(tmp_path)])

    assert exit_code == run_mission.EXIT_OK
    assert len(runner_factory.kwargs_list) == 1
    assert runner_factory.kwargs_list[0]["should_abort"] is None
    assert runner_factory.kwargs_list[0]["is_link_up"] is not None
