"""선택기 비교 집계 — 정답 dict + run_mission JSONL → 실행 1회 판정 → 비교표 파일 (CMP-01).

06-05 의 run_compare 가 실행마다 evaluate_run(또는 프로세스 실패·타임아웃이면 aborted_run)을 부르고,
끝에 write_summary 로 summary.md·summary.csv 를 쓴다. 시작 전 서버가 없으면 write_server_missing 만 쓴다 (D-14).

입력 형식(06-OUTLINE C1·C3·C4)만 알면 되므로 표준 라이브러리만 쓴다 — 시나리오 모듈·mission_log 를 import 하지 않는다.
- 성공(D-09): end_reason "landed" + forced_return false + 오발사 0 + 맞힌 빨강 = 정답 빨강 전부.
  빨강 없는 시나리오는 연사 0 이어야 성공.
- 연사(D-10): 같은 track_id 의 연속된 hit 기록 = 1회 발사, 맞은 배우 = null 이 아닌 actor 다수결(동률은 먼저 맞은 것).
- 소요 시간(D-11): mission end 기록의 elapsed.
"""

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

KIND_MISSION_RECORD = "mission"
KIND_SELECTION_RECORD = "selection"
KIND_HIT_RECORD = "hit"
EVENT_END = "end"
END_LANDED = "landed"

STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
UNKNOWN_RETURN_REASON = "unknown"
NO_END_RECORD_DETAIL = "no mission end record"

SELECTOR_D1 = "d1"
TOTAL_SCENARIO_LABEL = "ALL"
FALLBACK_REASON_LIMIT = 3
SUMMARY_MD_NAME = "summary.md"
SUMMARY_CSV_NAME = "summary.csv"
SERVER_DISPLAY_NAMES = {"d1": "d1", "yolo": "YOLO"}

MD_TABLE_HEADER = (
    "시나리오",
    "선택기",
    "실행",
    "성공률",
    "평균 소요 시간 s",
    "맞힌 빨강",
    "오발사",
    "빗나감",
    "비상 복귀(사유)",
    "d1 fallback(사유)",
    "중단",
)
CSV_HEADER = (
    "scenario",
    "selector",
    "runs",
    "completed",
    "successes",
    "success_rate",
    "avg_elapsed_sec",
    "red_hit",
    "red_total",
    "misfires",
    "misses",
    "forced_returns",
    "forced_return_reasons",
    "selections",
    "fallbacks",
    "fallback_rate",
    "fallback_reasons",
    "aborted",
)
SERVER_MISSING_CSV_HEADER = ("status", "server", "url", "error", "checked_at")
STATUS_SERVER_MISSING = "server_missing"

logger = logging.getLogger("modules.drone.compare_report")


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """실행 1회(시나리오 × 선택기 × 반복)의 판정 결과 (계약 C5)."""

    scenario_id: str
    selector: str
    repeat: int
    status: str
    is_success: bool
    elapsed_sec: float | None
    red_hit: int
    red_total: int
    misfires: int
    misses: int
    forced_return_reason: str | None
    selections: int
    fallbacks: int
    fallback_reasons: tuple[str, ...]
    abort_detail: str | None


# ---------------------------------------------------------------- 실행 1회 판정


def group_bursts(hit_records: list[dict]) -> list[str | None]:
    """hit 기록을 순서대로 읽어 같은 track_id 의 연속된 발을 1회 연사로 묶고, 연사별 맞은 actor id 를 돌려준다."""
    bursts: list[list[str | None]] = []
    previous_track_id = object()

    for record in hit_records:
        track_id = record.get("track_id")
        if not bursts or track_id != previous_track_id:
            bursts.append([])
        bursts[-1].append(record.get("actor"))
        previous_track_id = track_id

    return [_pick_burst_actor(actors) for actors in bursts]


def _pick_burst_actor(actors: list[str | None]) -> str | None:
    """null 을 뺀 actor 다수결 — 동률이면 먼저 맞은 것, 전부 null 이면 None (빗나감)."""
    hit_actors = [actor for actor in actors if actor is not None]
    if not hit_actors:
        return None

    counts = Counter(hit_actors)
    top_count = max(counts.values())

    return next(actor for actor in hit_actors if counts[actor] == top_count)


