"""재생 하네스 테스트 (SEL-05 질문 수 == 사건 수, SEL-02 지연 분포 보기).

선택기는 RuleSelector 만 쓰고 HTTP·가짜 HTTP 서버는 쓰지 않는다(D-11).
d1 경로 재생은 사용자가 Orin 서버를 띄운 뒤 수동으로 확인한다.
"""

import ast
import csv
import importlib.util
import json
import re
from pathlib import Path

import pytest

from modules.decision.interface.selector import (
    FALLBACK_NONE,
    FALLBACK_REASONS,
    FALLBACK_TIMEOUT,
    SOURCE_D1,
    SOURCE_RULE,
    Latency,
    SelectionResult,
)
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    ModeResult,
    SelectionEvent,
    SelectionState,
)
from modules.decision.params import ParamDefaults
from modules.decision.replay import (
    CSV_COLUMNS,
    ReplayRecord,
    ReplayRun,
    calculate_percentile,
    format_selection_line,
    format_summary_lines,
    format_warmup_line,
    load_ticks,
    main,
    run_replay,
    write_csv,
)
from modules.decision.rule_selector import RuleSelector
from modules.decision.trigger import SelectionTrigger

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = WAVEPILOT_ROOT / "modules/decision/fixtures"
PATROL_FIXTURE = FIXTURES_DIR / "patrol_no_target.jsonl"
CHAIN_FIXTURE = FIXTURES_DIR / "target_found_mode_chain.jsonl"
FACTORY_MODULE = "modules.decision.factory"  # 01-05 소유 — replay 는 main() 안에서만 지연 import 한다

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


# --- SELECT / WARMUP 줄 (SEL-02) ---

SELECT_LINE_PATTERN = re.compile(
    r"^SELECT t=\d+\.\ds event=(mode_ended|new_target|periodic_check) options=[a-z_]+(,[a-z_]+)* "
    r"rule_mode=[a-z_]+ source=(d1|rule) fallback=[a-z_]+ confidence=\d\.\d{2} "
    r"orin_ms=(-|\d+\.\d{2}) net_ms=(-|\d+\.\d{2}) total_ms=\d+\.\d{2} input_tokens=(-|\d+) params=\{.*\}$"
)


def _make_d1_result(
    total_ms: float = 120.0,
    orin_ms: float | None = 100.0,
    net_ms: float | None = 20.0,
    source: str = SOURCE_D1,
    fallback_reason: str = FALLBACK_NONE,
    params: dict | None = None,
) -> SelectionResult:
    return SelectionResult(
        rule_mode="search",
        params={"area": "zone_a"} if params is None else params,
        confidence=0.9,
        probabilities={"search": 0.9, "hold": 0.1},
        source=source,
        fallback_reason=fallback_reason,
        options=("search", "hold"),
        latency=Latency(total_ms=total_ms, orin_processing_ms=orin_ms, network_rtt_ms=net_ms),
        input_tokens=312,
    )


def _make_record(result: SelectionResult, time_sec: float = 3.0) -> ReplayRecord:
    return ReplayRecord(
        tick_index=int(time_sec), time_sec=time_sec, event=SelectionEvent(EVENT_PERIODIC_CHECK, None), result=result,
    )


def test_rule_selection_lines_match_pattern():
    run, _ = _replay_fixture(CHAIN_FIXTURE)

    lines = [format_selection_line(record) for record in run.records]

    assert all(SELECT_LINE_PATTERN.match(line) for line in lines), lines
    assert all("orin_ms=- net_ms=- " in line and "input_tokens=- " in line for line in lines)


def test_d1_selection_line_shows_latency_split_and_tokens():
    line = format_selection_line(_make_record(_make_d1_result()))

    assert SELECT_LINE_PATTERN.match(line), line
    assert "orin_ms=100.00 net_ms=20.00 total_ms=120.00 input_tokens=312" in line


def test_warmup_line_shares_select_body():
    result = _make_d1_result()

    warmup_line = format_warmup_line(result, 3.0)
    select_line = format_selection_line(_make_record(result, 3.0))

    assert warmup_line.startswith("WARMUP ")
    assert warmup_line.removeprefix("WARMUP ") == select_line.removeprefix("SELECT ")


def test_params_with_newline_stay_on_one_line():
    line = format_selection_line(_make_record(_make_d1_result(params={"note": "a\nSELECT forged"})))

    assert "\n" not in line
    assert SELECT_LINE_PATTERN.match(line), line


# --- calculate_percentile ---


def test_percentile_nearest_rank():
    values = [float(value) for value in range(1, 11)]

    assert calculate_percentile(values, 50) == 5
    assert calculate_percentile(values, 90) == 9
    assert calculate_percentile([7.0], 90) == 7.0
    assert calculate_percentile([3, 1, 2], 50) == 2


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        calculate_percentile([], 50)


# --- 요약 표 ---


def test_rule_summary_lines():
    run, _ = _replay_fixture(PATROL_FIXTURE)

    lines = format_summary_lines(run, "patrol_no_target.jsonl", "rule")

    assert lines[0] == "REPLAY fixture=patrol_no_target.jsonl selector=rule ticks=35 selections=4 question_ratio=0.114 warmup=0"
    assert "EVENTS mode_ended=1 new_target=0 periodic_check=3" in lines
    assert "SOURCES d1=0 rule=4" in lines
    assert "TOKENS n=0 p50=- max=-" in lines

    latency_lines = [line for line in lines if line.startswith("LATENCY ")]
    assert len(latency_lines) == 4
    assert latency_lines[1].split()[1:3] == ["total_ms", "4"]
    assert latency_lines[2].split()[1:] == ["orin_ms", "0", "-", "-", "-"]
    assert latency_lines[3].split()[1:] == ["network_ms", "0", "-", "-", "-"]

    fallback_line = next(line for line in lines if line.startswith("FALLBACK "))
    expected_counts = [f"{reason}={4 if reason == FALLBACK_NONE else 0}" for reason in FALLBACK_REASONS]
    assert fallback_line == "FALLBACK " + " ".join(expected_counts)


