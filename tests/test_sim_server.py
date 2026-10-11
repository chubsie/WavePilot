"""시뮬레이터 HTTP 서버 — 정적 파일, POST /sim/frame, POST /sim/telemetry, GET /sim/scenario 와이어 프로토콜 (SIM-02, X-01).

Phase 6: 텔레메트리 events 의 heart_shot(발마다 맞은 배우, D-10), 비교 시나리오 GET(D-05), sim_link.js 헬퍼 node 테스트.
"""

import http.client
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from modules.flight_control.interface import GoTo, Telemetry, ned_to_gps
from modules.simulation.browser.bridge import SimBridge
from modules.simulation.browser.server import (
    MAX_FRAME_BYTES,
    MAX_TELEMETRY_BYTES,
    VISUALIZER_DIR,
    parse_telemetry_post,
    start_sim_server,
)

JPEG_BYTES = b"\xff\xd8fake"
SIM_LINK_JS = VISUALIZER_DIR / "sim_link.js"
GOTO_COMMAND = GoTo(north_m=20.0, east_m=0.0, altitude_m=15.0)


def make_telemetry(time_sec: float = 1.5) -> Telemetry:
    return Telemetry(
        time_sec=time_sec,
        north_m=10.0,
        east_m=-5.0,
        down_m=-12.0,
        roll_deg=1.0,
        pitch_deg=-2.0,
        yaw_deg=45.0,
        gimbal_pitch_deg=-30.0,
        gimbal_yaw_deg=0.0,
        battery_pct=87.5,
        flight_mode="hover",
        gps=ned_to_gps(10.0, -5.0, -12.0),
    )


def make_telemetry_post(**overrides: object) -> dict:
    body = {
        "telemetry": make_telemetry().to_dict(),
        "ai_mode": True,
        "events": [{"kind": "goal_reached", "command_id": 1, "time_sec": 1.2}],
    }
    body.update(overrides)
    return body


