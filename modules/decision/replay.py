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
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from modules.decision.interface.selector import DecisionModel, SelectionResult
from modules.decision.interface.state import EVENT_PERIODIC_CHECK, ModeResult, SelectionEvent, SelectionState
from modules.decision.trigger import SelectionTrigger

TICK_KEYS = ("state", "mode_result")
DEFAULT_WARMUP_COUNT = 0

logger = logging.getLogger("modules.decision.replay")


@dataclass(frozen=True, slots=True)
class ReplayTick:
    line_no: int  # 픽스처 파일의 1부터 센 줄 번호
    state: SelectionState
    mode_result: ModeResult | None


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    tick_index: int  # ticks 안의 위치
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

        record = ReplayRecord(tick_index=tick_index, event=event, result=selector.select(tick.state, event))
        records.append(record)
        if on_record is not None:
            on_record(record)

    return ReplayRun(tick_count=len(ticks), records=tuple(records), warmup_results=tuple(warmup_results))
