"""mission_demo ⇄ Python YOLO 파이프라인 연결 — 정적 서빙, detections seq, BridgeCommLink, run_scenario --operator.

서버는 실제 포트 0 으로 띄우고(test_sim_server 와 같은 방식), YOLO 는 가짜 모델로 돌린다 — cv2·ultralytics 없이 통과한다.
"""

import http.client
import json
from collections.abc import Iterator

import pytest

from modules.comm.interface import CommLink, Message
from modules.drone import run_scenario
from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import BrowserFrame, SimBridge
from modules.simulation.browser.comm_link import BridgeCommLink
from modules.simulation.browser.detection_source import BrowserDetectionSource
from modules.simulation.browser.server import start_sim_server
from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

SHORT_POLL_SEC = 0.05
PERSON_BOX = ([100.0, 200.0, 150.0, 300.0], 0, 0.9, 3)
SENT_AT = "2026-10-11T12:00:00.000Z"


def _make_telemetry(time_sec: float = 1.5) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=0.0, east_m=0.0, down_m=-30.0, roll_deg=0.0, pitch_deg=0.0,
        yaw_deg=0.0, gimbal_pitch_deg=-45.0, gimbal_yaw_deg=0.0, battery_pct=90.0, flight_mode="hover",
    )


def _make_telemetry_post() -> dict:
    return {"telemetry": _make_telemetry().to_dict(), "ai_mode": True, "events": []}


def _make_message(message_type: str, seq: int = 4) -> Message:
    return Message(type=message_type, seq=seq, sent_at=SENT_AT, payload={"kind": message_type})


@pytest.fixture
def sim_server() -> Iterator[tuple[SimBridge, int]]:
    bridge = SimBridge()
    server = start_sim_server(bridge, port=0)

    yield bridge, server.server_address[1]

    bridge.close()
    server.shutdown()
    server.server_close()


def request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        # 표준 핸들러는 "Content-type" 으로 보낸다 — 대소문자 무시로 읽게 키를 소문자로 맞춘다
        return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def post_telemetry(port: int) -> dict:
    body = json.dumps(_make_telemetry_post()).encode("utf-8")
    status, _, raw = request(port, "POST", "/sim/telemetry", body=body, headers={"Content-Type": "application/json"})

    assert status == 200
    return json.loads(raw)


# --- A1 정적 서빙 ---


def test_serves_mission_demo_index(sim_server) -> None:
    _, port = sim_server

    status, headers, body = request(port, "GET", "/mission_demo/")

    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b"../visualizer/three.min.js" in body


def test_mission_demo_without_slash_redirects_to_directory(sim_server) -> None:
    _, port = sim_server

    status, headers, _ = request(port, "GET", "/mission_demo")

    assert status == 301
    assert headers["location"] == "/mission_demo/"


def test_serves_visualizer_mount_with_javascript_mime(sim_server) -> None:
    _, port = sim_server

    status, headers, body = request(port, "GET", "/visualizer/three.min.js")

    assert status == 200
    assert headers["content-type"] == "text/javascript"
    assert body


def test_mission_demo_module_script_and_glb_mime(sim_server) -> None:
    _, port = sim_server

    js_status, js_headers, _ = request(port, "HEAD", "/mission_demo/app.js")
    glb_status, glb_headers, _ = request(port, "HEAD", "/mission_demo/assets/urban.glb")

    assert js_status == 200
    assert js_headers["content-type"] == "text/javascript"
    assert glb_status == 200
    assert glb_headers["content-type"] == "model/gltf-binary"


def test_root_still_serves_visualizer_files(sim_server) -> None:
    _, port = sim_server

    page_status, _, _ = request(port, "GET", "/drone_camera.html")
    link_status, _, _ = request(port, "GET", "/sim_link.js")

    assert page_status == 200
    assert link_status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/../pyproject.toml",
        "/mission_demo/../../pyproject.toml",
        "/mission_demo/%2e%2e/%2e%2e/pyproject.toml",
        "/mission_demo/..%2f..%2fpyproject.toml",
        "/visualizer/%2e%2e/%2e%2e/pyproject.toml",
        "/mission_demo/../visualizer/../../pyproject.toml",
    ],
)
def test_static_mounts_do_not_escape_tools_dirs(sim_server, path: str) -> None:
    _, port = sim_server

    status, _, _ = request(port, "GET", path)

    assert status == 404


# --- A2 detections seq ---


def test_read_frame_publishes_browser_frame_seq(sim_server) -> None:
    bridge, port = sim_server
    model = FakeYoloModel([FakeResult(make_boxes([PERSON_BOX]))])

    def factory(items):
        return create_jpeg_yolo_source(items, model=model, decode=lambda jpeg: "image")

    bridge.put_frame(BrowserFrame(seq=42, timestamp_sec=0.1, jpeg_bytes=b"jpeg", telemetry=_make_telemetry(0.1), received_at_sec=0.0))
    source = BrowserDetectionSource(bridge, yolo_factory=factory, poll_timeout_sec=SHORT_POLL_SEC)

    frame = source.read_frame()
    response = post_telemetry(port)

    assert frame is not None
    assert response["detections"] == {**frame.to_dict(), "seq": 42}
    # Frame 인터페이스 dict 자체는 바뀌지 않는다
    assert "seq" not in frame.to_dict()
    assert bridge.latest_detections() == frame.to_dict()


def test_detections_without_seq_are_sent_unchanged(sim_server) -> None:
    bridge, port = sim_server
    detections = {"frame_index": 0, "timestamp_sec": 1.5, "detections": []}
    bridge.publish_detections(detections, 12.0)

    response = post_telemetry(port)

    assert response["detections"] == detections