def make_heart_shot(**overrides: object) -> dict:
    shot = {"kind": "heart_shot", "track_id": 3, "time_sec": 4.5, "actor_id": "red_1"}
    shot.update(overrides)
    return shot


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
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def request_headers_only(port: int, path: str, headers: dict) -> int:
    """본문 없이 헤더만 보낸다 — 서버가 본문을 읽기 전에 거절하는지 본다."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def post_frame(port: int, seq: str | None = "7", body: bytes = JPEG_BYTES, **header_overrides: str | None) -> int:
    headers = {"Content-Type": "image/jpeg", "X-Telemetry": json.dumps(make_telemetry().to_dict())}
    headers.update(header_overrides)
    headers = {name: value for name, value in headers.items() if value is not None}
    path = "/sim/frame" if seq is None else f"/sim/frame?seq={seq}"

    status, _, _ = request(port, "POST", path, body=body, headers=headers)
    return status


def post_telemetry(port: int, body: object, content_type: str = "application/json"):
    raw_body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return request(port, "POST", "/sim/telemetry", body=raw_body, headers={"Content-Type": content_type})


# --- 정적 파일 ---


def test_serves_drone_camera_page(sim_server) -> None:
    _, port = sim_server

    status, _, body = request(port, "GET", "/drone_camera.html")

    assert status == 200
    assert b"<html" in body.lower()


def test_static_files_do_not_escape_visualizer_dir(sim_server) -> None:
    _, port = sim_server

    status, _, _ = request(port, "GET", "/../pyproject.toml")

    assert status == 404


def test_unknown_post_path_returns_404(sim_server) -> None:
    _, port = sim_server

    status, _, _ = request(port, "POST", "/sim/unknown", body=b"{}", headers={"Content-Type": "application/json"})

    assert status == 404


# --- POST /sim/frame ---


def test_frame_post_fills_latest_frame_slot(sim_server) -> None:
    bridge, port = sim_server

    status = post_frame(port, seq="7")

    frame = bridge.wait_frame(None, 0.5)
    assert status == 204
    assert frame is not None
    assert frame.seq == 7
    assert frame.jpeg_bytes == JPEG_BYTES
    assert frame.telemetry == make_telemetry()
    assert frame.timestamp_sec == make_telemetry().time_sec


@pytest.mark.parametrize(
    ("seq", "body", "header_overrides"),
    [
        (None, JPEG_BYTES, {}),
        ("-1", JPEG_BYTES, {}),
        ("abc", JPEG_BYTES, {}),
        (str(2**31 + 1), JPEG_BYTES, {}),
        ("7", JPEG_BYTES, {"Content-Type": "text/plain"}),
        ("7", b"", {}),
        ("7", JPEG_BYTES, {"X-Telemetry": None}),
        ("7", JPEG_BYTES, {"X-Telemetry": "not json"}),
        ("7", JPEG_BYTES, {"X-Telemetry": json.dumps({**make_telemetry().to_dict(), "flight_mode": "flying"})}),
    ],
    ids=["missing-seq", "negative-seq", "text-seq", "seq-too-big", "wrong-type", "empty-body",
         "missing-telemetry", "telemetry-not-json", "bad-flight-mode"],
)
def test_invalid_frame_post_is_rejected_without_side_effects(sim_server, seq, body, header_overrides) -> None:
    bridge, port = sim_server

    status = post_frame(port, seq=seq, body=body, **header_overrides)

    assert status == 400
    assert bridge.wait_frame(None, 0.05) is None


def test_oversize_frame_is_rejected_before_reading_body(sim_server) -> None:
    bridge, port = sim_server
    headers = {
        "Content-Type": "image/jpeg",
        "Content-Length": str(MAX_FRAME_BYTES + 1),
        "X-Telemetry": json.dumps(make_telemetry().to_dict()),
    }

    status = request_headers_only(port, "/sim/frame?seq=1", headers)

    assert status == 400
    assert bridge.wait_frame(None, 0.05) is None


# --- POST /sim/telemetry ---


def test_telemetry_post_updates_bridge_and_delivers_commands_once(sim_server) -> None:
    bridge, port = sim_server
    bridge.enqueue_command(GOTO_COMMAND)

    status, headers, body = post_telemetry(port, make_telemetry_post())

    response = json.loads(body)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert response["commands"] == [{"id": 1, **GOTO_COMMAND.to_dict()}]
    assert response["detections"] is None
    assert set(response["status"]) == {"frame_fps", "yolo_latency_ms"}
    assert response["mission"] is None
    assert bridge.latest_telemetry() == make_telemetry()
    assert bridge.operator_link_status()[0] is True
    assert bridge.ai_mode is True
    assert bridge.recent_events() == ({"kind": "goal_reached", "command_id": 1, "time_sec": 1.2},)

    _, _, second_body = post_telemetry(port, make_telemetry_post())

    assert json.loads(second_body)["commands"] == []


def test_telemetry_response_carries_latest_detections(sim_server) -> None:
    bridge, port = sim_server
    detections = {"frame_index": 0, "timestamp_sec": 1.5, "detections": []}
    bridge.publish_detections(detections, 12.0)

    _, _, body = post_telemetry(port, make_telemetry_post())

    response = json.loads(body)
    assert response["detections"] == detections
    assert response["status"]["yolo_latency_ms"] == 12.0


def test_telemetry_post_with_operator_link_false_skips_heartbeat(sim_server) -> None:
    bridge, port = sim_server

    status, _, _ = post_telemetry(port, make_telemetry_post(operator_link=False))

    assert status == 200
    assert bridge.latest_telemetry() == make_telemetry()
    assert bridge.is_connected() is True
    assert bridge.operator_link_status() == (False, None)


def test_telemetry_response_carries_mission_status(sim_server) -> None:
    bridge, port = sim_server
    mission = {
        "mode": "RETURN",
        "reason": "link_lost",
        "link_connected": False,
        "link_last_heartbeat_sec": 12.0,
        "running_mode": "return_home",
        "t": 15.0,
    }
    bridge.set_mission_status(mission)

    _, _, body = post_telemetry(port, make_telemetry_post())

    assert json.loads(body)["mission"] == mission


def test_parse_telemetry_post_returns_operator_link() -> None:
    _, _, _, default_link = parse_telemetry_post(make_telemetry_post())
    _, _, _, cut_link = parse_telemetry_post(make_telemetry_post(operator_link=False))

    assert default_link is True
    assert cut_link is False


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        [],
        make_telemetry_post(ai_mode="yes"),
        {key: value for key, value in make_telemetry_post().items() if key != "telemetry"},
        make_telemetry_post(events={"kind": "goal_reached"}),
        make_telemetry_post(events=[{"kind": "other", "command_id": 1, "time_sec": 1.0}]),
        make_telemetry_post(events=[{"kind": "goal_reached", "command_id": True, "time_sec": 1.0}]),
        make_telemetry_post(operator_link="yes"),
        make_telemetry_post(events=[make_heart_shot(track_id=True)]),
        make_telemetry_post(events=[make_heart_shot(track_id="3")]),
        make_telemetry_post(events=[make_heart_shot(time_sec="4.5")]),
        make_telemetry_post(events=[make_heart_shot(actor_id="")]),
        make_telemetry_post(events=[make_heart_shot(actor_id="a" * 33)]),
        make_telemetry_post(events=[make_heart_shot(actor_id=7)]),
    ],
    ids=["not-json", "json-list", "ai-mode-str", "missing-telemetry", "events-not-list", "event-kind-other",
         "event-id-bool", "operator-link-str", "shot-track-bool", "shot-track-str", "shot-time-str",
         "shot-actor-empty", "shot-actor-long", "shot-actor-int"],
)
def test_invalid_telemetry_post_does_not_drain_commands(sim_server, body) -> None:
    bridge, port = sim_server
    bridge.enqueue_command(GOTO_COMMAND)

    status, _, _ = post_telemetry(port, body)

    assert status == 400
    assert bridge.latest_telemetry() is None
    _, _, valid_body = post_telemetry(port, make_telemetry_post())
    assert json.loads(valid_body)["commands"] == [{"id": 1, **GOTO_COMMAND.to_dict()}]


def test_oversize_telemetry_post_is_rejected_and_keeps_commands(sim_server) -> None:
    bridge, port = sim_server
    bridge.enqueue_command(GOTO_COMMAND)
    headers = {"Content-Type": "application/json", "Content-Length": str(MAX_TELEMETRY_BYTES + 1)}

    status = request_headers_only(port, "/sim/telemetry", headers)

    assert status == 400
    _, _, valid_body = post_telemetry(port, make_telemetry_post())
    assert json.loads(valid_body)["commands"] == [{"id": 1, **GOTO_COMMAND.to_dict()}]


# --- heart_shot 이벤트 (Phase 6 D-10) ---


def test_heart_shot_event_reaches_bridge_queue(sim_server) -> None:
    bridge, port = sim_server

    status, _, _ = post_telemetry(port, make_telemetry_post(events=[make_heart_shot(extra=1)]))

    assert status == 200
    assert bridge.drain_heart_shots() == [make_heart_shot()]
    assert bridge.recent_events() == ()


def test_heart_shot_with_null_actor_is_accepted(sim_server) -> None:
    bridge, port = sim_server

    status, _, _ = post_telemetry(port, make_telemetry_post(events=[make_heart_shot(actor_id=None)]))

    assert status == 200
    assert bridge.drain_heart_shots() == [make_heart_shot(actor_id=None)]


# --- GET /sim/scenario (Phase 6 D-05) ---

BASIC_SCENARIO_TOML = """\
name = "basic"
description = "red soldier and a car"
mission = "missions/compare.toml"
battery_pct = 100.0

