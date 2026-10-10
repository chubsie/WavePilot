"""재생 하네스 — 틱 시퀀스를 선택기에 흘려 SEL-05 와 SEL-02 를 확인한다.

목적:
- SEL-05: 선택기는 사건이 있을 때만 불린다. 질문 수 == 사건 수로 증명한다.
- SEL-02(성공 기준 2): 여러 번 돌린 지연 분포를 한눈에 본다 — 선택마다 SELECT 줄, 끝에 요약 표.

픽스처 줄 모양(JSONL, 키는 정확히 2개, 이 순서):
    {"state": <SelectionState.to_dict()>, "mode_result": null | <ModeResult.to_dict()>}
mode_result 는 01-01 ModeResult 계약(키 rule_mode / outcome / track)을 그대로 쓰고, 검증도 ModeResult.from_dict 에
맡긴다. 틱 시각은 state.time_sec 이다(별도 시각 키 없음).

픽스처 (modules/decision/fixtures/):
| 파일                           | 틱 | 기대 사건                                                                   |
|--------------------------------|----|-----------------------------------------------------------------------------|
| patrol_no_target.jsonl         | 35 | 4 — periodic_check@0, @10, @20, mode_ended@25 (car 는 대상이 아니다)          |
| target_found_mode_chain.jsonl  | 30 | 6 — periodic_check@0, new_target@3, mode_ended@8, @12, @18, periodic_check@28 |

실행 예시 (WavePilot 루트에서):
    python -m modules.decision.replay modules/decision/fixtures/patrol_no_target.jsonl --selector rule
    python -m modules.decision.replay modules/decision/fixtures/target_found_mode_chain.jsonl --selector rule --csv /tmp/rule.csv
    D1_SERVER_URL=http://<orin-ip>:8081 python -m modules.decision.replay \
        modules/decision/fixtures/target_found_mode_chain.jsonl --selector d1 --warmup 1 --csv /tmp/d1.csv

같은 픽스처를 --selector rule 과 --selector d1 로 돌리면 Phase 6 비교를 작게 미리 해 보는 셈이다.
orin_ms 는 서버가 timings 를 주지 않아 total − /health RTT 로 낸 추정치다 (RESEARCH §2.3).

선택기 조립: --selector 가 없으면 WAVEPILOT_SELECTOR 를 따르고, d1 서버 주소는 D1_SERVER_URL 이다(01-05 factory 가 읽는다).
factory(modules.decision.factory)는 main() 안에서만 지연 import 한다. 01-05 와 같은 웨이브에서 병렬로 만들어지므로
run_replay 와 출력·CSV 함수, 그 테스트가 factory 없이 돌아야 하기 때문이다.
"""

import argparse
import csv
import importlib.util
import json
import logging
import math
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

from modules.decision.interface.selector import (
    FALLBACK_REASONS,
    SOURCES,
    DecisionModel,
    SelectionResult,
)
from modules.decision.interface.state import (
    EVENT_KINDS,
    EVENT_PERIODIC_CHECK,
    ModeResult,
    SelectionEvent,
    SelectionState,
)
from modules.decision.trigger import SelectionTrigger

TICK_KEYS = ("state", "mode_result")
DEFAULT_WARMUP_COUNT = 0

SELECTOR_ENV_NAME = "WAVEPILOT_SELECTOR"
SELECTOR_CHOICES = ("rule", "d1")
FACTORY_MODULE = "modules.decision.factory"

PERCENTILES = (50, 90)
# 표 행 이름과 Latency 필드를 한 곳에 묶는다. 규칙 결과의 orin/network 는 None 이라 표본에서 빠진다
LATENCY_COMPONENTS = (
    ("total_ms", attrgetter("total_ms")),
    ("orin_ms", attrgetter("orin_processing_ms")),
    ("network_ms", attrgetter("network_rtt_ms")),
)
LATENCY_NAME_WIDTH = 12
LATENCY_COUNT_WIDTH = 5
LATENCY_VALUE_WIDTH = 10
MISSING_VALUE = "-"

CSV_COLUMNS = (
    "tick_index", "time_sec", "event", "options", "rule_mode", "source", "fallback_reason", "confidence",
    "orin_processing_ms", "network_rtt_ms", "total_ms", "input_tokens", "params",
)
CSV_OPTION_SEPARATOR = "|"  # 쉼표 열 구분자와 섞이지 않게

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

