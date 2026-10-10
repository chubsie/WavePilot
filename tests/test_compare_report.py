"""compare_report 집계 테스트 — 합성 정답 dict·JSONL 기록으로 실행 판정과 비교표 파일을 검증한다 (CMP-01, D-09~D-14)."""

import csv
import json

from modules.drone.compare_report import (
    RunOutcome,
    aborted_run,
    evaluate_run,
    group_bursts,
    read_jsonl,
    write_server_missing,
    write_summary,
)


# ---------------------------------------------------------------- 헬퍼


def make_truth(red_ids, others=()):
    """C1 정답 dict 모양 — others 는 (id, kind, color) 튜플."""
    actors = [
        {"id": actor_id, "kind": "person", "color": "red", "north_m": 15.0, "east_m": 0.0, "walk": None}
        for actor_id in red_ids
    ]
    actors += [
        {"id": actor_id, "kind": kind, "color": color, "north_m": 20.0, "east_m": 3.0, "walk": None}
        for actor_id, kind, color in others
    ]

    return {"scenario": "test", "mission": "missions/compare.toml", "actors": actors, "red_ids": list(red_ids)}


def end_record(end_reason="landed", elapsed=61.5, forced_return=False, return_reason=None):
    return {
        "t": elapsed,
        "kind": "mission",
        "event": "end",
        "end_reason": end_reason,
        "elapsed": elapsed,
        "return_reason": return_reason,
        "forced_return": forced_return,
    }


def start_record(selector="rule"):
    return {"t": 0.0, "kind": "mission", "event": "start", "selector": selector}


def hit(track_id, actor):
    return {"t": 10.0, "kind": "hit", "track": f"T{track_id}", "track_id": track_id, "actor": actor}


def selection(fallback=None):
    return {"t": 5.0, "kind": "selection", "mode": "auto", "source": "d1", "confidence": 0.8, "fallback": fallback}


def make_outcome(scenario_id="basic", selector="rule", repeat=1, **overrides):
    fields = {
        "status": "completed",
        "is_success": True,
        "elapsed_sec": 60.0,
        "red_hit": 1,
        "red_total": 1,
        "misfires": 0,
        "misses": 0,
        "forced_return_reason": None,
        "selections": 0,
        "fallbacks": 0,
        "fallback_reasons": (),
        "abort_detail": None,
    }
    fields.update(overrides)

    return RunOutcome(scenario_id=scenario_id, selector=selector, repeat=repeat, **fields)


def md_cells(line):
    """Markdown 표 한 줄 → 칸 문자열 목록 (열 순서: 시나리오, 선택기, 실행, 성공률, 평균 소요 시간, 맞힌 빨강, 오발사, 빗나감, 비상 복귀, d1 fallback, 중단). 합계 행의 굵게 표시는 벗긴다."""
    return [cell.strip().strip("*") for cell in line.strip().strip("|").split("|")]