[[actors]]
id = "red_1"
model = "soldier"
north_m = 16.0
east_m = -3.0
heading_deg = 90.0
walk_m = 6.0
shirt = "red"
shirt_rgb = [208, 24, 24]

[[actors]]
id = "car_1"
model = "car"
north_m = 25.0
east_m = 9.0
heading_deg = 60.0
"""


@pytest.fixture
def scenario_server(tmp_path: Path) -> Iterator[int]:
    (tmp_path / "basic.toml").write_text(BASIC_SCENARIO_TOML, encoding="utf-8")
    (tmp_path / "broken.toml").write_text("name = [unclosed\n", encoding="utf-8")
    bridge = SimBridge()
    server = start_sim_server(bridge, port=0, scenarios_dir=tmp_path)

    yield server.server_address[1]

    bridge.close()
    server.shutdown()
    server.server_close()


def test_scenario_get_returns_browser_dict(scenario_server) -> None:
    status, headers, body = request(scenario_server, "GET", "/sim/scenario?id=basic")

    scenario = json.loads(body)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert scenario["id"] == "basic"
    assert scenario["actors"][0]["id"] == "red_1"
    assert scenario["actors"][0]["model"] == "Soldier.glb"
    assert scenario["actors"][0]["dx"] == -3.0
    assert scenario["actors"][0]["dz"] == -16.0


@pytest.mark.parametrize(
    "path",
    ["/sim/scenario", "/sim/scenario?id=../pyproject", "/sim/scenario?id=BAD", "/sim/scenario?id=broken"],
    ids=["missing-id", "path-traversal", "bad-pattern", "broken-toml"],
)
def test_scenario_get_rejects_bad_id(scenario_server, path) -> None:
    status, _, _ = request(scenario_server, "GET", path)

    assert status == 400


def test_scenario_get_missing_file_is_404(scenario_server) -> None:
    status, _, _ = request(scenario_server, "GET", "/sim/scenario?id=missing")

    assert status == 404


# --- sim_link.js 헬퍼 (Phase 6 C2, node 로 실행) ---

SIM_LINK_HELPER_SCRIPT = """
const SimLink = require(process.argv[1]);
const autopilot = SimLink.createAutopilot();
for (let n = 0; n < SimLink.MAX_PENDING_EVENTS + 2; n++) autopilot.recordEvent({ kind: 'heart_shot', n });
const taken = autopilot.takeEvents();
const child = { userData: {}, parent: { userData: { actorId: 'red_1' }, parent: null } };
console.log(JSON.stringify({
  scenario: [SimLink.parseScenarioId('?scenario=basic'), SimLink.parseScenarioId(''),
             SimLink.parseScenarioId('?scenario='), SimLink.parseScenarioId('?battery=50')],
  autostart: [SimLink.parseAutostart('?autostart=ai'), SimLink.parseAutostart('?autostart=AI'),
              SimLink.parseAutostart('?autostart=x'), SimLink.parseAutostart('')],
  actor: [SimLink.findActorId(child), SimLink.findActorId({ userData: {}, parent: null }), SimLink.findActorId(null)],
  maxPending: SimLink.MAX_PENDING_EVENTS,
  takenCount: taken.length,
  firstTaken: taken[0].n,
  takenAgain: autopilot.takeEvents().length,
  maxPerPost: SimLink.MAX_EVENTS_PER_POST,
}));
"""


def test_sim_link_scenario_helpers() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    completed = subprocess.run(
        [node, "-e", SIM_LINK_HELPER_SCRIPT, str(SIM_LINK_JS)],
        timeout=10,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["scenario"] == ["basic", None, None, None]
    assert result["autostart"] == ["ai", None, None, None]
    assert result["actor"] == ["red_1", None, None]
    assert result["takenCount"] == result["maxPending"]
    assert result["firstTaken"] == 2
    assert result["takenAgain"] == 0
    assert result["maxPerPost"] == 32
