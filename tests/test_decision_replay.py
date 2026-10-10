"""재생 하네스 테스트 (SEL-05 질문 수 == 사건 수, SEL-02 지연 분포 보기).

선택기는 RuleSelector 만 쓰고 HTTP·가짜 HTTP 서버는 쓰지 않는다(D-11).
d1 경로 재생은 사용자가 Orin 서버를 띄운 뒤 수동으로 확인한다.
"""

import json
from pathlib import Path

import pytest

from modules.decision.interface.selector import FALLBACK_NONE, SOURCE_RULE, SelectionResult
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    ModeResult,
    SelectionEvent,
    SelectionState,
)
from modules.decision.params import ParamDefaults
from modules.decision.replay import ReplayRun, load_ticks, run_replay
from modules.decision.rule_selector import RuleSelector
from modules.decision.trigger import SelectionTrigger

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = WAVEPILOT_ROOT / "modules/decision/fixtures"
PATROL_FIXTURE = FIXTURES_DIR / "patrol_no_target.jsonl"
CHAIN_FIXTURE = FIXTURES_DIR / "target_found_mode_chain.jsonl"

# 픽스처별 기대 사건 (시각, kind) — 이 상수가 기대값의 정본이다
EXPECTED_PATROL_EVENTS = [
    (0.0, EVENT_PERIODIC_CHECK),
    (10.0, EVENT_PERIODIC_CHECK),
    (20.0, EVENT_PERIODIC_CHECK),
    (25.0, EVENT_MODE_ENDED),
]
EXPECTED_CHAIN_EVENTS = [
    (0.0, EVENT_PERIODIC_CHECK),
    (3.0, EVENT_NEW_TARGET),
    (8.0, EVENT_MODE_ENDED),
    (12.0, EVENT_MODE_ENDED),
    (18.0, EVENT_MODE_ENDED),
    (28.0, EVENT_PERIODIC_CHECK),
]


# --- 도우미 ---


