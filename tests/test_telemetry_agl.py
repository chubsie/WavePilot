"""Telemetry 아래쪽 거리(AGL) 필드 — 왕복, 하위 호환, 검증, mock, 서버 파싱 (D-09, RM-05)."""

import http.client
import json
from collections.abc import Iterator

import pytest

from modules.flight_control.interface import Telemetry, ned_to_gps
from modules.flight_control.mock import MockFlightControl
from modules.simulation.browser.bridge import SimBridge
from modules.simulation.browser.server import parse_telemetry_post, start_sim_server

AGL_M = 12.5


class ZeroClock:
    now_sec = 0.0


def make_telemetry(height_above_ground_m: float | None = AGL_M) -> Telemetry:
    return Telemetry(
        time_sec=1.5,
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
        height_above_ground_m=height_above_ground_m,
    )


def make_dict_with_agl(value: object) -> dict:
    data = make_telemetry().to_dict()
    data["height_above_ground_m"] = value

    return data


# --- 왕복·하위 호환 ---


def test_agl_round_trips_through_dict() -> None:
    telemetry = make_telemetry(12.5)

    data = telemetry.to_dict()

    assert data["height_above_ground_m"] == 12.5
    assert Telemetry.from_dict(data) == telemetry


def test_to_dict_always_emits_agl_key_even_when_none() -> None:
    data = make_telemetry(None).to_dict()

    assert "height_above_ground_m" in data
    assert data["height_above_ground_m"] is None


def test_from_dict_without_agl_key_gives_none() -> None:
    data = make_telemetry().to_dict()
    del data["height_above_ground_m"]

    assert Telemetry.from_dict(data).height_above_ground_m is None


def test_from_dict_with_null_agl_gives_none() -> None:
    assert Telemetry.from_dict(make_dict_with_agl(None)).height_above_ground_m is None


def test_from_dict_converts_int_agl_to_float() -> None:
    agl = Telemetry.from_dict(make_dict_with_agl(0)).height_above_ground_m

    assert agl == 0.0
    assert isinstance(agl, float)


def test_from_dict_accepts_zero_agl() -> None:
    assert Telemetry.from_dict(make_dict_with_agl(0.0)).height_above_ground_m == 0.0


# --- 검증 ---


@pytest.mark.parametrize(
    "bad_value",
    [-0.5, float("nan"), float("inf"), float("-inf"), True, "12"],
    ids=["negative", "nan", "inf", "minus-inf", "bool", "string"],
)
def test_from_dict_rejects_invalid_agl(bad_value: object) -> None:
    with pytest.raises(ValueError, match="height_above_ground_m"):
        Telemetry.from_dict(make_dict_with_agl(bad_value))


# --- mock ---


def test_mock_fills_agl_with_hover_altitude() -> None:
    mock = MockFlightControl(ZeroClock(), hover_altitude_m=40.0)

    telemetry = mock.get_telemetry()

    assert telemetry.height_above_ground_m == 40.0
    assert telemetry.down_m + telemetry.height_above_ground_m == 0.0


# --- 서버 ---


@pytest.fixture
def sim_server() -> Iterator[tuple[SimBridge, int]]:
    bridge = SimBridge()
    server = start_sim_server(bridge, port=0)

    yield bridge, server.server_address[1]

    bridge.close()
    server.shutdown()
    server.server_close()


def post(port: int, path: str, body: bytes, headers: dict) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def post_frame_with_telemetry_header(port: int, telemetry_dict: dict) -> int:
    headers = {"Content-Type": "image/jpeg", "X-Telemetry": json.dumps(telemetry_dict)}

    return post(port, "/sim/frame?seq=1", b"\xff\xd8fake", headers)


def test_parse_telemetry_post_preserves_agl() -> None:
    payload = {"telemetry": make_telemetry().to_dict(), "ai_mode": True, "events": []}

    telemetry, _, _ = parse_telemetry_post(payload)

    assert telemetry.height_above_ground_m == 12.5


def test_parse_telemetry_post_without_agl_key_gives_none() -> None:
    telemetry_dict = make_telemetry().to_dict()
    del telemetry_dict["height_above_ground_m"]
    payload = {"telemetry": telemetry_dict, "ai_mode": True, "events": []}

    telemetry, _, _ = parse_telemetry_post(payload)

    assert telemetry.height_above_ground_m is None


def test_telemetry_post_preserves_agl_in_bridge(sim_server) -> None:
    bridge, port = sim_server
    body = json.dumps({"telemetry": make_telemetry().to_dict(), "ai_mode": True, "events": []}).encode()

    status = post(port, "/sim/telemetry", body, {"Content-Type": "application/json"})

    assert status == 200
    assert bridge.latest_telemetry().height_above_ground_m == 12.5


def test_frame_header_preserves_agl_in_frame_telemetry(sim_server) -> None:
    bridge, port = sim_server

    status = post_frame_with_telemetry_header(port, make_telemetry().to_dict())

    frame = bridge.wait_frame(None, 0.5)
    assert status == 204
    assert frame is not None
    assert frame.telemetry.height_above_ground_m == 12.5


def test_frame_header_with_negative_agl_is_rejected(sim_server) -> None:
    bridge, port = sim_server

    status = post_frame_with_telemetry_header(port, make_dict_with_agl(-1))

    assert status == 400
    assert bridge.wait_frame(None, 0.1) is None
