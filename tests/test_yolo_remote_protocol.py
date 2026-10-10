"""원격 YOLO 와이어 프로토콜 검사 (SIM-05, D-01) — 응답 왕복과 입력 검증."""

import json

import pytest

from modules.yolo_detection.interface.detection import Detection, Frame
from modules.yolo_detection.remote.protocol import (
    ServerTiming,
    build_detect_response,
    build_error_response,
    format_timestamp_header,
    parse_detect_response,
    parse_timestamp_header,
)

FRAME = Frame(
    frame_id=3,
    timestamp_sec=12.5,
    image_width=1280,
    image_height=720,
    detections=(
        Detection(class_id=0, class_name="person", confidence=0.9, xyxy=(10.0, 20.0, 30.0, 40.0), track_id=7),
        Detection(class_id=2, class_name="car", confidence=0.5, xyxy=(100.0, 110.0, 200.0, 210.0), track_id=None),
    ),
)


def _build_valid_response() -> dict:
    return build_detect_response(FRAME.to_dict(), 1.0, 2.0, 3.5)


def test_build_detect_response_shape() -> None:
    response = _build_valid_response()

    assert response == {"frame": FRAME.to_dict(), "server_ms": {"decode": 1.0, "infer": 2.0, "total": 3.5}}


@pytest.mark.parametrize("encode", [lambda text: text, lambda text: text.encode("utf-8")], ids=["str", "bytes"])
def test_parse_detect_response_round_trips(encode) -> None:
    body = encode(json.dumps(_build_valid_response()))

    frame, timing = parse_detect_response(body)

    assert frame == FRAME
    assert frame.detections[1].track_id is None
    assert timing == ServerTiming(decode_ms=1.0, infer_ms=2.0, total_ms=3.5)


def test_parse_detect_response_accepts_int_timings() -> None:
    response = build_detect_response(FRAME.to_dict(), 0, 1, 2)

    _, timing = parse_detect_response(json.dumps(response))

    assert timing == ServerTiming(decode_ms=0.0, infer_ms=1.0, total_ms=2.0)


def _with_timing(key: str, value: object) -> dict:
    response = _build_valid_response()
    response["server_ms"][key] = value
    return response


def _without(key: str) -> dict:
    response = _build_valid_response()
    del response[key]
    return response


def _without_timing(key: str) -> dict:
    response = _build_valid_response()
    del response["server_ms"][key]
    return response


def _with_bad_frame() -> dict:
    response = _build_valid_response()
    del response["frame"]["frame_id"]
    return response


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{not json", id="invalid-json"),
        pytest.param(b"\xff\xfe\x00", id="invalid-utf8"),
        pytest.param(json.dumps([1, 2]), id="top-level-list"),
        pytest.param(json.dumps(_without("frame")), id="missing-frame"),
        pytest.param(json.dumps(_without("server_ms")), id="missing-server-ms"),
        pytest.param(json.dumps({**_build_valid_response(), "server_ms": [1, 2, 3]}), id="server-ms-not-dict"),
        pytest.param(json.dumps(_without_timing("infer")), id="missing-infer"),
        pytest.param(json.dumps(_with_timing("decode", True)), id="bool-timing"),
        pytest.param(json.dumps(_with_timing("total", "3.5")), id="str-timing"),
        pytest.param(json.dumps(_with_timing("infer", -1.0)), id="negative-timing"),
        pytest.param(json.dumps(_with_timing("total", float("nan"))), id="nan-timing"),
        pytest.param(json.dumps(_with_bad_frame()), id="invalid-frame"),
    ],
)
def test_parse_detect_response_rejects_invalid(body: str | bytes) -> None:
    with pytest.raises(ValueError):
        parse_detect_response(body)


def test_timestamp_header_round_trips() -> None:
    header = format_timestamp_header(12.5)

    assert header == "12.5"
    assert parse_timestamp_header(header) == 12.5


def test_timestamp_header_keeps_exact_float() -> None:
    timestamp_sec = 0.1 + 0.2

    assert parse_timestamp_header(format_timestamp_header(timestamp_sec)) == timestamp_sec


@pytest.mark.parametrize("value", [None, "", "  ", "abc", "nan", "inf", "-inf", "-1"])
def test_parse_timestamp_header_rejects_invalid(value: str | None) -> None:
    with pytest.raises(ValueError):
        parse_timestamp_header(value)


def test_build_error_response() -> None:
    assert build_error_response("x") == {"error": "x"}