class CountingSelector:
    """select 호출마다 (시각, kind) 를 기록하고 안쪽 선택기에 넘긴다."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple[float, str]] = []

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        self.calls.append((state.time_sec, event.kind))
        return self.inner.select(state, event)


def _make_counting_selector() -> CountingSelector:
    return CountingSelector(RuleSelector(ParamDefaults()))


def _replay_fixture(path: Path, warmup_count: int = 0, **callbacks) -> tuple[ReplayRun, CountingSelector]:
    selector = _make_counting_selector()
    run = run_replay(load_ticks(path), selector, SelectionTrigger(), warmup_count, **callbacks)
    return run, selector


def _get_record_events(path: Path, run: ReplayRun) -> list[tuple[float, str]]:
    ticks = load_ticks(path)
    return [(ticks[record.tick_index].state.time_sec, record.event.kind) for record in run.records]


def _write_lines(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "fixture.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _get_first_line_object() -> dict:
    return json.loads(PATROL_FIXTURE.read_text(encoding="utf-8").splitlines()[0])


# --- load_ticks ---


@pytest.mark.parametrize(("path", "tick_count"), [(PATROL_FIXTURE, 35), (CHAIN_FIXTURE, 30)])
def test_load_ticks_reads_fixture_in_one_second_steps(path, tick_count):
    ticks = load_ticks(path)

    assert len(ticks) == tick_count
    assert all(isinstance(tick.state, SelectionState) for tick in ticks)
    assert [tick.state.time_sec for tick in ticks] == [float(index) for index in range(tick_count)]


@pytest.mark.parametrize("path", [PATROL_FIXTURE, CHAIN_FIXTURE])
def test_load_ticks_mode_result_round_trips_contract(path):
    raw_rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    ticks = load_ticks(path)

    for raw_row, tick in zip(raw_rows, ticks):
        if raw_row["mode_result"] is None:
            assert tick.mode_result is None
        else:
            assert isinstance(tick.mode_result, ModeResult)
            assert tick.mode_result.to_dict() == raw_row["mode_result"]


def test_load_ticks_skips_blank_lines(tmp_path):
    line = json.dumps(_get_first_line_object())
    path = _write_lines(tmp_path, ["", line, "   ", line])

    ticks = load_ticks(path)

    assert len(ticks) == 2
    assert ticks[0].line_no == 2


def _make_broken_lines() -> dict[str, str]:
    base = _get_first_line_object()
    state = base["state"]
    bad_state = dict(state, extra_key=1)
    return {
        "broken_json": "{not json",
        "not_object": "[1, 2]",
        "missing_key": json.dumps({"state": state}),
        "unknown_key": json.dumps({"state": state, "mode_result": None, "note": "x"}),
        "mode_result_target_track_key": json.dumps(
            {"state": state, "mode_result": {"rule_mode": "search", "outcome": "timeout", "target_track": None}}
        ),
        "mode_result_bad_outcome": json.dumps(
            {"state": state, "mode_result": {"rule_mode": "search", "outcome": "crashed", "track": None}}
        ),
        "bad_state": json.dumps({"state": bad_state, "mode_result": None}),
    }


@pytest.mark.parametrize("case", sorted(_make_broken_lines()))
def test_load_ticks_rejects_bad_line_with_line_number(tmp_path, case):
    good_line = json.dumps(_get_first_line_object())
    path = _write_lines(tmp_path, [good_line, _make_broken_lines()[case]])

    with pytest.raises(ValueError, match="line 2"):
        load_ticks(path)


def test_load_ticks_rejects_empty_file(tmp_path):
    path = _write_lines(tmp_path, ["", "  "])

    with pytest.raises(ValueError):
        load_ticks(path)


# --- run_replay: 질문 수 == 사건 수 (SEL-05) ---


@pytest.mark.parametrize(
    ("path", "expected_events"),
    [(PATROL_FIXTURE, EXPECTED_PATROL_EVENTS), (CHAIN_FIXTURE, EXPECTED_CHAIN_EVENTS)],
)
def test_replay_selects_only_on_events(path, expected_events):
    run, selector = _replay_fixture(path)

    assert _get_record_events(path, run) == expected_events
    # 사건 없는 틱에서는 질문 0회 — 호출 시각 목록이 사건 시각 목록과 같다
    assert selector.calls == expected_events
    assert len(run.records) == len(expected_events)
    assert run.tick_count == len(load_ticks(path))


@pytest.mark.parametrize("path", [PATROL_FIXTURE, CHAIN_FIXTURE])
def test_replay_results_are_valid_rule_results(path):
    run, _ = _replay_fixture(path)

    for record in run.records:
        assert record.result.rule_mode in record.result.options
        assert record.result.source == SOURCE_RULE
        assert record.result.fallback_reason == FALLBACK_NONE


def test_patrol_has_no_new_target_event():
    # car 는 대상 클래스가 아니므로 새 대상 사건이 없다
    run, _ = _replay_fixture(PATROL_FIXTURE)

    assert all(record.event.kind != EVENT_NEW_TARGET for record in run.records)


# --- warm-up ---


def test_warmup_is_excluded_from_records_and_trigger():
    records = []
    warmups = []

    run, selector = _replay_fixture(
        CHAIN_FIXTURE, warmup_count=2,
        on_record=records.append, on_warmup=lambda result, time_sec: warmups.append((result, time_sec)),
    )

    event_count = len(EXPECTED_CHAIN_EVENTS)
    assert len(selector.calls) == event_count + 2
    assert len(run.records) == event_count
    assert len(run.warmup_results) == 2
    assert len(warmups) == 2
    assert all(time_sec == 0.0 for _, time_sec in warmups)
    assert len(records) == event_count
    # warm-up 은 trigger 를 건드리지 않으므로 사건 목록이 warm-up 0 일 때와 같다
    assert _get_record_events(CHAIN_FIXTURE, run) == EXPECTED_CHAIN_EVENTS


def test_run_replay_rejects_negative_warmup():
    with pytest.raises(ValueError):
        run_replay(load_ticks(PATROL_FIXTURE), _make_counting_selector(), SelectionTrigger(), -1)


def test_run_replay_rejects_empty_ticks():
    with pytest.raises(ValueError):
        run_replay((), _make_counting_selector(), SelectionTrigger())
