"""운용자 콘솔 — 수신 메시지·링크 변화를 시각 붙은 태그 줄로 출력하고 stdin 명령을 보낸다 (CLI-01).

출력 계약(SKELETON "운용자 콘솔 출력 줄", 01-01 E2E 가 검사):
    <ISO-8601 밀리초 로컬 시각> <TAG> <body>
보고 문장 줄은 `REPORT   [T1,T3] (observation) <text>` 모양이다.

이 모듈은 다른 모듈의 interface/ 만 import 한다 — 구체 전송(TcpJsonLinesLink)은 __main__.py 컴포지션 루트만 안다(SC4).
report 페이로드는 llm_report 타입 없이 SKELETON 모양 dict 로 읽는다.
"""

import math
import sys
import threading
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone

from modules.comm.interface.comm import CommLink, Message
from modules.flight_control.interface.flight_control import (
    Command,
    GoTo,
    Land,
    ReturnToHome,
    TakeOff,
    Telemetry,
    command_from_dict,
)

LINE_TAGS = ("LISTEN", "LINK", "TELEMETRY", "STATE", "DECISION", "REPORT", "COMMAND", "WARN")
# 수신 대기 겸 링크 상태 확인 주기
POLL_INTERVAL_SEC = 0.2
# 태그 뒤 구분 공백 1 개 + 들여쓰기 2 개 = SKELETON 의 `REPORT   [T1,T3]` 3 칸.
# 헤더 줄과 문장 줄을 시각적으로 한 묶음으로 보이게 한다
REPORT_SENTENCE_INDENT = "  "
# peer 가 만든 거대한 오류 문자열이 운용자 화면을 덮지 않게 자른다
WARN_ERROR_MAX_CHARS = 200
# 제어·서식·줄/문단 구분 문자(ANSI escape 포함)는 \uXXXX 로 escape 해서 보인다
UNPRINTABLE_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")

# stdin 명령 단어 -> (숫자 인자 개수, 명령 생성자). 화이트리스트는 여기 한곳에만 둔다
COMMAND_WORDS: dict[str, tuple[int, Callable[..., Command]]] = {
    "rth": (0, ReturnToHome),
    "land": (0, Land),
    "takeoff": (1, TakeOff),
    "goto": (3, GoTo),
}
QUIT_WORDS = ("quit", "exit")
COMMAND_USAGE = "commands: rth | land | takeoff <alt_m> | goto <north_m> <east_m> <alt_m> | quit"


# --- 줄 형식 ---


def format_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).astimezone().isoformat(timespec="milliseconds")


def format_line(tag: str, body: str, now: datetime | None = None) -> str:
    """한 줄 조립. 태그는 패딩하지 않는다 — E2E 가 "LINK disconnected" 를 부분 문자열로 찾기 때문."""
    if tag not in LINE_TAGS:
        raise ValueError(f"unknown line tag {tag!r}; expected one of {LINE_TAGS}")

    return f"{format_timestamp(now)} {tag} {_to_single_line(body)}"


def format_message_lines(message: Message) -> list[tuple[str, str]]:
    """Message 를 (tag, body) 목록으로. payload 모양이 틀리면 WARN 줄 하나 — 콘솔은 peer 데이터로 죽지 않는다."""
    formatter = _MESSAGE_FORMATTERS.get(message.type)
    if formatter is None:
        return [("WARN", f"unknown message type {message.type!r} seq={message.seq}")]

    try:
        lines = formatter(message.payload)
    except (KeyError, TypeError, ValueError) as error:
        reason = f"{type(error).__name__}: {error}"[:WARN_ERROR_MAX_CHARS]
        lines = [("WARN", f"unreadable {message.type} payload seq={message.seq}: {reason}")]

    return [(tag, _to_single_line(body)) for tag, body in lines]


def _to_single_line(text: str) -> str:
    # peer 텍스트(보고 문장·reason·flags) 속 줄바꿈·제어문자·ANSI escape 로 가짜 LINK/REPORT 줄을
    # 끼워 넣는 로그 위조를 막는다 — 1 문장 = 1 줄. \v·\f·U+2028 도 splitlines 는 줄로 본다
    return "".join(_to_printable_char(char) for char in text)


def _to_printable_char(char: str) -> str:
    if char in "\r\n\t":
        return " "
    if unicodedata.category(char) in UNPRINTABLE_CATEGORIES:
        return f"\\u{ord(char):04x}"

    return char