# --- A3 BridgeCommLink → pipeline 필드 ---


def test_bridge_comm_link_satisfies_comm_link_protocol() -> None:
    assert isinstance(BridgeCommLink(SimBridge()), CommLink)


def test_telemetry_response_has_empty_pipeline_before_any_message(sim_server) -> None:
    _, port = sim_server

    response = post_telemetry(port)

    assert response["pipeline"] == {"state": None, "report": None}
    assert set(response) == {"commands", "detections", "status", "mission", "pipeline"}
    assert set(response["status"]) == {"frame_fps", "yolo_latency_ms"}


def test_bridge_comm_link_sends_latest_state_and_report_to_page(sim_server) -> None:
    bridge, port = sim_server
    link = BridgeCommLink(bridge)

    link.send(_make_message("state", seq=1))
    link.send(_make_message("state", seq=2))
    link.send(_make_message("report", seq=3))
    response = post_telemetry(port)

    assert response["pipeline"] == {
        "state": {"seq": 2, "sent_at": SENT_AT, "payload": {"kind": "state"}},
        "report": {"seq": 3, "sent_at": SENT_AT, "payload": {"kind": "report"}},
    }


def test_bridge_comm_link_drops_telemetry_messages() -> None:
    bridge = SimBridge()
    link = BridgeCommLink(bridge)

    link.send(_make_message("telemetry"))

    assert bridge.pipeline_messages() == {"state": None, "report": None}


def test_bridge_comm_link_receive_never_blocks_and_status_follows_bridge(sim_server) -> None:
    bridge, port = sim_server
    link = BridgeCommLink(bridge)

    before = link.link_status()
    post_telemetry(port)
    after = link.link_status()
    link.close()

    assert link.receive(timeout_sec=0.0) is None
    assert link.receive() is None
    assert before.connected is False
    assert after.connected is True
    assert after.peer == "browser"


def test_publish_pipeline_message_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        SimBridge().publish_pipeline_message("telemetry", {})


def test_pipeline_messages_returns_copies() -> None:
    bridge = SimBridge()
    bridge.publish_pipeline_message("state", {"seq": 1})

    bridge.pipeline_messages()["state"]["seq"] = 99

    assert bridge.pipeline_messages()["state"] == {"seq": 1}


# --- A4 run_scenario --operator ---


def test_operator_defaults_to_tcp_and_accepts_browser() -> None:
    parser = run_scenario.build_arg_parser()

    default_args = parser.parse_args(["--sim", "browser", "--operator-port", "1"])
    browser_args = parser.parse_args(["--sim", "browser", "--operator", "browser"])

    assert default_args.operator == "tcp"
    assert browser_args.operator == "browser"
    assert browser_args.operator_port is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--sim", "browser", "--operator", "radio"])


def test_operator_browser_requires_sim_browser() -> None:
    with pytest.raises(SystemExit) as error:
        run_scenario.main(["--scenario", "modules/simulation/scenarios/checkpoint.toml", "--operator", "browser"])

    assert error.value.code == run_scenario.EXIT_USAGE


def test_operator_tcp_still_requires_operator_port() -> None:
    with pytest.raises(SystemExit) as error:
        run_scenario.main(["--sim", "browser"])

    assert error.value.code == run_scenario.EXIT_USAGE


class _FakeServer:
    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        pass


class _FakeDetectionSource:
    def __init__(self, bridge, yolo_factory) -> None:
        self.bridge = bridge

    def close(self) -> None:
        pass


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, bridge: SimBridge, captured: dict) -> None:
    def capture_pipeline(args, detection_source, flight_control, pace, run_reports_in_background, label, **kwargs):
        captured.update(kwargs)
        return run_scenario.EXIT_OK

    components = run_scenario.BrowserComponents(
        sim_bridge=lambda: bridge,
        start_sim_server=lambda bridge, host, port: _FakeServer(),
        browser_flight_control=lambda bridge: object(),
        browser_detection_source=_FakeDetectionSource,
    )
    monkeypatch.setattr(run_scenario, "_load_browser_components", lambda: components)
    monkeypatch.setattr(run_scenario, "create_yolo_factory", lambda kind, device, url: None)
    monkeypatch.setattr(run_scenario, "_wait_for_simulator", lambda bridge, timeout_sec: True)
    monkeypatch.setattr(run_scenario, "_run_pipeline", capture_pipeline)


def test_browser_operator_wires_bridge_comm_link_and_logs_mission_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = SimBridge()
    captured: dict = {}
    _install_fake_browser(monkeypatch, bridge, captured)
    args = run_scenario.build_arg_parser().parse_args(["--sim", "browser", "--operator", "browser", "--sim-port", "9001"])

    with caplog.at_level("INFO", logger="modules.drone.run_scenario"):
        exit_code = run_scenario._run_browser_sim(args)

    link = captured["link"]
    link.send(_make_message("report"))
    assert exit_code == run_scenario.EXIT_OK
    assert isinstance(link, BridgeCommLink)
    assert bridge.pipeline_messages()["report"]["payload"] == {"kind": "report"}
    assert "http://127.0.0.1:9001/mission_demo/" in caplog.text


def test_tcp_operator_keeps_default_link(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    _install_fake_browser(monkeypatch, SimBridge(), captured)
    args = run_scenario.build_arg_parser().parse_args(["--sim", "browser", "--operator-port", "1"])

    exit_code = run_scenario._run_browser_sim(args)

    assert exit_code == run_scenario.EXIT_OK
    assert "link" not in captured
