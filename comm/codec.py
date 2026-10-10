"""Message <-> UTF-8 JSON 한 줄 코덱 (IF-03).

1 메시지 = 1 줄 UTF-8 JSON (JSON lines). envelope 는 interfaces.comm.Message 그대로이며
전송 수단(TCP, 무선 모뎀, LTE)과 무관하다 — 프레이밍("\\n" 붙이기·줄 자르기)은 전송 쪽 책임이다.

줄 길이 상한을 두는 이유: 악성이거나 고장 난 peer 가 끝없는 줄을 보내 수신 측 메모리를
고갈시키는 것을 막는다. 상한 검사는 인코딩·디코딩 양쪽에서 같은 바이트 기준으로 한다.
"""

import json

from interfaces.comm import Message

# 1 MiB, 끝의 "\n" 제외. 보고 페이로드는 수 KB 라 여유가 크다
MAX_LINE_BYTES = 1_048_576


def encode_message(message: Message) -> str:
    """Message 를 줄바꿈 없는 JSON 한 줄로. NaN/Infinity·상한 초과는 ValueError."""
    # allow_nan=False — NaN 은 표준 JSON 이 아니라 상대가 못 읽는다. 내는 ValueError 는 그대로 둔다
    line = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    line_byte_count = len(line.encode("utf-8"))
    if line_byte_count > MAX_LINE_BYTES:
        raise ValueError(f"encoded message is {line_byte_count} bytes; limit is {MAX_LINE_BYTES}")

    return line


def decode_message(line: str | bytes) -> Message:
    """JSON 한 줄(끝의 "\\n"/"\\r\\n" 허용)을 Message 로. 모든 실패는 ValueError 하나로 모은다."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"line is not valid UTF-8: {error}") from error
    text = line.rstrip("\r\n")

    text_byte_count = len(text.encode("utf-8"))
    if text_byte_count > MAX_LINE_BYTES:
        raise ValueError(f"line is {text_byte_count} bytes; limit is {MAX_LINE_BYTES}")

    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("message line must be a JSON object")

    return Message.from_dict(data)


def _reject_json_constant(constant: str) -> float:
    # json.loads 는 기본으로 NaN/Infinity 를 받아들인다 — 상대 peer 입력이므로 거부한다
    raise ValueError(f"JSON constant {constant} is not allowed")