def _format_telemetry_lines(payload: dict) -> list[tuple[str, str]]:
    telemetry = Telemetry.from_dict(payload)

    body = (
        f"t={telemetry.time_sec:.1f}s mode={telemetry.flight_mode} "
        f"pos_ned=({telemetry.north_m:.1f},{telemetry.east_m:.1f},{telemetry.down_m:.1f}) "
        f"alt={telemetry.altitude_m:.1f}m "
        f"att_deg=({telemetry.roll_deg:.1f},{telemetry.pitch_deg:.1f},{telemetry.yaw_deg:.1f}) "
        f"gimbal_deg=({telemetry.gimbal_pitch_deg:.1f},{telemetry.gimbal_yaw_deg:.1f}) "
        f"battery={telemetry.battery_pct:.1f}%"
    )

    return [("TELEMETRY", body)]


def _format_state_lines(payload: dict) -> list[tuple[str, str]]:
    drone = _require_type(payload["drone"], dict, "drone")
    tracks = _require_type(payload["tracks"], list, "tracks")
    counts_by_class = _require_type(payload["counts_by_class"], dict, "counts_by_class")
    track_ids = [str(_require_type(track, dict, "track")["track_id"]) for track in tracks]

    body = (
        f"t={float(payload['time_sec']):.1f}s alt={float(drone['altitude_m']):.1f}m "
        f"battery={float(drone['battery_pct']):.1f}% tracks={len(tracks)} "
        f"ids={','.join(track_ids) or '-'} "
        f"counts={','.join(f'{name}:{count}' for name, count in counts_by_class.items()) or '-'} "
        f"omitted={payload['omitted_track_count']}"
    )

    return [("STATE", body)]


def _format_report_lines(payload: dict) -> list[tuple[str, str]]:
    time_text = f"t={float(payload['time_sec']):.1f}s"
    decision = _require_type(payload["decision"], dict, "decision")
    answers = _require_type(decision["answers"], list, "answers")
    report = payload["report"]

    answer_text = "; ".join(
        f"{answer['question_id']}={answer['value']}@{float(answer['confidence']):.2f}" for answer in answers
    )
    lines = [("DECISION", f"{time_text} verdict={decision['verdict']} reason={decision['reason']} answers={answer_text or '-'}")]
    if report is None:
        lines.append(("REPORT", f"{time_text} error={payload.get('report_error') or 'no report'}"))
        return lines

    report = _require_type(report, dict, "report")
    sentences = _require_type(report["sentences"], list, "sentences")
    flags = _require_type(report["flags"], list, "flags")
    grounded_text = "true" if report["is_grounded"] else "false"
    lines.append(("REPORT", f"{time_text} sentences={len(sentences)} grounded={grounded_text} flags={'; '.join(map(str, flags)) or '-'}"))
    for sentence in sentences:
        sentence = _require_type(sentence, dict, "sentence")
        track_ids = _require_type(sentence["track_ids"], list, "track_ids")
        lines.append(("REPORT", f"{REPORT_SENTENCE_INDENT}[{','.join(track_ids) or '-'}] ({sentence['kind']}) {sentence['text']}"))

    return lines


def _format_command_lines(payload: dict) -> list[tuple[str, str]]:
    # 드론→운용자 방향 command 는 정상 흐름이 아니지만 4 종 중 하나라 표시만 한다
    command = command_from_dict(payload)

    return [("COMMAND", f"received kind={command.KIND}")]


_MESSAGE_FORMATTERS: dict[str, Callable[[dict], list[tuple[str, str]]]] = {
    "telemetry": _format_telemetry_lines,
    "state": _format_state_lines,
    "report": _format_report_lines,
    "command": _format_command_lines,
}


def _require_type(value: object, expected_type: type, name: str):
    if not isinstance(value, expected_type):
        raise TypeError(f"field '{name}' must be {expected_type.__name__}, got {type(value).__name__}")
    return value


# --- stdin 명령 ---