def evaluate_run(truth: dict, records: list[dict], scenario_id: str, selector: str, repeat: int) -> RunOutcome:
    """정답 dict + 실행 JSONL 기록 → RunOutcome (D-09, D-10, D-11, D-13)."""
    end_record = _find_end_record(records)
    if end_record is None:
        return aborted_run(scenario_id, selector, repeat, NO_END_RECORD_DETAIL)

    red_ids = set(truth.get("red_ids", ()))
    hit_records = [record for record in records if record.get("kind") == KIND_HIT_RECORD]
    burst_actors = group_bursts(hit_records)
    hit_red_ids, misfires, misses = _classify_bursts(burst_actors, red_ids)

    forced_return_reason = None
    if end_record.get("forced_return"):
        forced_return_reason = end_record.get("return_reason") or UNKNOWN_RETURN_REASON

    selections, fallback_reasons = _count_fallbacks(records)

    if red_ids:
        targets_done = hit_red_ids == red_ids
    else:
        targets_done = not burst_actors
    is_success = (
        end_record.get("end_reason") == END_LANDED
        and forced_return_reason is None
        and misfires == 0
        and targets_done
    )

    elapsed = end_record.get("elapsed")

    return RunOutcome(
        scenario_id=scenario_id,
        selector=selector,
        repeat=repeat,
        status=STATUS_COMPLETED,
        is_success=is_success,
        elapsed_sec=float(elapsed) if elapsed is not None else None,
        red_hit=len(hit_red_ids),
        red_total=len(red_ids),
        misfires=misfires,
        misses=misses,
        forced_return_reason=forced_return_reason,
        selections=selections,
        fallbacks=len(fallback_reasons),
        fallback_reasons=fallback_reasons,
        abort_detail=None,
    )


def _find_end_record(records: list[dict]) -> dict | None:
    """마지막 mission end 기록 — 없으면 None (프로세스가 중간에 죽은 실행)."""
    end_records = [
        record
        for record in records
        if record.get("kind") == KIND_MISSION_RECORD and record.get("event") == EVENT_END
    ]

    return end_records[-1] if end_records else None


def _classify_bursts(burst_actors: list[str | None], red_ids: set[str]) -> tuple[set[str], int, int]:
    """연사별 맞은 actor → (맞힌 빨강 id 집합, 오발사 수, 빗나감 수). 정답에 없는 id 도 오발사다."""
    hit_red_ids: set[str] = set()
    misfires = 0
    misses = 0

    for actor in burst_actors:
        if actor is None:
            misses += 1
        elif actor in red_ids:
            hit_red_ids.add(actor)
        else:
            misfires += 1

    return hit_red_ids, misfires, misses


def _count_fallbacks(records: list[dict]) -> tuple[int, tuple[str, ...]]:
    """selection 기록 수와 fallback 사유 목록(기록 순서)."""
    selection_records = [record for record in records if record.get("kind") == KIND_SELECTION_RECORD]
    fallback_reasons = tuple(
        str(record["fallback"]) for record in selection_records if record.get("fallback") is not None
    )

    return len(selection_records), fallback_reasons


def aborted_run(scenario_id: str, selector: str, repeat: int, detail: str) -> RunOutcome:
    """끝까지 가지 못한 실행(프로세스 실패·타임아웃·로그 없음) — 성공률 분모에서 빠진다."""
    return RunOutcome(
        scenario_id=scenario_id,
        selector=selector,
        repeat=repeat,
        status=STATUS_ABORTED,
        is_success=False,
        elapsed_sec=None,
        red_hit=0,
        red_total=0,
        misfires=0,
        misses=0,
        forced_return_reason=None,
        selections=0,
        fallbacks=0,
        fallback_reasons=(),
        abort_detail=detail,
    )


def read_jsonl(path: Path) -> list[dict]:
    """JSONL 파일 → dict 목록. 빈 줄·깨진 줄·dict 가 아닌 줄은 건너뛴다. 파일 없음은 그대로 FileNotFoundError."""
    records: list[dict] = []

    with Path(path).open(encoding="utf-8") as jsonl_file:
        for line_number, line in enumerate(jsonl_file, start=1):
            text = line.strip()
            if not text:
                continue

            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                record = None
            if not isinstance(record, dict):
                logger.warning("skip invalid jsonl line %s:%d", path, line_number)
                continue

            records.append(record)

    return records


# ---------------------------------------------------------------- 비교표 집계


@dataclass(frozen=True, slots=True)
class _GroupStats:
    """실행 묶음(시나리오×선택기 또는 선택기 합계) 하나의 합계."""

    runs: int
    completed: int
    successes: int
    elapsed_total: float
    elapsed_count: int
    red_hit: int
    red_total: int
    misfires: int
    misses: int
    forced_reasons: Counter
    selections: int
    fallbacks: int
    fallback_reasons: Counter
    aborted: int