def test_d1_summary_percentiles_and_fallback_count():
    results = (
        _make_d1_result(total_ms=100.0, orin_ms=80.0, net_ms=20.0),
        _make_d1_result(total_ms=200.0, orin_ms=170.0, net_ms=30.0),
        _make_d1_result(total_ms=300.0, orin_ms=260.0, net_ms=40.0, source=SOURCE_RULE, fallback_reason=FALLBACK_TIMEOUT),
    )
    run = ReplayRun(
        tick_count=10,
        records=tuple(_make_record(result, float(index)) for index, result in enumerate(results)),
        warmup_results=(),
    )

    lines = format_summary_lines(run, "x.jsonl", "d1")

    total_line = next(line for line in lines if line.startswith("LATENCY total_ms"))
    assert total_line.split()[1:] == ["total_ms", "3", "200.00", "300.00", "300.00"]
    assert "SOURCES d1=2 rule=1" in lines
    assert "TOKENS n=3 p50=312 max=312" in lines
    fallback_line = next(line for line in lines if line.startswith("FALLBACK "))
    assert " timeout=1 " in fallback_line


def test_latency_table_columns_are_aligned():
    run, _ = _replay_fixture(PATROL_FIXTURE)

    latency_lines = [line for line in format_summary_lines(run, "p.jsonl", "rule") if line.startswith("LATENCY ")]

    assert len({len(line) for line in latency_lines}) == 1


# --- CSV ---


def test_write_csv_one_row_per_selection(tmp_path):
    run, _ = _replay_fixture(CHAIN_FIXTURE, warmup_count=1)
    csv_path = tmp_path / "rule.csv"

    write_csv(run, csv_path)

    with open(csv_path, newline="", encoding="utf-8") as csv_file:
        rows = list(csv.reader(csv_file))
    assert tuple(rows[0]) == CSV_COLUMNS
    data_rows = [dict(zip(CSV_COLUMNS, row)) for row in rows[1:]]
    assert len(data_rows) == len(EXPECTED_CHAIN_EVENTS)
    for row in data_rows:
        assert row["orin_processing_ms"] == ""
        assert row["network_rtt_ms"] == ""
        assert row["input_tokens"] == ""
        assert "," not in row["options"]
        assert isinstance(json.loads(row["params"]), dict)


def test_write_csv_joins_options_with_pipe(tmp_path):
    run = ReplayRun(tick_count=1, records=(_make_record(_make_d1_result()),), warmup_results=())
    csv_path = tmp_path / "d1.csv"

    write_csv(run, csv_path)

    with open(csv_path, newline="", encoding="utf-8") as csv_file:
        row = dict(zip(CSV_COLUMNS, list(csv.reader(csv_file))[1]))
    assert row["options"] == "search|hold"
    assert row["orin_processing_ms"] == "100.0"
    assert row["network_rtt_ms"] == "20.0"
    assert row["input_tokens"] == "312"


# --- factory 지연 import 계약 ---


def test_factory_is_imported_only_inside_main():
    tree = ast.parse((WAVEPILOT_ROOT / "modules/decision/replay.py").read_text(encoding="utf-8"))

    top_level_modules = [
        alias.name if isinstance(node, ast.Import) else node.module
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert FACTORY_MODULE not in top_level_modules

    main_function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    main_imports = [node.module for node in ast.walk(main_function) if isinstance(node, ast.ImportFrom)]
    assert FACTORY_MODULE in main_imports


# --- CLI ---

requires_factory = pytest.mark.skipif(
    importlib.util.find_spec(FACTORY_MODULE) is None, reason="plan 01-05 factory not merged yet"
)


@pytest.fixture
def clean_selector_env(monkeypatch):
    monkeypatch.delenv("WAVEPILOT_SELECTOR", raising=False)
    monkeypatch.delenv("D1_SERVER_URL", raising=False)


@requires_factory
def test_main_rule_prints_selections_and_writes_csv(tmp_path, capsys, clean_selector_env):
    csv_path = tmp_path / "rule.csv"

    exit_code = main([str(PATROL_FIXTURE), "--selector", "rule", "--csv", str(csv_path)])

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert len([line for line in stdout.splitlines() if line.startswith("SELECT ")]) == 4
    assert "LATENCY" in stdout
    with open(csv_path, newline="", encoding="utf-8") as csv_file:
        assert len(list(csv.reader(csv_file))) == 1 + 4


@requires_factory
def test_main_follows_selector_env(monkeypatch, capsys, clean_selector_env):
    monkeypatch.setenv("WAVEPILOT_SELECTOR", "rule")

    assert main([str(PATROL_FIXTURE)]) == 0


@requires_factory
def test_main_missing_fixture_is_usage_error(tmp_path, clean_selector_env):
    assert main([str(tmp_path / "missing.jsonl"), "--selector", "rule"]) == 2


def test_main_rejects_negative_warmup():
    # 인자 검증은 factory 보다 먼저 일어나므로 factory 없이도 돈다
    with pytest.raises(SystemExit) as exit_info:
        main([str(PATROL_FIXTURE), "--warmup", "-1"])

    assert exit_info.value.code == 2
