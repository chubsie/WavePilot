import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from modules.mission.mission_log import (
    DEFAULT_LOG_DIR,
    KIND_DETECTION,
    KIND_HEART,
    KIND_LINK,
    KIND_MISSION,
    KIND_MODE_TRANSITION,
    KIND_RETURN,
    KIND_SAFETY,
    KIND_SELECTION,
    LOG_KINDS,
    MissionLog,
    default_log_path,
)

TEST_LOGGER_NAME = "tests.mission_log"


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_appends_one_json_line_per_call(tmp_path):
    path = tmp_path / "a" / "b.jsonl"

    with MissionLog(path) as log:
        log.record("mission", 0.0, event="start")
        log.record("mode_transition", 1.5, **{"from": "MANUAL", "to": "AI", "reason": "operator"})
        log.record("heart", 12.3, target="T3", north=1.0, east=2.0)

    lines = _read_lines(path)
    assert len(lines) == 3
    assert lines[0] == {"t": 0.0, "kind": "mission", "event": "start"}
    assert lines[1] == {"t": 1.5, "kind": "mode_transition", "from": "MANUAL", "to": "AI", "reason": "operator"}
    assert list(lines[2].keys())[:2] == ["t", "kind"]


def test_record_is_flushed_before_close(tmp_path):
    path = tmp_path / "log.jsonl"
    log = MissionLog(path)

    log.record("safety", 3.0, reason="gps_lost")

    assert _read_lines(path) == [{"t": 3.0, "kind": "safety", "reason": "gps_lost"}]
    log.close()


def test_reopening_same_path_appends(tmp_path):
    path = tmp_path / "log.jsonl"

    with MissionLog(path) as log:
        log.record("mission", 0.0, event="start")
    with MissionLog(path) as log:
        log.record("mission", 9.0, event="end")

    assert [line["event"] for line in _read_lines(path)] == ["start", "end"]


def test_non_ascii_values_are_written_as_is(tmp_path):
    path = tmp_path / "log.jsonl"

    with MissionLog(path) as log:
        log.record("safety", 1.0, detail="배터리 부족")

    text = path.read_text(encoding="utf-8")
    assert "배터리 부족" in text
    assert "\\u" not in text


def test_unknown_kind_is_rejected_without_side_effects(tmp_path):
    path = tmp_path / "log.jsonl"

    with MissionLog(path) as log:
        log.record("link", 0.5, connected=True)

        with pytest.raises(ValueError, match="bogus"):
            log.record("bogus", 1.0, x=1)

        assert len(log.records) == 1

    assert len(_read_lines(path)) == 1


def test_record_returns_entry_and_records_accumulate_in_order():
    log = MissionLog()

    first = log.record("detection", 1.0, track_id=4)
    second = log.record("return", 2.0, reason="battery_low")

    assert first == {"t": 1.0, "kind": "detection", "track_id": 4}
    assert log.records == (first, second)
    assert isinstance(log.records, tuple)


def test_path_none_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = MissionLog(path=None)

    log.record("mission", 0.0, event="start")
    log.close()

    assert list(tmp_path.iterdir()) == []
    assert len(log.records) == 1


def test_console_line_uses_compact_json_for_containers(caplog):
    caplog.set_level(logging.INFO, logger=TEST_LOGGER_NAME)
    log = MissionLog(console=logging.getLogger(TEST_LOGGER_NAME))

    log.record("heart", 12.34, target="T3", params={"a": 1})

    assert caplog.messages == ['heart t=12.3 target=T3 params={"a":1}']
    assert caplog.records[0].name == TEST_LOGGER_NAME


def test_console_line_formats_lists_and_tuples_on_one_line(caplog):
    caplog.set_level(logging.INFO, logger=TEST_LOGGER_NAME)
    log = MissionLog(console=logging.getLogger(TEST_LOGGER_NAME))

    log.record("selection", 0.0, modes=["search", "track"], home=(1.0, 2.0))

    assert caplog.messages == ['selection t=0.0 modes=["search","track"] home=[1.0,2.0]']


def test_context_manager_closes_file(tmp_path):
    path = tmp_path / "log.jsonl"

    with MissionLog(path) as log:
        log.record("mission", 0.0, event="start")

    log.close()  # 두 번 닫아도 안전
    log.record("mission", 1.0, event="late")  # close 뒤 기록은 메모리에만

    assert len(_read_lines(path)) == 1
    assert len(log.records) == 2


def test_unserializable_value_leaves_no_partial_line(tmp_path):
    path = tmp_path / "log.jsonl"

    with MissionLog(path) as log:
        with pytest.raises(TypeError):
            log.record("mission", 0.0, bad=object())

    assert path.read_text(encoding="utf-8") == ""
    assert log.records == ()


def test_default_log_path_uses_start_time():
    path = default_log_path(Path("logs/missions"), datetime(2026, 10, 11, 9, 5, 7))

    assert path == Path("logs/missions/20261011-090507.jsonl")
    assert DEFAULT_LOG_DIR == Path("logs") / "missions"


def test_log_kinds_are_the_eight_kind_constants():
    kind_constants = (
        KIND_MISSION,
        KIND_MODE_TRANSITION,
        KIND_SELECTION,
        KIND_DETECTION,
        KIND_HEART,
        KIND_RETURN,
        KIND_SAFETY,
        KIND_LINK,
    )

    assert len(LOG_KINDS) == 8
    assert set(LOG_KINDS) == set(kind_constants)