def _aggregate(outcomes: list[RunOutcome]) -> _GroupStats:
    """시나리오×선택기 행과 선택기 합계 행이 같이 쓰는 집계 — 성공률 분모는 완료 실행(중단 제외)."""
    completed = [outcome for outcome in outcomes if outcome.status == STATUS_COMPLETED]
    elapsed_values = [outcome.elapsed_sec for outcome in completed if outcome.elapsed_sec is not None]
    forced_reasons = Counter(
        outcome.forced_return_reason for outcome in outcomes if outcome.forced_return_reason is not None
    )
    fallback_reasons = Counter(reason for outcome in outcomes for reason in outcome.fallback_reasons)

    return _GroupStats(
        runs=len(outcomes),
        completed=len(completed),
        successes=sum(1 for outcome in completed if outcome.is_success),
        elapsed_total=sum(elapsed_values),
        elapsed_count=len(elapsed_values),
        red_hit=sum(outcome.red_hit for outcome in outcomes),
        red_total=sum(outcome.red_total for outcome in outcomes),
        misfires=sum(outcome.misfires for outcome in outcomes),
        misses=sum(outcome.misses for outcome in outcomes),
        forced_reasons=forced_reasons,
        selections=sum(outcome.selections for outcome in outcomes),
        fallbacks=sum(outcome.fallbacks for outcome in outcomes),
        fallback_reasons=fallback_reasons,
        aborted=len(outcomes) - len(completed),
    )


def _group_rows(outcomes: list[RunOutcome]) -> list[tuple[str, str, bool, _GroupStats]]:
    """(시나리오, 선택기, 합계 행 여부, 집계) 목록 — 처음 나온 시나리오 × 처음 나온 선택기 순서, 뒤에 선택기별 합계."""
    by_scenario_selector: dict[tuple[str, str], list[RunOutcome]] = {}
    by_selector: dict[str, list[RunOutcome]] = {}
    for outcome in outcomes:
        by_scenario_selector.setdefault((outcome.scenario_id, outcome.selector), []).append(outcome)
        by_selector.setdefault(outcome.selector, []).append(outcome)

    scenario_order = list(dict.fromkeys(outcome.scenario_id for outcome in outcomes))
    selector_order = list(by_selector)

    rows = [
        (scenario_id, selector, False, _aggregate(by_scenario_selector[(scenario_id, selector)]))
        for scenario_id in scenario_order
        for selector in selector_order
        if (scenario_id, selector) in by_scenario_selector
    ]
    rows += [
        (TOTAL_SCENARIO_LABEL, selector, True, _aggregate(by_selector[selector]))
        for selector in selector_order
    ]

    return rows


def _format_ratio(numerator: int, denominator: int) -> str:
    """"2/3 (67%)" — 분모 0 이면 "-"."""
    if denominator == 0:
        return "-"

    return f"{numerator}/{denominator} ({round(100 * numerator / denominator)}%)"


def _format_reason_counts(counter: Counter, limit: int | None = None, separator: str = ", ") -> str:
    """사유별 개수 "battery:1, geofence:1" — 많은 순, 같은 개수는 사유 이름순."""
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None:
        ordered = ordered[:limit]

    return separator.join(f"{reason}:{count}" for reason, count in ordered)


def _format_average_elapsed(stats: _GroupStats) -> str:
    if stats.elapsed_count == 0:
        return ""

    return f"{stats.elapsed_total / stats.elapsed_count:.1f}"


def _md_row_cells(scenario_id: str, selector: str, stats: _GroupStats) -> list[str]:
    """Markdown 표 한 행의 칸 문자열 (MD_TABLE_HEADER 순서)."""
    forced_count = sum(stats.forced_reasons.values())
    forced_cell = str(forced_count)
    if forced_count:
        forced_cell = f"{forced_count} ({_format_reason_counts(stats.forced_reasons)})"

    fallback_cell = "-"
    if selector == SELECTOR_D1:
        fallback_cell = _format_ratio(stats.fallbacks, stats.selections)
        if stats.selections == 0:
            fallback_cell = "0/0"
        elif stats.fallbacks:
            fallback_cell += " " + _format_reason_counts(stats.fallback_reasons, FALLBACK_REASON_LIMIT)

    red_cell = f"{stats.red_hit}/{stats.red_total}" if stats.red_total else "-"

    return [
        scenario_id,
        selector,
        str(stats.runs),
        _format_ratio(stats.successes, stats.completed),
        _format_average_elapsed(stats) or "-",
        red_cell,
        str(stats.misfires),
        str(stats.misses),
        forced_cell,
        fallback_cell,
        str(stats.aborted),
    ]


