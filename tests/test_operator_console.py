"""운용자 콘솔 — 시각 붙은 태그 줄 출력, stdin 명령, 두절 종료 (CLI-01, SC3, SC5, SC8)."""

import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from modules.comm.tcp_link import TcpJsonLinesLink
from modules.comm.interface.comm import LinkStatus, Message
from modules.flight_control.interface.flight_control import GoTo, Land, ReturnToHome, TakeOff
from modules.operator_console.console import (
    LINE_TAGS,
    OperatorConsole,
    format_line,
    format_message_lines,
    parse_command_line,
)

# 01-01 E2E(tests/test_e2e_two_process.py)와 같은 정규식 — 콘솔 출력 계약
LINE_PREFIX_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} [A-Z]+ "
)
REPORT_SENTENCE_PATTERN = re.compile(r"REPORT +\[T\d+(,T\d+)*\] \((observation|suggestion)\) \S")
LISTEN_PATTERN = re.compile(r"LISTEN host=127\.0\.0\.1 port=(\d+)")

SENT_AT = "2026-10-10T12:00:00.000+00:00"
LISTEN_TIMEOUT_SEC = 10.0
WAIT_TIMEOUT_SEC = 5.0

TELEMETRY_PAYLOAD = {
    "time_sec": 12.0, "north_m": 0.0, "east_m": 0.0, "down_m": -40.0,
    "roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0,
    "gimbal_pitch_deg": -55.0, "gimbal_yaw_deg": 0.0,
    "battery_pct": 99.8, "flight_mode": "hover",
}
STATE_PAYLOAD = {
    "schema_version": 1,
    "time_sec": 12.3,
    "drone": {
        "position_ned_m": {"north": 0.0, "east": 0.0, "down": -40.0},
        "altitude_m": 40.0,
        "attitude_deg": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "gimbal_deg": {"pitch": -55.0, "yaw": 0.0},
        "battery_pct": 99.8,
        "flight_mode": "hover",
    },
    "tracks": [
        {"track_id": "T1", "class": "person", "position_ned_m": {"north": 30.0, "east": 2.0}},
        {"track_id": "T3", "class": "car", "position_ned_m": {"north": 31.0, "east": 4.0}},
    ],
    "counts_by_class": {"person": 1, "car": 1},
    "omitted_track_count": 0,
}
REPORT_PAYLOAD = {
    "time_sec": 12.3,
    "decision": {
        "verdict": "escalate",
        "reason": "person next to vehicle at checkpoint",
        "answers": [
            {"question_id": "q1", "value": "yes", "probabilities": {"yes": 0.9, "no": 0.1}, "confidence": 0.9},
        ],
    },
    "report": {
        "sentences": [
            {"kind": "observation", "text": "A person stands next to a car.", "track_ids": ["T1", "T3"]},
            {"kind": "suggestion", "text": "Keep watching the car.", "track_ids": ["T3"]},
        ],
        "flags": [],
        "is_grounded": True,
    },
    "report_error": None,
}


def _make_message(message_type: str, payload: dict, seq: int = 0) -> Message:
    return Message(type=message_type, seq=seq, sent_at=SENT_AT, payload=payload)


def _get_bodies(lines: list[tuple[str, str]], tag: str) -> list[str]:
    return [body for line_tag, body in lines if line_tag == tag]


class FakeLink:
    """CommLink 메서드 4 개를 가진 in-memory 링크. status 는 테스트가 바꾼다."""

    def __init__(self, messages: list[Message] | None = None, connected: bool = True) -> None:
        self.inbox: deque[Message] = deque(messages or [])
        self.sent: list[Message] = []
        self.status = LinkStatus(connected=connected, peer="127.0.0.1:50000", last_received_at=None)
        self.send_error: Exception | None = None
        self.disconnect_when_drained = False

    def send(self, message: Message) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)

    def receive(self, timeout_sec: float | None = None) -> Message | None:
        if self.inbox:
            return self.inbox.popleft()
        if self.disconnect_when_drained:
            self.status = LinkStatus(connected=False, peer=self.status.peer, last_received_at=SENT_AT)
        return None

    def link_status(self) -> LinkStatus:
        return self.status

    def close(self) -> None:
        pass


def _run_console(link: FakeLink) -> tuple[int, list[str]]:
    lines: list[str] = []
    console = OperatorConsole(link, write_line=lines.append, poll_interval_sec=0.01)
    stop_event = threading.Event()
    # 안전장치 — 구현이 두절을 못 보면 테스트가 멈추지 않고 실패하게
    safety_timer = threading.Timer(WAIT_TIMEOUT_SEC, stop_event.set)
    safety_timer.start()
    try:
        exit_code = console.run(stop_event, exit_on_disconnect=True)
    finally:
        safety_timer.cancel()
    assert not stop_event.is_set(), "console did not notice the disconnect"
    return exit_code, lines


# --- 줄 형식 ---