logger = logging.getLogger("modules.decision.replay")


@dataclass(frozen=True, slots=True)
class ReplayTick:
    line_no: int  # 픽스처 파일의 1부터 센 줄 번호
    state: SelectionState
    mode_result: ModeResult | None


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    tick_index: int  # ticks 안의 위치
    time_sec: float  # 그 틱의 state.time_sec — 출력·CSV 가 틱 목록 없이 시각을 쓰게 한다
    event: SelectionEvent
    result: SelectionResult


@dataclass(frozen=True, slots=True)
class ReplayRun:
    tick_count: int
    records: tuple[ReplayRecord, ...]  # 사건마다 1개 — warm-up 은 빠진다
    warmup_results: tuple[SelectionResult, ...]


# --- 픽스처 읽기 ---


def load_ticks(path: Path) -> tuple[ReplayTick, ...]:
    ticks: list[ReplayTick] = []
    with open(path, encoding="utf-8") as fixture_file:
        for line_no, line in enumerate(fixture_file, start=1):
            if line.strip() == "":
                continue
            ticks.append(_parse_tick(path, line_no, line))

    if not ticks:
        raise ValueError(f"{path}: no ticks found")

    return tuple(ticks)


def _parse_tick(path: Path, line_no: int, line: str) -> ReplayTick:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: line {line_no}: invalid JSON: {error}") from error

    if not isinstance(row, dict):
        raise ValueError(f"{path}: line {line_no}: tick must be a JSON object, got {type(row).__name__}")
    if set(row) != set(TICK_KEYS):
        raise ValueError(f"{path}: line {line_no}: tick keys must be {list(TICK_KEYS)}, got {sorted(row)}")

    # mode_result 키 규약은 01-01 ModeResult 한 곳에만 둔다 — 여기서는 검사하지 않고 from_dict 에 맡긴다
    try:
        state = SelectionState.from_dict(row["state"])
        raw_mode_result = row["mode_result"]
        mode_result = None if raw_mode_result is None else ModeResult.from_dict(raw_mode_result)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{path}: line {line_no}: {error}") from error

    return ReplayTick(line_no=line_no, state=state, mode_result=mode_result)


# --- 재생 ---


def run_replay(
    ticks: Sequence[ReplayTick],
    selector: DecisionModel,
    trigger: SelectionTrigger,
    warmup_count: int = DEFAULT_WARMUP_COUNT,
    on_record: Callable[[ReplayRecord], None] | None = None,
    on_warmup: Callable[[SelectionResult, float], None] | None = None,
) -> ReplayRun:
    """selector 는 DecisionModel 로만 다룬다(D-06) — rule 인지 d1 인지 모른다.

    출력은 콜백으로 떼어 내서 재생 코어를 출력 없이 테스트한다.
    """
    if warmup_count < 0:
        raise ValueError(f"warmup_count must be >= 0, got {warmup_count}")
    if not ticks:
        raise ValueError("ticks must not be empty")

    # warm-up 은 측정·CSV·호출 수에서 빠지고 trigger 를 건드리지 않는다.
    # 첫 요청의 CUDA 초기화 지연이 통계를 오염시키지 않게 한다 (RESEARCH Pitfall 6)
    first_state = ticks[0].state
    warmup_event = SelectionEvent(EVENT_PERIODIC_CHECK, None)
    warmup_results: list[SelectionResult] = []
    for _ in range(warmup_count):
        result = selector.select(first_state, warmup_event)
        warmup_results.append(result)
        if on_warmup is not None:
            on_warmup(result, first_state.time_sec)

    records: list[ReplayRecord] = []
    for tick_index, tick in enumerate(ticks):
        event = trigger.poll(tick.state.time_sec, tick.state, tick.mode_result)
        # 사건이 없으면 질문하지 않는다 — SEL-05 의 핵심
        if event is None:
            continue

        result = selector.select(tick.state, event)
        record = ReplayRecord(tick_index=tick_index, time_sec=tick.state.time_sec, event=event, result=result)
        records.append(record)
        if on_record is not None:
            on_record(record)

    return ReplayRun(tick_count=len(ticks), records=tuple(records), warmup_results=tuple(warmup_results))


