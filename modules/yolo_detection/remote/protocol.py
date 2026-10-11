"""원격 YOLO 와이어 프로토콜 — Orin 서버(server.py)와 Mac 클라이언트가 함께 쓰는 고정 계약 (SIM-05, D-01).

와이어 프로토콜:
1. POST /v1/detect  — body image/jpeg, 헤더 X-Timestamp-Sec: <float>
                      → 200 {"frame": Frame.to_dict(), "server_ms": {"decode", "infer", "total"}}
2. POST /v1/reset   — 트래커·frame_id 초기화 → 200 {"status": "reset"}
3. GET  /health     — 200 {"status": "ok", "weights": str, "device": str, "frames": int}

깨진 JPEG 은 422, 그 밖의 잘못된 요청(헤더·본문 길이·경로)은 400/404, 서버 내부 오류는 500 이고 본문은 {"error": "..."} 다.
server_ms.total 은 서버가 본문을 다 읽은 직후부터 응답 JSON 을 만들기 직전까지다 — 클라이언트는 왕복에서 이를 빼 네트워크 지연을 구한다.
표준 라이브러리와 탐지 interface 만 쓴다. 그래야 Mac 은 yolo 그룹 없이 Orin 모드를 쓸 수 있다.
"""

import json
import math
from dataclasses import dataclass

from modules.yolo_detection.interface.detection import Frame

DEFAULT_PORT = 8082  # d1(SystemOne) 이 8081 을 쓴다
HEALTH_PATH = "/health"
DETECT_PATH = "/v1/detect"
RESET_PATH = "/v1/reset"

TIMESTAMP_HEADER = "X-Timestamp-Sec"
JPEG_CONTENT_TYPE = "image/jpeg"
JSON_CONTENT_TYPE = "application/json"

MAX_JPEG_BYTES = 5 * 1024 * 1024  # 1280x720 jpg 는 수백 KB — 이보다 크면 잘못된 요청

SERVER_TIMING_KEYS = ("decode", "infer", "total")


@dataclass(frozen=True, slots=True)
class ServerTiming:
    decode_ms: float
    infer_ms: float
    total_ms: float


def format_timestamp_header(timestamp_sec: float) -> str:
    # repr 은 float 를 정확히 되살릴 수 있는 가장 짧은 표기다
    return repr(float(timestamp_sec))


def parse_timestamp_header(value: str | None) -> float:
    if value is None or not value.strip():
        raise ValueError(f"missing header {TIMESTAMP_HEADER}")

    try:
        timestamp_sec = float(value)
    except ValueError:
        raise ValueError(f"header {TIMESTAMP_HEADER} must be a number, got {value!r}") from None

    if not math.isfinite(timestamp_sec) or timestamp_sec < 0:
        raise ValueError(f"header {TIMESTAMP_HEADER} must be finite and >= 0, got {value!r}")
    return timestamp_sec


def build_detect_response(frame_dict: dict, decode_ms: float, infer_ms: float, total_ms: float) -> dict:
    return {
        "frame": frame_dict,
        "server_ms": {"decode": decode_ms, "infer": infer_ms, "total": total_ms},
    }


def parse_detect_response(body: bytes | str) -> tuple[Frame, ServerTiming]:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"detect response is not valid JSON: {error}") from None

    if not isinstance(data, dict):
        raise ValueError(f"detect response must be a JSON object, got {type(data).__name__}")
    if "frame" not in data:
        raise ValueError("detect response missing field 'frame'")
    if "server_ms" not in data:
        raise ValueError("detect response missing field 'server_ms'")

    frame = Frame.from_dict(data["frame"])
    timing = _parse_server_timing(data["server_ms"])
    return frame, timing


def build_error_response(message: str) -> dict:
    return {"error": message}


def _parse_server_timing(data: object) -> ServerTiming:
    if not isinstance(data, dict):
        raise ValueError(f"field 'server_ms' must be a dict, got {type(data).__name__}")

    values: list[float] = []
    for key in SERVER_TIMING_KEYS:
        if key not in data:
            raise ValueError(f"field 'server_ms' missing key '{key}'")
        values.append(_require_duration_ms(key, data[key]))

    decode_ms, infer_ms, total_ms = values
    return ServerTiming(decode_ms=decode_ms, infer_ms=infer_ms, total_ms=total_ms)


def _require_duration_ms(key: str, value: object) -> float:
    # bool 은 int 의 하위 타입이라 따로 막는다 (interface/detection.py 의 _is_int 와 같은 규칙)
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not is_number or not math.isfinite(value) or value < 0:
        raise ValueError(f"field 'server_ms.{key}' must be a finite number >= 0, got {value!r}")
    return float(value)