@pytest.mark.parametrize("tag", LINE_TAGS)
def test_format_line_matches_e2e_prefix(tag: str) -> None:
    line = format_line(tag, "t=1.0s body")

    assert LINE_PREFIX_PATTERN.match(line)
    assert line.endswith(f" {tag} t=1.0s body")


def test_format_line_rejects_unknown_tag_and_keeps_one_line() -> None:
    with pytest.raises(ValueError):
        format_line("NOPE", "x")

    assert "\n" not in format_line("REPORT", "fake\n2026-01-01T00:00:00.000+00:00 LINK disconnected")


# --- 메시지 포매터 ---


def test_telemetry_message_formats_one_line() -> None:
    lines = format_message_lines(_make_message("telemetry", TELEMETRY_PAYLOAD))

    assert [tag for tag, _ in lines] == ["TELEMETRY"]
    body = lines[0][1]
    for part in ("t=12.0s", "mode=hover", "alt=40.0m", "battery=99.8%"):
        assert part in body


def test_state_message_formats_track_summary() -> None:
    lines = format_message_lines(_make_message("state", STATE_PAYLOAD))

    assert [tag for tag, _ in lines] == ["STATE"]
    assert "tracks=2" in lines[0][1] and "ids=T1,T3" in lines[0][1]


def test_report_message_formats_decision_header_and_sentences() -> None:
    lines = format_message_lines(_make_message("report", REPORT_PAYLOAD))

    assert len(_get_bodies(lines, "DECISION")) == 1
    report_bodies = _get_bodies(lines, "REPORT")
    assert len(report_bodies) == 3
    assert "sentences=2" in report_bodies[0]

    rendered = [format_line(tag, body) for tag, body in lines]
    assert any("REPORT   [T1,T3] (observation) " in line for line in rendered)
    assert sum(bool(REPORT_SENTENCE_PATTERN.search(line)) for line in rendered) == 2


def test_report_without_report_shows_error() -> None:
    payload = {**REPORT_PAYLOAD, "report": None, "report_error": "llm timeout"}

    lines = format_message_lines(_make_message("report", payload))

    assert [tag for tag, _ in lines] == ["DECISION", "REPORT"]
    assert "error=llm timeout" in lines[1][1]


def test_sentence_without_track_ids_is_shown_as_dash() -> None:
    report = {"sentences": [{"kind": "observation", "text": "Nothing.", "track_ids": []}], "flags": [], "is_grounded": False}
    payload = {**REPORT_PAYLOAD, "report": report}

    lines = format_message_lines(_make_message("report", payload))

    assert "  [-] (observation) Nothing." in _get_bodies(lines, "REPORT")


def test_sentence_text_newlines_cannot_forge_lines() -> None:
    forged_text = "ok\n2026-10-10T12:00:00.000+09:00 LINK disconnected peer=x"
    report = {"sentences": [{"kind": "observation", "text": forged_text, "track_ids": ["T1"]}], "flags": [], "is_grounded": True}

    lines = format_message_lines(_make_message("report", {**REPORT_PAYLOAD, "report": report}))

    assert all("\n" not in body and "\r" not in body for _, body in lines)


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029", "\x1b[1A"])
def test_sentence_text_control_chars_cannot_forge_lines(separator: str) -> None:
    forged_text = f"ok{separator}2026-10-10T12:00:00.000+09:00 LINK disconnected peer=x"
    report = {"sentences": [{"kind": "observation", "text": forged_text, "track_ids": ["T1"]}], "flags": [], "is_grounded": True}

    lines = format_message_lines(_make_message("report", {**REPORT_PAYLOAD, "report": report}))

    assert all(len(body.splitlines()) == 1 and "\x1b" not in body for _, body in lines)


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [("state", {}), ("report", {"decision": 3}), ("telemetry", {"time_sec": "x"}), ("command", {"kind": "fly"})],
)
def test_unreadable_payload_becomes_warn_line(message_type: str, payload: dict) -> None:
    lines = format_message_lines(_make_message(message_type, payload, seq=4))

    assert len(lines) == 1
    assert lines[0][0] == "WARN" and "seq=4" in lines[0][1]


# --- stdin 명령 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rth", ReturnToHome()),
        (" RTH \n", ReturnToHome()),
        ("land", Land()),
        ("takeoff 30", TakeOff(30.0)),
        ("goto 10 -5 40", GoTo(10.0, -5.0, 40.0)),
        ("", None),
        ("   \n", None),
    ],
)
def test_parse_command_line_accepts_whitelist(text: str, expected) -> None:
    assert parse_command_line(text) == expected


@pytest.mark.parametrize("text", ["fly", "goto 1", "takeoff abc", "takeoff nan", "land now"])
def test_parse_command_line_rejects_others(text: str) -> None:
    with pytest.raises(ValueError):
        parse_command_line(text)