# --- 선택 줄 ---


def format_selection_line(record: ReplayRecord) -> str:
    return "SELECT " + _format_result_fields(record.time_sec, record.event.kind, record.result)


def format_warmup_line(result: SelectionResult, time_sec: float) -> str:
    return "WARMUP " + _format_result_fields(time_sec, EVENT_PERIODIC_CHECK, result)


def _format_result_fields(time_sec: float, kind: str, result: SelectionResult) -> str:
    latency = result.latency
    input_tokens = MISSING_VALUE if result.input_tokens is None else str(result.input_tokens)
    # params 를 json.dumps 로 쓰면 문자열 값의 줄바꿈이 이스케이프되어 1 선택 = 1 줄이 된다 — 로그 위조 방지
    params = json.dumps(result.params, separators=(",", ":"), sort_keys=True)

    return (
        f"t={time_sec:.1f}s event={kind} options={','.join(result.options)} rule_mode={result.rule_mode} "
        f"source={result.source} fallback={result.fallback_reason} confidence={result.confidence:.2f} "
        f"orin_ms={_format_optional_ms(latency.orin_processing_ms)} net_ms={_format_optional_ms(latency.network_rtt_ms)} "
        f"total_ms={latency.total_ms:.2f} input_tokens={input_tokens} params={params}"
    )


def _format_optional_ms(value: float | None) -> str:
    return MISSING_VALUE if value is None else f"{value:.2f}"


# --- 요약 표 ---


def calculate_percentile(values: Sequence[float], percent: float) -> float:
    """nearest-rank 백분위수.

    보간하지 않는다 — 표본이 수십 개라 실제 관측값을 보여 주는 편이 읽기 쉽고 결정적이다.
    """
    if not values:
        raise ValueError("values must not be empty")

    sorted_values = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(sorted_values)) - 1)
    return sorted_values[index]


def format_summary_lines(run: ReplayRun, fixture_name: str, selector_name: str) -> list[str]:
    results = [record.result for record in run.records]
    selection_count = len(results)

    event_counts = {kind: 0 for kind in EVENT_KINDS}
    source_counts = {source: 0 for source in SOURCES}
    fallback_counts = {reason: 0 for reason in FALLBACK_REASONS}
    for record in run.records:
        event_counts[record.event.kind] += 1
        source_counts[record.result.source] += 1
        fallback_counts[record.result.fallback_reason] += 1

    return [
        f"REPLAY fixture={fixture_name} selector={selector_name} ticks={run.tick_count} "
        f"selections={selection_count} question_ratio={selection_count / run.tick_count:.3f} "
        f"warmup={len(run.warmup_results)}",
        "EVENTS " + _format_counts(event_counts),
        "SOURCES " + _format_counts(source_counts),
        *_format_latency_table(results),
        _format_token_line(results),
        "FALLBACK " + _format_counts(fallback_counts),
    ]


def _format_counts(counts: dict[str, int]) -> str:
    return " ".join(f"{name}={count}" for name, count in counts.items())


def _format_latency_table(results: Sequence[SelectionResult]) -> list[str]:
    header = (
        "LATENCY "
        + "component".ljust(LATENCY_NAME_WIDTH)
        + "n".rjust(LATENCY_COUNT_WIDTH)
        + "".join(f"p{percent}_ms".rjust(LATENCY_VALUE_WIDTH) for percent in PERCENTILES)
        + "max_ms".rjust(LATENCY_VALUE_WIDTH)
    )

    rows = []
    for name, get_value in LATENCY_COMPONENTS:
        values = [value for value in (get_value(result.latency) for result in results) if value is not None]
        if values:
            cells = [f"{calculate_percentile(values, percent):.2f}" for percent in PERCENTILES] + [f"{max(values):.2f}"]
        else:
            cells = [MISSING_VALUE] * (len(PERCENTILES) + 1)
        rows.append(
            "LATENCY "
            + name.ljust(LATENCY_NAME_WIDTH)
            + str(len(values)).rjust(LATENCY_COUNT_WIDTH)
            + "".join(cell.rjust(LATENCY_VALUE_WIDTH) for cell in cells)
        )

    return [header, *rows]


