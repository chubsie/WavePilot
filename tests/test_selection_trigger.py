"""사건 판정기 테스트 (SEL-05 — 사건이 있을 때만 선택기를 부른다).

사건 3종(모드 종료·새 대상·주기 확인)의 판정, 우선순위, 주기 시계 재설정, 시각 역행 거부를 고정한다.
선택기·HTTP 는 쓰지 않는다.
"""

import math

import pytest

from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionState,
    TrackState,
)
from modules.decision.trigger import PERIODIC_CHECK_INTERVAL_SEC, SelectionTrigger


# --- 상태 빌더 ---


def _make_track(label: str, class_name: str, is_active: bool = True) -> TrackState:
    return TrackState(
        track_id=label, class_name=class_name, north_m=10.0, east_m=5.0,
        first_seen_sec=0.0, last_seen_sec=0.0, observation_count=3, mean_confidence=0.85,
        is_active=is_active, upper_color=None,
    )


def _make_state(time_sec: float, tracks: tuple[TrackState, ...] = (), target_class: str = "person") -> SelectionState:
    drone = DroneState(
        north_m=0.0, east_m=0.0, down_m=-40.0, altitude_m=40.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-55.0, gimbal_yaw_deg=0.0,
        battery_pct=98.0, flight_mode="hover",
    )
    mission = MissionSummary(target_class=target_class, target_upper_color=None, hearted_tracks=())
    return SelectionState(time_sec=time_sec, drone=drone, tracks=tracks, mission=mission)


def _make_started_trigger() -> SelectionTrigger:
    # 첫 poll 은 늘 주기 확인이라, 그 뒤 동작만 보려는 테스트는 t=0 을 먼저 소비한다
    trigger = SelectionTrigger()
    trigger.poll(0.0, _make_state(0.0), None)
    return trigger


def _poll(trigger: SelectionTrigger, time_sec: float, tracks=(), mode_result=None):
    return trigger.poll(time_sec, _make_state(time_sec, tracks), mode_result)


# --- 생성자 검증 ---


def test_default_interval_is_ten_seconds():
    assert PERIODIC_CHECK_INTERVAL_SEC == 10.0


@pytest.mark.parametrize("interval_sec", [0, -1.0, math.nan, math.inf])
def test_rejects_non_positive_or_non_finite_interval(interval_sec):
    with pytest.raises(ValueError):
        SelectionTrigger(interval_sec)


# --- 주기 확인 ---


def test_first_poll_is_periodic_check():
    # 아직 고른 모드가 없으므로 시작 자체를 첫 주기 확인으로 친다
    event = SelectionTrigger().poll(0.0, _make_state(0.0), None)

    assert event is not None
    assert event.kind == EVENT_PERIODIC_CHECK
    assert event.mode_result is None


def test_periodic_check_fires_every_interval():
    trigger = _make_started_trigger()

    quiet_times = [1.0, 2.0, 5.0, 9.0, 9.9]
    assert all(_poll(trigger, time_sec) is None for time_sec in quiet_times)
    assert _poll(trigger, 10.0).kind == EVENT_PERIODIC_CHECK
    assert _poll(trigger, 19.9) is None
    assert _poll(trigger, 20.0).kind == EVENT_PERIODIC_CHECK


# --- 모드 종료 ---


def test_mode_result_emits_mode_ended_and_resets_periodic_clock():
    trigger = _make_started_trigger()
    mode_result = ModeResult(rule_mode="search", outcome="timeout", target_track=None)

    event = _poll(trigger, 5.0, mode_result=mode_result)

    assert event.kind == EVENT_MODE_ENDED
    assert event.mode_result is mode_result
    # 모드 종료가 주기 시계를 다시 맞춘다
    assert _poll(trigger, 10.0) is None
    assert _poll(trigger, 15.0).kind == EVENT_PERIODIC_CHECK


# --- 새 대상 ---


def test_new_active_target_class_track_emits_new_target_once():
    trigger = _make_started_trigger()
    person = _make_track("T2", "person")

    event = _poll(trigger, 1.0, (person,))

    assert event.kind == EVENT_NEW_TARGET
    assert event.mode_result is None
    assert _poll(trigger, 2.0, (person,)) is None


def test_new_track_of_other_class_is_not_an_event():
    trigger = _make_started_trigger()

    assert _poll(trigger, 1.0, (_make_track("T3", "car"),)) is None


def test_inactive_track_counts_only_once_it_becomes_active():
    trigger = _make_started_trigger()

    assert _poll(trigger, 1.0, (_make_track("T4", "person", is_active=False),)) is None
    # 활성 track 만 "봤다"로 세므로 활성으로 바뀐 틱에 처음 보는 대상이 된다
    assert _poll(trigger, 2.0, (_make_track("T4", "person"),)).kind == EVENT_NEW_TARGET


def test_two_new_targets_in_one_tick_emit_single_event():
    trigger = _make_started_trigger()
    tracks = (_make_track("T5", "person"), _make_track("T6", "person"))

    assert _poll(trigger, 1.0, tracks).kind == EVENT_NEW_TARGET
    assert _poll(trigger, 2.0, tracks) is None


# --- 우선순위 ---


def test_mode_ended_wins_over_new_target_and_absorbs_it():
    trigger = _make_started_trigger()
    person = _make_track("T7", "person")
    mode_result = ModeResult(rule_mode="approach", outcome="success", target_track="T2")

    event = _poll(trigger, 3.0, (person,), mode_result)

    assert event.kind == EVENT_MODE_ENDED
    # 모드 종료 질문의 상태에 T7 이 이미 들어 있으므로 다음 틱에 따로 묻지 않는다
    assert _poll(trigger, 4.0, (person,)) is None


def test_new_target_wins_over_periodic_check_and_resets_clock():
    trigger = _make_started_trigger()
    person = _make_track("T8", "person")

    event = _poll(trigger, 10.0, (person,))

    assert event.kind == EVENT_NEW_TARGET
    assert _poll(trigger, 19.9, (person,)) is None
    assert _poll(trigger, 20.0, (person,)).kind == EVENT_PERIODIC_CHECK


def test_mode_ended_wins_over_periodic_check():
    trigger = _make_started_trigger()
    mode_result = ModeResult(rule_mode="search", outcome="failure", target_track=None)

    assert _poll(trigger, 10.0, mode_result=mode_result).kind == EVENT_MODE_ENDED


# --- 시각 검증 ---


def test_rejects_time_going_backwards():
    trigger = _make_started_trigger()
    _poll(trigger, 5.0)

    with pytest.raises(ValueError):
        _poll(trigger, 4.9)


def test_accepts_same_time_twice():
    trigger = _make_started_trigger()

    assert _poll(trigger, 0.0) is None


def test_rejects_non_finite_time():
    trigger = SelectionTrigger()

    with pytest.raises(ValueError):
        trigger.poll(math.nan, _make_state(0.0), None)