def test_handle_command_line_sends_command_messages() -> None:
    link = FakeLink()
    lines: list[str] = []
    console = OperatorConsole(link, write_line=lines.append)

    assert console.handle_command_line("rth") is True
    assert console.handle_command_line("land") is True

    assert [message.type for message in link.sent] == ["command", "command"]
    assert link.sent[0].payload == {"kind": "return_to_home"}
    assert [message.seq for message in link.sent] == [0, 1]
    assert sum(" COMMAND sent kind=return_to_home seq=0" in line for line in lines) == 1


def test_handle_command_line_warns_on_bad_input_and_send_failure() -> None:
    link = FakeLink()
    lines: list[str] = []
    console = OperatorConsole(link, write_line=lines.append)

    assert console.handle_command_line("fly") is True
    link.send_error = ConnectionError("link not connected")
    assert console.handle_command_line("rth") is True
    assert console.handle_command_line("quit") is False

    warn_lines = [line for line in lines if " WARN " in line]
    assert len(warn_lines) == 2
    assert "command not sent" in warn_lines[1]


# --- 수신 루프 ---


def test_run_prints_link_messages_then_disconnect_and_exits_zero() -> None:
    messages = [
        _make_message("telemetry", TELEMETRY_PAYLOAD, seq=0),
        _make_message("state", STATE_PAYLOAD, seq=1),
        _make_message("report", REPORT_PAYLOAD, seq=2),
    ]
    link = FakeLink(messages, connected=True)
    link.disconnect_when_drained = True

    exit_code, lines = _run_console(link)

    assert exit_code == 0
    assert " LINK connected peer=127.0.0.1:50000" in lines[0]
    assert " LINK disconnected" in lines[-1]
    assert [line for line in lines if " TELEMETRY " in line or " STATE " in line]
    assert all(LINE_PREFIX_PATTERN.match(line) for line in lines)


def test_run_with_fast_peer_still_orders_connected_before_messages() -> None:
    # peer 가 다 보내고 이미 끊은 상태 — 그래도 connected 줄과 메시지 줄이 disconnected 보다 앞서야 한다
    messages = [_make_message("state", STATE_PAYLOAD, seq=seq) for seq in range(3)]
    link = FakeLink(messages, connected=False)

    exit_code, lines = _run_console(link)

    assert exit_code == 0
    assert " LINK connected" in lines[0]
    assert sum(" STATE " in line for line in lines) == 3
    assert " LINK disconnected" in lines[-1]


# --- 프로세스 스모크 (드론 없이 콘솔 절반을 매번 검증) ---


def _start_stdout_reader(process: subprocess.Popen) -> "queue.Queue[str | None]":
    line_queue: queue.Queue[str | None] = queue.Queue()

    def read_lines() -> None:
        for line in process.stdout:
            line_queue.put(line.rstrip("\n"))
        line_queue.put(None)

    threading.Thread(target=read_lines, daemon=True).start()
    return line_queue


def _wait_for_line(
    line_queue: "queue.Queue[str | None]", collected: list[str], pattern: re.Pattern, timeout_sec: float
) -> re.Match:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            line = line_queue.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        collected.append(line)
        match = pattern.search(line)
        if match:
            return match
    pytest.fail(f"no line matching {pattern.pattern!r} within {timeout_sec} s: {collected}")


def test_operator_console_process_smoke(wavepilot_root: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "modules.operator_console", "--host", "127.0.0.1", "--port", "0", "--exit-on-disconnect"],
        cwd=wavepilot_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    collected: list[str] = []
    client: TcpJsonLinesLink | None = None

    try:
        line_queue = _start_stdout_reader(process)
        port = int(_wait_for_line(line_queue, collected, LISTEN_PATTERN, LISTEN_TIMEOUT_SEC).group(1))

        client = TcpJsonLinesLink.connect("127.0.0.1", port, retry_timeout_sec=WAIT_TIMEOUT_SEC)
        client.send(_make_message("report", REPORT_PAYLOAD))
        _wait_for_line(line_queue, collected, re.compile(r" LINK connected "), WAIT_TIMEOUT_SEC)

        process.stdin.write("rth\n")
        process.stdin.flush()
        command_message = client.receive(timeout_sec=WAIT_TIMEOUT_SEC)
        assert command_message is not None and command_message.type == "command"
        assert command_message.payload == {"kind": "return_to_home"}

        client.close()
        assert process.wait(timeout=WAIT_TIMEOUT_SEC) == 0
        while (line := line_queue.get(timeout=WAIT_TIMEOUT_SEC)) is not None:
            collected.append(line)
    finally:
        if client is not None:
            client.close()
        if process.poll() is None:
            process.kill()
            process.wait()

    non_empty_lines = [line for line in collected if line.strip()]
    bad_lines = [line for line in non_empty_lines if not LINE_PREFIX_PATTERN.match(line)]
    assert not bad_lines, f"lines without timestamp + tag prefix: {bad_lines}"
    assert any("LINK disconnected" in line for line in non_empty_lines)
    assert any(REPORT_SENTENCE_PATTERN.search(line) for line in non_empty_lines)