def read_csv_rows(path):
    with path.open(encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


# ---------------------------------------------------------------- group_bursts


def test_group_bursts_merges_consecutive_shots_of_same_track():
    records = [hit(3, "red_1"), hit(3, "red_1"), hit(3, None), hit(5, "car_1"), hit(5, "car_1")]

    assert group_bursts(records) == ["red_1", "car_1"]


def test_group_bursts_splits_when_other_track_interleaves():
    records = [hit(3, "a"), hit(4, "b"), hit(3, "a")]

    assert group_bursts(records) == ["a", "b", "a"]


def test_group_bursts_tie_goes_to_first_hit_actor():
    records = [hit(3, "blue_1"), hit(3, "red_1"), hit(3, "red_1"), hit(3, "blue_1")]

    assert group_bursts(records) == ["blue_1"]


def test_group_bursts_all_null_is_miss_and_empty_is_empty():
    assert group_bursts([hit(3, None), hit(3, None)]) == [None]
    assert group_bursts([]) == []


# ---------------------------------------------------------------- evaluate_run


def test_evaluate_run_success():
    truth = make_truth(["red_1"], [("blue_1", "person", "other")])
    records = [start_record(), hit(3, "red_1"), hit(3, "red_1"), end_record(elapsed=72.3)]

    outcome = evaluate_run(truth, records, "basic", "rule", 1)

    assert outcome.status == "completed"
    assert outcome.is_success is True
    assert (outcome.red_hit, outcome.red_total) == (1, 1)
    assert outcome.misfires == 0
    assert outcome.elapsed_sec == 72.3
    assert (outcome.scenario_id, outcome.selector, outcome.repeat) == ("basic", "rule", 1)
    assert outcome.abort_detail is None


def test_evaluate_run_partial_red_hit_is_not_success():
    truth = make_truth(["red_1", "red_2"])
    records = [hit(3, "red_1"), end_record()]

    outcome = evaluate_run(truth, records, "two_red", "rule", 1)

    assert outcome.is_success is False
    assert (outcome.red_hit, outcome.red_total) == (1, 2)


def test_evaluate_run_repeated_red_hit_counts_distinct_and_is_not_misfire():
    truth = make_truth(["red_1"])
    records = [hit(3, "red_1"), hit(4, "red_1"), end_record()]

    outcome = evaluate_run(truth, records, "basic", "rule", 1)

    assert outcome.red_hit == 1
    assert outcome.misfires == 0
    assert outcome.is_success is True


def test_evaluate_run_misfires_on_other_person_car_and_unknown_actor():
    truth = make_truth(["red_1"], [("blue_1", "person", "other"), ("car_1", "car", None)])
    records = [hit(1, "red_1"), hit(2, "blue_1"), hit(3, "car_1"), hit(4, "default_0"), end_record()]

    outcome = evaluate_run(truth, records, "basic", "rule", 1)

    assert outcome.misfires == 3
    assert outcome.red_hit == 1
    assert outcome.is_success is False


def test_evaluate_run_miss_does_not_affect_success_with_red():
    truth = make_truth(["red_1"])
    records = [hit(1, None), hit(2, "red_1"), end_record()]

    outcome = evaluate_run(truth, records, "basic", "rule", 1)

    assert outcome.misses == 1
    assert outcome.is_success is True


def test_evaluate_run_no_red_success_requires_zero_bursts():
    truth = make_truth([], [("blue_1", "person", "other")])

    clean = evaluate_run(truth, [end_record()], "no_red", "rule", 1)
    with_miss = evaluate_run(truth, [hit(1, None), end_record()], "no_red", "rule", 1)

    assert clean.is_success is True
    assert clean.red_total == 0
    assert with_miss.is_success is False
    assert with_miss.misses == 1


def test_evaluate_run_forced_return_is_failure_with_reason():
    truth = make_truth(["red_1"])
    battery = [hit(1, "red_1"), end_record(forced_return=True, return_reason="battery")]
    unknown = [hit(1, "red_1"), end_record(forced_return=True, return_reason=None)]

    battery_outcome = evaluate_run(truth, battery, "basic", "rule", 1)
    unknown_outcome = evaluate_run(truth, unknown, "basic", "rule", 1)

    assert battery_outcome.is_success is False
    assert battery_outcome.forced_return_reason == "battery"
    assert unknown_outcome.forced_return_reason == "unknown"


def test_evaluate_run_non_landed_end_is_completed_failure():
    truth = make_truth(["red_1"])
    records = [hit(1, "red_1"), end_record(end_reason="return_failed")]

    outcome = evaluate_run(truth, records, "basic", "rule", 1)

    assert outcome.status == "completed"
    assert outcome.is_success is False


def test_evaluate_run_counts_selections_and_fallbacks():
    truth = make_truth(["red_1"])
    records = [
        start_record("d1"),
        selection(),
        selection("timeout"),
        selection(),
        selection("timeout"),
        hit(1, "red_1"),
        end_record(),
    ]

    outcome = evaluate_run(truth, records, "basic", "d1", 1)

    assert outcome.selections == 4
    assert outcome.fallbacks == 2
    assert outcome.fallback_reasons == ("timeout", "timeout")


def test_evaluate_run_without_end_record_is_aborted():
    truth = make_truth(["red_1"])

    outcome = evaluate_run(truth, [start_record(), hit(1, "red_1")], "basic", "rule", 2)

    assert outcome.status == "aborted"
    assert outcome.is_success is False
    assert outcome.abort_detail == "no mission end record"
    assert outcome.repeat == 2


def test_aborted_run_has_zero_metrics():
    outcome = aborted_run("basic", "d1", 3, "timeout after 300s")

    assert outcome.status == "aborted"
    assert outcome.is_success is False
    assert outcome.elapsed_sec is None
    assert (outcome.red_hit, outcome.red_total, outcome.misfires, outcome.misses) == (0, 0, 0, 0)
    assert (outcome.selections, outcome.fallbacks, outcome.fallback_reasons) == (0, 0, ())
    assert outcome.forced_return_reason is None
    assert outcome.abort_detail == "timeout after 300s"


# ---------------------------------------------------------------- read_jsonl


def test_read_jsonl_skips_blank_truncated_and_non_dict_lines(tmp_path):
    path = tmp_path / "run.jsonl"
    lines = [
        json.dumps({"t": 0.0, "kind": "mission", "event": "start"}),
        "",
        json.dumps([1, 2, 3]),
        json.dumps({"t": 1.0, "kind": "hit", "track_id": 3, "actor": None}),
        '{"t": 2.0, "kind": "hi',
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    records = read_jsonl(path)

    assert [record["kind"] for record in records] == ["mission", "hit"]


def test_read_jsonl_missing_file_raises(tmp_path):
    try:
        read_jsonl(tmp_path / "missing.jsonl")
    except FileNotFoundError:
        return

    raise AssertionError("FileNotFoundError expected")


# ---------------------------------------------------------------- write_summary


def test_write_summary_returns_paths_and_creates_dir(tmp_path):
    out_dir = tmp_path / "nested" / "compare"

    md_path, csv_path = write_summary(out_dir, [make_outcome()])

    assert md_path == out_dir / "summary.md"
    assert csv_path == out_dir / "summary.csv"
    assert md_path.exists() and csv_path.exists()


def test_write_summary_row_order_and_total_rows(tmp_path):
    outcomes = [
        make_outcome("basic", "rule"),
        make_outcome("basic", "d1"),
        make_outcome("no_red", "rule", red_hit=0, red_total=0),
        make_outcome("no_red", "d1", red_hit=0, red_total=0),
        make_outcome("basic", "rule", repeat=2),
    ]

    _, csv_path = write_summary(tmp_path, outcomes)
    rows = read_csv_rows(csv_path)

    assert [(row["scenario"], row["selector"]) for row in rows] == [
        ("basic", "rule"),
        ("basic", "d1"),
        ("no_red", "rule"),
        ("no_red", "d1"),
        ("ALL", "rule"),
        ("ALL", "d1"),
    ]
    assert rows[0]["runs"] == "2"
    assert rows[4]["runs"] == "3"
    assert rows[4]["red_hit"] == "2"
    assert rows[4]["red_total"] == "2"


def test_write_summary_success_rate_and_average_elapsed(tmp_path):
    outcomes = [
        make_outcome(elapsed_sec=60.0),
        make_outcome(repeat=2, elapsed_sec=70.0),
        make_outcome(repeat=3, is_success=False, elapsed_sec=80.5),
    ]

    md_path, csv_path = write_summary(tmp_path, outcomes)
    row = read_csv_rows(csv_path)[0]
    md_text = md_path.read_text(encoding="utf-8")

    assert "2/3 (67%)" in md_text
    assert "70.2" in md_text
    assert row["success_rate"] == "0.667"
    assert row["avg_elapsed_sec"] == "70.2"
    assert (row["completed"], row["successes"]) == ("3", "2")


def test_write_summary_excludes_aborted_from_success_denominator(tmp_path):
    outcomes = [
        make_outcome(),
        make_outcome(repeat=2, is_success=False),
        aborted_run("basic", "rule", 3, "process exited with code 1"),
    ]

    md_path, csv_path = write_summary(tmp_path, outcomes)
    row = read_csv_rows(csv_path)[0]
    md_text = md_path.read_text(encoding="utf-8")

    assert "1/2 (50%)" in md_text
    assert row["aborted"] == "1"
    assert row["completed"] == "2"
    assert "중단 실행" in md_text
    assert "basic/rule/r3" in md_text
    assert "process exited with code 1" in md_text


def test_write_summary_all_aborted_shows_dash(tmp_path):
    outcomes = [aborted_run("basic", "rule", 1, "timeout")]

    md_path, csv_path = write_summary(tmp_path, outcomes)
    row = read_csv_rows(csv_path)[0]
    table_row = next(line for line in md_path.read_text(encoding="utf-8").splitlines() if line.startswith("| basic"))
    cells = md_cells(table_row)

    assert cells[3] == "-"
    assert cells[4] == "-"
    assert row["success_rate"] == ""
    assert row["avg_elapsed_sec"] == ""


def test_write_summary_red_ratio_and_no_red_dash(tmp_path):
    outcomes = [
        make_outcome("two_red", "rule", red_hit=2, red_total=2),
        make_outcome("two_red", "rule", repeat=2, red_hit=1, red_total=2, is_success=False),
        make_outcome("no_red", "rule", red_hit=0, red_total=0),
    ]

    md_path, _ = write_summary(tmp_path, outcomes)
    lines = md_path.read_text(encoding="utf-8").splitlines()
    two_red_line = next(line for line in lines if line.startswith("| two_red"))
    no_red_line = next(line for line in lines if line.startswith("| no_red"))
    total_line = next(line for line in lines if "**ALL**" in line)

    assert md_cells(two_red_line)[5] == "3/4"
    assert md_cells(no_red_line)[5] == "-"
    assert md_cells(total_line)[5] == "3/4"


def test_write_summary_forced_return_column(tmp_path):
    outcomes = [
        make_outcome(is_success=False, forced_return_reason="geofence"),
        make_outcome(repeat=2, is_success=False, forced_return_reason="battery"),
        make_outcome("two_red", "rule"),
    ]

    md_path, csv_path = write_summary(tmp_path, outcomes)
    md_text = md_path.read_text(encoding="utf-8")
    rows = read_csv_rows(csv_path)

    assert "2 (battery:1, geofence:1)" in md_text
    assert rows[0]["forced_returns"] == "2"
    assert rows[0]["forced_return_reasons"] == "battery:1;geofence:1"
    assert rows[1]["forced_returns"] == "0"


def test_write_summary_d1_fallback_column(tmp_path):
    outcomes = [
        make_outcome("basic", "rule", selections=4),
        make_outcome("basic", "d1", selections=4, fallbacks=2, fallback_reasons=("timeout", "timeout")),
        make_outcome("basic", "d1", repeat=2, selections=4),
        make_outcome("two_red", "d1", selections=0),
    ]

    md_path, csv_path = write_summary(tmp_path, outcomes)
    lines = md_path.read_text(encoding="utf-8").splitlines()
    rows = read_csv_rows(csv_path)

    rule_line = next(line for line in lines if line.startswith("| basic | rule"))
    d1_line = next(line for line in lines if line.startswith("| basic | d1"))
    empty_d1_line = next(line for line in lines if line.startswith("| two_red | d1"))
    d1_total_line = next(line for line in lines if "**ALL**" in line and "**d1**" in line)

    assert md_cells(rule_line)[9] == "-"
    assert md_cells(d1_line)[9] == "2/8 (25%) timeout:2"
    assert md_cells(empty_d1_line)[9] == "0/0"
    assert md_cells(d1_total_line)[9] == "2/8 (25%) timeout:2"

    d1_row = next(row for row in rows if row["scenario"] == "basic" and row["selector"] == "d1")
    rule_row = next(row for row in rows if row["scenario"] == "basic" and row["selector"] == "rule")
    assert (d1_row["selections"], d1_row["fallbacks"], d1_row["fallback_rate"]) == ("8", "2", "0.250")
    assert d1_row["fallback_reasons"] == "timeout:2"
    assert rule_row["fallback_rate"] == ""


def test_write_summary_fallback_reasons_top_three(tmp_path):
    reasons = ("timeout", "timeout", "timeout", "http_error", "http_error", "bad_json", "invalid")
    outcomes = [make_outcome("basic", "d1", selections=10, fallbacks=7, fallback_reasons=reasons)]

    md_path, _ = write_summary(tmp_path, outcomes)

    assert "7/10 (70%) timeout:3, http_error:2, bad_json:1" in md_path.read_text(encoding="utf-8")


def test_write_summary_md_header_and_bold_totals(tmp_path):
    md_path, _ = write_summary(tmp_path, [make_outcome()])
    md_text = md_path.read_text(encoding="utf-8")

    assert md_text.startswith("# ")
    assert (
        "| 시나리오 | 선택기 | 실행 | 성공률 | 평균 소요 시간 s | 맞힌 빨강 | 오발사 | 빗나감 | 비상 복귀(사유) | d1 fallback(사유) | 중단 |"
        in md_text
    )
    assert "| **ALL** | **rule** |" in md_text
    assert "중단 실행" not in md_text


def test_write_summary_csv_header(tmp_path):
    _, csv_path = write_summary(tmp_path, [make_outcome()])

    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        header = next(csv.reader(csv_file))

    assert header == [
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
    ]


# ---------------------------------------------------------------- write_server_missing


def test_write_server_missing_d1(tmp_path):
    missing = [("d1", "http://orin:8090", "connection refused")]

    md_path, csv_path = write_server_missing(tmp_path / "out", missing, "2026-10-11T10:00:00")
    md_text = md_path.read_text(encoding="utf-8")
    rows = read_csv_rows(csv_path)

    assert md_path == tmp_path / "out" / "summary.md"
    assert "d1 서버 없음 — 비교 중단" in md_text
    assert "http://orin:8090" in md_text
    assert "connection refused" in md_text
    assert "2026-10-11T10:00:00" in md_text
    assert rows == [
        {
            "status": "server_missing",
            "server": "d1",
            "url": "http://orin:8090",
            "error": "connection refused",
            "checked_at": "2026-10-11T10:00:00",
        }
    ]


def test_write_server_missing_yolo_and_both(tmp_path):
    missing = [("d1", "http://orin:8090", "timeout"), ("yolo", "http://orin:8082", "HTTP 503")]

    md_path, csv_path = write_server_missing(tmp_path, missing, "2026-10-11T10:00:00")
    md_text = md_path.read_text(encoding="utf-8")
    rows = read_csv_rows(csv_path)

    assert "YOLO 서버 없음 — 비교 중단" in md_text
    assert "d1 서버 없음 — 비교 중단" in md_text
    assert [row["server"] for row in rows] == ["d1", "yolo"]
    assert all(row["status"] == "server_missing" for row in rows)


# ---------------------------------------------------------------- md 이스케이프


def test_md_cells_escape_pipe_and_newline(tmp_path):
    outcomes = [
        make_outcome(is_success=False, forced_return_reason="a|b"),
        aborted_run("basic", "rule", 2, "line one\nline | two"),
    ]

    md_path, _ = write_summary(tmp_path, outcomes)
    md_text = md_path.read_text(encoding="utf-8")

    assert "a\\|b:1" in md_text
    assert "line one line \\| two" in md_text

    server_md, _ = write_server_missing(tmp_path / "srv", [("d1", "http://x", "bad | err\nmore")], "t")
    assert "bad \\| err more" in server_md.read_text(encoding="utf-8")
