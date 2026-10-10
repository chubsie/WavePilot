"""워킹 스켈레톤 E2E 계약: 운용자 콘솔과 드론이 별도 프로세스로 TCP 연결해 보고까지 흐른다.

01-01 에서 RED 로 작성하고 Wave 2 가 끝나면 green 이 되어야 한다.
실행: uv run pytest -m e2e tests/test_e2e_two_process.py
"""

import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

LISTEN_TIMEOUT_SEC = 10.0
DRONE_TIMEOUT_SEC = 60.0
OPERATOR_EXIT_TIMEOUT_SEC = 15.0

LISTEN_PATTERN = re.compile(r"LISTEN host=127\.0\.0\.1 port=(\d+)")
LINE_PREFIX_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} [A-Z]+ "
)
REPORT_SENTENCE_PATTERN = re.compile(r"REPORT +\[T\d+(,T\d+)*\] \((observation|suggestion)\) \S")


def _start_stdout_reader(process: subprocess.Popen) -> queue.Queue:
    # readline 은 블로킹이라 별도 스레드로 읽어야 LISTEN 대기에 시간 제한을 걸 수 있다
    line_queue: queue.Queue = queue.Queue()

    def read_lines() -> None:
        for line in process.stdout:
            line_queue.put(line.rstrip("\n"))
        line_queue.put(None)

    threading.Thread(target=read_lines, daemon=True).start()
    return line_queue


def _wait_for_listen_port(line_queue: queue.Queue, collected_lines: list[str]) -> str:
    deadline = time.monotonic() + LISTEN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            line = line_queue.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        collected_lines.append(line)
        match = LISTEN_PATTERN.search(line)
        if match:
            return match.group(1)

    pytest.fail(f"operator did not print LISTEN within {LISTEN_TIMEOUT_SEC} s: {collected_lines}")


def _drain_lines(line_queue: queue.Queue, collected_lines: list[str]) -> None:
    while True:
        try:
            line = line_queue.get(timeout=1.0)
        except queue.Empty:
            return
        if line is None:
            return
        collected_lines.append(line)


def test_scenario_run_reaches_operator_console(wavepilot_root: Path) -> None:
    operator_process = subprocess.Popen(
        [
            sys.executable, "-m", "modules.operator_console",
            "--host", "127.0.0.1", "--port", "0", "--exit-on-disconnect",
        ],
        cwd=wavepilot_root,
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
    )
    operator_lines: list[str] = []

    try:
        line_queue = _start_stdout_reader(operator_process)
        port = _wait_for_listen_port(line_queue, operator_lines)

        drone_result = subprocess.run(
            [
                sys.executable, "-m", "modules.drone.run_scenario",
                "--scenario", "modules/simulation/scenarios/checkpoint.toml",
                "--operator-host", "127.0.0.1",
                "--operator-port", port,
                "--backend", "fake",
                "--speed", "0",
            ],
            cwd=wavepilot_root,
            timeout=DRONE_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
        assert drone_result.returncode == 0, drone_result.stderr

        operator_process.wait(timeout=OPERATOR_EXIT_TIMEOUT_SEC)
        _drain_lines(line_queue, operator_lines)
    finally:
        if operator_process.poll() is None:
            operator_process.kill()
            operator_process.wait()

    non_empty_lines = [line for line in operator_lines if line.strip()]
    bad_lines = [line for line in non_empty_lines if not LINE_PREFIX_PATTERN.match(line)]
    assert not bad_lines, f"lines without timestamp + tag prefix: {bad_lines}"

    for tag in (" LINK ", " STATE ", " DECISION ", " REPORT "):
        assert any(tag in line for line in non_empty_lines), f"no line with tag {tag.strip()}"

    assert any(REPORT_SENTENCE_PATTERN.search(line) for line in non_empty_lines), (
        "no report sentence citing track IDs"
    )
    assert any("LINK disconnected" in line for line in non_empty_lines)