def _csv_row(scenario_id: str, selector: str, stats: _GroupStats) -> list[str | int]:
    """CSV 한 행 — 숫자 원값 (CSV_HEADER 순서). 계산할 수 없는 비율은 빈 칸."""
    success_rate = f"{stats.successes / stats.completed:.3f}" if stats.completed else ""
    fallback_rate = ""
    if selector == SELECTOR_D1 and stats.selections:
        fallback_rate = f"{stats.fallbacks / stats.selections:.3f}"

    return [
        scenario_id,
        selector,
        stats.runs,
        stats.completed,
        stats.successes,
        success_rate,
        _format_average_elapsed(stats),
        stats.red_hit,
        stats.red_total,
        stats.misfires,
        stats.misses,
        sum(stats.forced_reasons.values()),
        _format_reason_counts(stats.forced_reasons, separator=";"),
        stats.selections,
        stats.fallbacks,
        fallback_rate,
        _format_reason_counts(stats.fallback_reasons, separator=";"),
        stats.aborted,
    ]


# ---------------------------------------------------------------- 파일 쓰기


def write_summary(out_dir: Path, outcomes: list[RunOutcome]) -> tuple[Path, Path]:
    """시나리오×선택기 행 + 선택기별 합계 행으로 summary.md·summary.csv 를 쓴다 (D-12, D-13)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / SUMMARY_MD_NAME
    csv_path = out_dir / SUMMARY_CSV_NAME

    rows = _group_rows(outcomes)

    md_lines = ["# 선택기 비교 결과", "", _md_table_line(MD_TABLE_HEADER), _md_table_line(["---"] * len(MD_TABLE_HEADER))]
    for scenario_id, selector, is_total, stats in rows:
        cells = _md_row_cells(scenario_id, selector, stats)
        if is_total:
            cells = [f"**{cell}**" for cell in cells]
        md_lines.append(_md_table_line(cells))

    aborted_outcomes = [outcome for outcome in outcomes if outcome.status == STATUS_ABORTED]
    if aborted_outcomes:
        md_lines += ["", "## 중단 실행", ""]
        md_lines += [
            f"- {outcome.scenario_id}/{outcome.selector}/r{outcome.repeat}: "
            f"{_escape_md_cell(outcome.abort_detail or '')}"
            for outcome in aborted_outcomes
        ]

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)
        for scenario_id, selector, _, stats in rows:
            writer.writerow(_csv_row(scenario_id, selector, stats))

    return md_path, csv_path


def write_server_missing(out_dir: Path, missing: list[tuple[str, str, str]], checked_at: str) -> tuple[Path, Path]:
    """서버가 없어 비교를 시작하지 않았다는 사실을 summary.md·summary.csv 에 남긴다 (D-14, ROADMAP SC4)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / SUMMARY_MD_NAME
    csv_path = out_dir / SUMMARY_CSV_NAME

    md_lines = ["# 선택기 비교 — 중단", ""]
    for server_name, url, error in missing:
        display_name = SERVER_DISPLAY_NAMES.get(server_name, server_name)
        md_lines += [
            f"**{display_name} 서버 없음 — 비교 중단**",
            "",
            f"- URL: {_escape_md_cell(url)}",
            f"- 오류: {_escape_md_cell(error)}",
            f"- 확인 시각: {_escape_md_cell(checked_at)}",
            "",
        ]
    md_lines.append("서버가 없어 비교를 시작하지 않았다 (D-14).")

    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(SERVER_MISSING_CSV_HEADER)
        for server_name, url, error in missing:
            writer.writerow([STATUS_SERVER_MISSING, server_name, url, error, checked_at])

    return md_path, csv_path


def _md_table_line(cells: list[str] | tuple[str, ...]) -> str:
    return "| " + " | ".join(_escape_md_cell(cell) for cell in cells) + " |"


def _escape_md_cell(text: str) -> str:
    """Markdown 칸 값 — '|' 는 '\\|', 줄바꿈은 공백 (오류 문자열이 표 구조를 깨지 않게)."""
    return str(text).replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("|", "\\|")