def parse_command_line(text: str) -> Command | None:
    """운용자 입력 한 줄을 명령으로. 빈 줄은 None, 화이트리스트 밖·인자 오류는 ValueError."""
    words = text.strip().lower().split()
    if not words:
        return None

    command_word, *arguments = words
    if command_word not in COMMAND_WORDS:
        raise ValueError(f"unknown command {command_word!r}; {COMMAND_USAGE}")
    argument_count, create_command = COMMAND_WORDS[command_word]
    if len(arguments) != argument_count:
        raise ValueError(f"{command_word!r} takes {argument_count} number(s); {COMMAND_USAGE}")

    numbers = [_parse_finite_number(argument) for argument in arguments]

    return create_command(*numbers)


def _parse_finite_number(text: str) -> float:
    # NaN/inf 는 JSON 으로 보낼 수 없고(encode_message 가 거부) 비행 명령으로도 의미가 없다
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"number must be finite, got {text!r}")
    return number


# --- 콘솔 ---


class OperatorConsole:
    """수신 루프 + stdin 명령 처리. 출력은 락으로 직렬화한다(stdin 스레드와 수신 루프가 동시에 쓴다)."""

    def __init__(
        self,
        link: CommLink,
        write_line: Callable[[str], None] | None = None,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._link = link
        self._write_line = write_line or _write_stdout_line
        self._poll_interval_sec = poll_interval_sec
        # RLock — handle_message 가 여러 줄을 한 묶음으로 쓰는 동안 write 를 다시 부른다
        self._output_lock = threading.RLock()
        self._command_seq = 0
        self._was_connected = False

    def write(self, tag: str, body: str) -> None:
        with self._output_lock:
            self._write_line(format_line(tag, body))

    def handle_message(self, message: Message) -> None:
        # 한 메시지의 줄들 사이에 stdin 쪽 COMMAND 줄이 끼지 않게 묶어서 쓴다
        with self._output_lock:
            for tag, body in format_message_lines(message):
                self.write(tag, body)

    def handle_command_line(self, text: str) -> bool:
        """명령 한 줄 처리. 콘솔을 끝내야 하면 False."""
        if text.strip().lower() in QUIT_WORDS:
            return False
        try:
            command = parse_command_line(text)
        except ValueError as error:
            self.write("WARN", str(error))
            return True
        if command is None:
            return True

        seq = self._command_seq
        message = Message(
            type="command",
            seq=seq,
            sent_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            payload=command.to_dict(),
        )

        try:
            self._link.send(message)
        except ConnectionError as error:
            self.write("WARN", f"command not sent: {error}")
            return True
        self._command_seq += 1
        self.write("COMMAND", f"sent kind={command.KIND} seq={seq}")

        return True

    def check_link(self) -> bool:
        """링크 변화를 출력한다. 방금 두절이 감지되면 True."""
        status = self._link.link_status()

        if not self._was_connected and status.connected:
            self._handle_link_connected(status.peer)
            return False
        if not (self._was_connected and not status.connected):
            return False

        # TcpJsonLinesLink 는 마지막 줄을 inbox 에 넣은 뒤에만 connected=False 가 된다 —
        # 그래서 지금 inbox 를 끝까지 비우면 두절 줄보다 앞서 모든 메시지가 출력된다
        while (message := self._link.receive(timeout_sec=0)) is not None:
            self.handle_message(message)
        self.write("LINK", f"disconnected peer={status.peer} last_received_at={status.last_received_at or 'none'}")
        self._was_connected = False

        return True

    def run(self, stop_event: threading.Event, exit_on_disconnect: bool) -> int:
        """stop 될 때까지 수신·출력. exit_on_disconnect 면 두절 직후 0 을 반환한다."""
        while not stop_event.is_set():
            message = self._link.receive(timeout_sec=self._poll_interval_sec)
            if message is not None:
                # 빠른 peer 가 연결~두절 사이에 다 보내고 끊어도 connected 줄이 메시지 줄보다 앞서게
                if not self._was_connected:
                    self._handle_link_connected(self._link.link_status().peer)
                self.handle_message(message)

            has_disconnected = self.check_link()
            if has_disconnected and exit_on_disconnect:
                return 0

        return 0

    def _handle_link_connected(self, peer: str | None) -> None:
        self.write("LINK", f"connected peer={peer}")
        self._was_connected = True


def _write_stdout_line(line: str) -> None:
    sys.stdout.write(line + "\n")
    # flush 필수 — stdout 이 PIPE 면 블록 버퍼라 E2E 가 LISTEN 줄을 영영 못 본다
    sys.stdout.flush()
