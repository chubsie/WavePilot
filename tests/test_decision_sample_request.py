"""스모크 요청 본문 생성기 테스트 (IN-07).

본문이 실제 d1 요청과 같은 조합 — 판정기 사건, filter_options 보기, RULE_MODE_DEFINITIONS 정의, 요청 상태 v2 문자열 —
으로 만들어지는지 본다. HTTP 는 쓰지 않는다.
"""

import json
from pathlib import Path

from modules.decision.interface.menu import RULE_MODE_DEFINITIONS
from modules.decision.interface.state import EVENT_MODE_ENDED
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.prompt import (
    QUESTION_ID_RULE_MODE,
    RULE_MODE_INSTRUCTIONS,
    serialize_request_state,
)
from modules.decision.replay import load_ticks
from modules.decision.sample_request import (
    DEFAULT_FIXTURE,
    DEFAULT_LINE_NO,
    EXIT_OK,
    EXIT_USAGE,
    get_event_tick,
    main,
)

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
PATROL_FIXTURE = WAVEPILOT_ROOT / "modules/decision/fixtures/patrol_no_target.jsonl"
PATROL_MODE_ENDED_LINE_NO = 26  # t=25.0s mode_ended
PATROL_NO_EVENT_LINE_NO = 2  # t=1.0s — 사건 없음


def _run_main(capsys, argv: list[str]) -> tuple[int, str, str]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _get_expected_options(fixture: Path, line_no: int) -> tuple[str, ...]:
    tick, event = get_event_tick(load_ticks(fixture), line_no)
    return filter_options(tick.state, event, ParamDefaults().approach_stop_distance_m)


def test_default_body_matches_real_request(capsys):
    exit_code, stdout, stderr = _run_main(capsys, [])

    body = json.loads(stdout)
    state = json.loads(body["state"])
    question = body["questions"][QUESTION_ID_RULE_MODE]
    expected_options = _get_expected_options(DEFAULT_FIXTURE, DEFAULT_LINE_NO)
    assert exit_code == EXIT_OK
    assert stderr == ""
    assert set(body) == {"state", "questions"}
    assert list(body["questions"]) == [QUESTION_ID_RULE_MODE]
    assert question["type"] == "choice"
    assert question["instructions"] == RULE_MODE_INSTRUCTIONS
    assert tuple(question["criteria"]) == expected_options
    assert question["criteria"] == {mode: RULE_MODE_DEFINITIONS[mode] for mode in expected_options}
    assert state["schema_version"] == 2
    assert state["event"]["kind"] == EVENT_MODE_ENDED
    assert state["tracks"], "default line must have target tracks"


def test_default_state_string_is_serialized_request_state(capsys):
    _, stdout, _ = _run_main(capsys, [])

    tick, event = get_event_tick(load_ticks(DEFAULT_FIXTURE), DEFAULT_LINE_NO)
    assert json.loads(stdout)["state"] == serialize_request_state(tick.state, event)


def test_other_fixture_line_uses_its_trigger_event(capsys):
    argv = ["--fixture", str(PATROL_FIXTURE), "--line", str(PATROL_MODE_ENDED_LINE_NO)]

    exit_code, stdout, _ = _run_main(capsys, argv)

    body = json.loads(stdout)
    criteria = body["questions"][QUESTION_ID_RULE_MODE]["criteria"]
    assert exit_code == EXIT_OK
    assert json.loads(body["state"])["event"]["kind"] == EVENT_MODE_ENDED
    assert tuple(criteria) == _get_expected_options(PATROL_FIXTURE, PATROL_MODE_ENDED_LINE_NO)


def test_line_without_event_is_usage_error(capsys):
    argv = ["--fixture", str(PATROL_FIXTURE), "--line", str(PATROL_NO_EVENT_LINE_NO)]

    exit_code, stdout, stderr = _run_main(capsys, argv)

    assert exit_code == EXIT_USAGE
    assert stdout == ""
    assert f"line {PATROL_NO_EVENT_LINE_NO} has no selection event" in stderr
    assert len(stderr.splitlines()) == 1


def test_line_past_end_is_usage_error(capsys):
    exit_code, stdout, stderr = _run_main(capsys, ["--line", "9999"])

    assert exit_code == EXIT_USAGE
    assert stdout == ""
    assert "line 9999 is not a tick line" in stderr


def test_missing_fixture_is_usage_error(tmp_path, capsys):
    exit_code, stdout, stderr = _run_main(capsys, ["--fixture", str(tmp_path / "missing.jsonl")])

    assert exit_code == EXIT_USAGE
    assert stdout == ""
    assert "Traceback" not in stderr
    assert len(stderr.splitlines()) == 1
