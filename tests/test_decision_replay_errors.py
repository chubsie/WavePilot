"""재생 CLI 오류 처리 테스트 (WR-05).

흔한 실수 네 가지 — 잘못된 --log-level, 나쁜 D1_SERVER_URL, time_sec 역행 픽스처, 쓸 수 없는 CSV 경로 — 가
traceback 없이 stderr 한 줄과 정해진 종료 코드로 끝나는지 본다.
logging.basicConfig 는 pytest 가 root 에 handler 를 단 상태에서는 아무것도 하지 않으므로 실제 stderr 를 보려고
하위 프로세스로 CLI 를 돌린다. 네트워크는 쓰지 않는다 — 나쁜 URL 은 클라이언트 생성 단계에서 거절된다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from modules.decision.replay import EXIT_FAILURE, EXIT_USAGE, load_ticks

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
PATROL_FIXTURE = WAVEPILOT_ROOT / "modules/decision/fixtures/patrol_no_target.jsonl"
REPLAY_MODULE = "modules.decision.replay"
SELECTOR_ENV_NAMES = ("WAVEPILOT_SELECTOR", "D1_SERVER_URL")


# --- 도우미 ---


def _run_replay_cli(args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {name: value for name, value in os.environ.items() if name not in SELECTOR_ENV_NAMES}
    env.update(extra_env or {})

    return subprocess.run(
        [sys.executable, "-m", REPLAY_MODULE, *args],
        cwd=WAVEPILOT_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def _get_stderr_lines(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in completed.stderr.splitlines() if line.strip()]


def _write_backwards_fixture(tmp_path: Path) -> Path:
    first_row = json.loads(PATROL_FIXTURE.read_text(encoding="utf-8").splitlines()[0])
    rows = []
    for time_sec in (0.0, 5.0, 3.0):
        row = json.loads(json.dumps(first_row))
        row["state"]["time_sec"] = time_sec
        rows.append(json.dumps(row))

    path = tmp_path / "backwards.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# --- load_ticks 시각 순서 ---


def test_load_ticks_rejects_time_going_backwards_with_line_number(tmp_path):
    path = _write_backwards_fixture(tmp_path)

    with pytest.raises(ValueError, match=r"line 3: time_sec must not go backwards: 3\.0 < 5\.0"):
        load_ticks(path)


def test_load_ticks_accepts_equal_time_sec(tmp_path):
    first_line = PATROL_FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "same_time.jsonl"
    path.write_text(first_line + "\n" + first_line + "\n", encoding="utf-8")

    assert len(load_ticks(path)) == 2


# --- CLI: traceback 없이 한 줄 ---


def test_cli_invalid_log_level_is_usage_error():
    completed = _run_replay_cli([str(PATROL_FIXTURE), "--selector", "rule", "--log-level", "foo"])

    assert completed.returncode == EXIT_USAGE
    assert "Traceback" not in completed.stderr
    assert "--log-level must be one of" in _get_stderr_lines(completed)[-1]
    assert completed.stdout == ""


def test_cli_bad_d1_server_url_is_usage_error():
    completed = _run_replay_cli(
        [str(PATROL_FIXTURE), "--selector", "d1"], extra_env={"D1_SERVER_URL": "ftp://orin:8081"}
    )

    stderr_lines = _get_stderr_lines(completed)
    assert completed.returncode == EXIT_USAGE
    assert "Traceback" not in completed.stderr
    assert len(stderr_lines) == 1
    assert "invalid selector config" in stderr_lines[0]
    assert completed.stdout == ""


def test_cli_backwards_fixture_fails_before_any_selection(tmp_path):
    path = _write_backwards_fixture(tmp_path)

    completed = _run_replay_cli([str(path), "--selector", "rule"])

    stderr_lines = _get_stderr_lines(completed)
    assert completed.returncode == EXIT_USAGE
    assert "Traceback" not in completed.stderr
    assert len(stderr_lines) == 1
    assert "time_sec must not go backwards" in stderr_lines[0]
    # 읽는 시점에 거르므로 SELECT 줄이 하나도 나가지 않는다
    assert "SELECT " not in completed.stdout


def test_cli_unwritable_csv_is_failure(tmp_path):
    csv_path = tmp_path / "missing_dir" / "out.csv"

    completed = _run_replay_cli([str(PATROL_FIXTURE), "--selector", "rule", "--csv", str(csv_path)])

    stderr_lines = _get_stderr_lines(completed)
    assert completed.returncode == EXIT_FAILURE
    assert "Traceback" not in completed.stderr
    assert len(stderr_lines) == 1
    assert "cannot write csv" in stderr_lines[0]
    assert not csv_path.exists()