def _format_token_line(results: Sequence[SelectionResult]) -> str:
    # usage.input_tokens 로 토큰 수와 지연의 관계를 실측한다 (RESEARCH A3)
    token_counts = [result.input_tokens for result in results if result.input_tokens is not None]
    if not token_counts:
        return f"TOKENS n=0 p50={MISSING_VALUE} max={MISSING_VALUE}"

    return f"TOKENS n={len(token_counts)} p50={int(calculate_percentile(token_counts, 50))} max={max(token_counts)}"


# --- CSV ---


def write_csv(run: ReplayRun, path: Path) -> None:
    """선택 1회 = 1행 (warm-up 제외). 숫자는 반올림하지 않는다 — 반올림은 표에서만 한다."""
    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_COLUMNS)
        for record in run.records:
            writer.writerow(_build_csv_row(record))


def _build_csv_row(record: ReplayRecord) -> list[object]:
    result = record.result
    latency = result.latency
    return [
        record.tick_index,
        record.time_sec,
        record.event.kind,
        CSV_OPTION_SEPARATOR.join(result.options),
        result.rule_mode,
        result.source,
        result.fallback_reason,
        result.confidence,
        _format_optional_cell(latency.orin_processing_ms),
        _format_optional_cell(latency.network_rtt_ms),
        latency.total_ms,
        _format_optional_cell(result.input_tokens),
        json.dumps(result.params, separators=(",", ":"), sort_keys=True),
    ]


def _format_optional_cell(value: object | None) -> object:
    return "" if value is None else value


# --- CLI ---


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.decision.replay",
        description="Replay a tick fixture through a rule_mode selector and print per-selection latency and a summary.",
    )
    parser.add_argument("fixture", help="path to a tick fixture JSONL file")
    parser.add_argument(
        "--selector", choices=SELECTOR_CHOICES, default=None,
        help=f"selector kind; if omitted, follows {SELECTOR_ENV_NAME}. d1 requires D1_SERVER_URL",
    )
    parser.add_argument("--csv", type=Path, default=None, help="write one CSV row per selection to this path")
    parser.add_argument(
        "--warmup", type=int, default=DEFAULT_WARMUP_COUNT,
        help="unmeasured selections before replay (1 recommended for d1)",
    )
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG, INFO, WARNING, ...)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    logging.basicConfig(
        stream=sys.stderr, level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        ticks = load_ticks(Path(args.fixture))
    except (FileNotFoundError, ValueError) as error:
        logger.error("invalid fixture %s: %s", args.fixture, error)
        return EXIT_USAGE

    # 지연 import — 01-05 factory 가 없어도 이 모듈의 나머지(run_replay·출력·CSV)는 돌아야 한다
    if importlib.util.find_spec(FACTORY_MODULE) is None:
        logger.error("selector factory not available: %s (plan 01-05)", FACTORY_MODULE)
        return EXIT_FAILURE
    from modules.decision.factory import SelectorConfig, create_decision_model

    environ = dict(os.environ)
    if args.selector is not None:
        environ[SELECTOR_ENV_NAME] = args.selector
    try:
        config = SelectorConfig.from_env(environ)
    except ValueError as error:
        logger.error("invalid selector config: %s", error)
        return EXIT_USAGE
    selector = create_decision_model(config)

    try:
        run = run_replay(
            ticks, selector, SelectionTrigger(), args.warmup,
            on_record=lambda record: print(format_selection_line(record), flush=True),
            on_warmup=lambda result, time_sec: print(format_warmup_line(result, time_sec), flush=True),
        )
    except KeyboardInterrupt:
        logger.info("interrupted")
        return EXIT_INTERRUPTED

    for line in format_summary_lines(run, Path(args.fixture).name, config.kind):
        print(line)
    if args.csv is not None:
        write_csv(run, args.csv)
        logger.info("csv written: %s rows=%d", args.csv, len(run.records))

    return EXIT_OK


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # parser.error 는 종료 코드 2로 끝낸다
    if args.warmup < 0:
        parser.error(f"--warmup must be >= 0, got {args.warmup}")


if __name__ == "__main__":
    sys.exit(main())
