"""modules.comm.codec — Message <-> UTF-8 JSON 한 줄 코덱 테스트 (IF-03)."""

import json

import pytest

from modules.comm.codec import MAX_LINE_BYTES, decode_message, encode_message
from modules.comm.interface.comm import Message

SENT_AT = "2026-10-10T12:00:00.000+00:00"

# SKELETON 페이로드 모양 예시 — 4종 각각
TELEMETRY_PAYLOAD = {
    "time_sec": 12.0, "north_m": 0.0, "east_m": 0.0, "down_m": -40.0,
    "roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0,
    "gimbal_pitch_deg": -55.0, "gimbal_yaw_deg": 0.0,
    "battery_pct": 99.8, "flight_mode": "hover",
}
STATE_PAYLOAD = {
    "schema_version": 1,
    "time_sec": 12.3,
    "drone": {"altitude_m": 40.0, "battery_pct": 99.8, "flight_mode": "hover"},
    "tracks": [{"track_id": "T1", "class": "person"}, {"track_id": "T3", "class": "car"}],
    "counts_by_class": {"person": 1, "car": 1},
    "omitted_track_count": 0,
}
REPORT_PAYLOAD = {
    "time_sec": 12.3,
    "decision": {"verdict": "escalate", "reason": "person near vehicle", "answers": []},
    "report": {
        "sentences": [
            {"kind": "observation", "text": "A person stands next to a car.", "track_ids": ["T1", "T3"]},
        ],
        "flags": [],
        "is_grounded": True,
    },
    "report_error": None,
}
COMMAND_PAYLOAD = {"kind": "return_to_home"}


def _make_message(message_type: str, payload: dict, seq: int = 0) -> Message:
    return Message(type=message_type, seq=seq, sent_at=SENT_AT, payload=payload)


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        ("telemetry", TELEMETRY_PAYLOAD),
        ("state", STATE_PAYLOAD),
        ("report", REPORT_PAYLOAD),
        ("command", COMMAND_PAYLOAD),
    ],
)
def test_round_trip_for_each_message_type(message_type: str, payload: dict) -> None:
    message = _make_message(message_type, payload, seq=7)

    assert decode_message(encode_message(message)) == message


def test_encoded_line_has_no_newline_and_is_envelope_object() -> None:
    line = encode_message(_make_message("report", REPORT_PAYLOAD))

    assert "\n" not in line and "\r" not in line
    assert set(json.loads(line)) == {"type", "seq", "sent_at", "payload"}


def test_text_with_newline_in_payload_stays_on_one_line() -> None:
    # 문자열 안 줄바꿈은 JSON 이스케이프(\n)로 나가므로 프레이밍이 깨지지 않는다
    message = _make_message("state", {"note": "line1\nline2\r"})

    line = encode_message(message)

    assert "\n" not in line and "\r" not in line
    assert decode_message(line) == message


def test_non_ascii_text_is_kept_unescaped() -> None:
    message = _make_message("report", {"text": "보고 ✓"})

    line = encode_message(message)

    assert "보고 ✓" in line
    assert decode_message(line) == message


def test_decode_accepts_str_and_bytes_with_trailing_newlines() -> None:
    message = _make_message("command", COMMAND_PAYLOAD, seq=3)
    line = encode_message(message)

    assert decode_message(line + "\n") == message
    assert decode_message(line + "\r\n") == message
    assert decode_message((line + "\n").encode("utf-8")) == message


@pytest.mark.parametrize(
    "bad_line",
    [
        "not json",
        "[1, 2]",
        json.dumps({"type": "decision", "seq": 0, "sent_at": SENT_AT, "payload": {}}),
        json.dumps({"type": "state", "seq": -1, "sent_at": SENT_AT, "payload": {}}),
        '{"type": "state", "seq": 0, "sent_at": "x", "payload": {"v": NaN}}',
        '{"type": "state", "seq": 0, "sent_at": "x", "payload": {"v": Infinity}}',
        '{"type": "state", "seq": 0, "sent_at": "x", "payload": {"v": -Infinity}}',
    ],
)
def test_decode_rejects_invalid_lines(bad_line: str) -> None:
    with pytest.raises(ValueError):
        decode_message(bad_line)


def test_decode_rejects_invalid_utf8() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        decode_message(b"\xff\xfe\n")


def test_decode_rejects_line_over_limit() -> None:
    oversized_line = b"x" * (MAX_LINE_BYTES + 1)

    with pytest.raises(ValueError):
        decode_message(oversized_line)


def test_encode_rejects_nan_payload() -> None:
    with pytest.raises(ValueError):
        encode_message(_make_message("state", {"v": float("nan")}))


def test_encode_rejects_message_over_limit() -> None:
    huge_payload = {"blob": "x" * MAX_LINE_BYTES}

    with pytest.raises(ValueError, match="limit"):
        encode_message(_make_message("state", huge_payload))


def test_max_line_bytes_is_one_mebibyte() -> None:
    assert MAX_LINE_BYTES == 1_048_576
