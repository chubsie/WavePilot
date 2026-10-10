"""01-REVIEW CR-01·WR-01·WR-04·IN-01 회귀 테스트 — 규칙 사다리·보기 필터·대상 선택·라벨 형식."""

import pytest

from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_PERIODIC_CHECK,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.options import AIM_READY_MARGIN_M, filter_options, get_target_tracks
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.rule_selector import RuleSelector

STOP_DISTANCE_M = ParamDefaults().approach_stop_distance_m
AIM_READY_DISTANCE_M = STOP_DISTANCE_M + AIM_READY_MARGIN_M


# --- 상태 빌더 ---


def _make_drone() -> DroneState:
    return DroneState(
        north_m=0.0, east_m=0.0, down_m=-10.0, altitude_m=10.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0,
        battery_pct=90.0, flight_mode="hover",
    )


def _make_track(track_id: str, north_m: float, upper_color: str | None = None) -> TrackState:
    return TrackState(
        track_id=track_id, class_name="person", north_m=north_m, east_m=0.0,
        first_seen_sec=1.0, last_seen_sec=2.0, observation_count=3, mean_confidence=0.8,
        is_active=True, upper_color=upper_color,
    )


def _make_state(tracks: tuple[TrackState, ...], target_upper_color: str | None = None) -> SelectionState:
    mission = MissionSummary(target_class="person", target_upper_color=target_upper_color, hearted_tracks=())
    return SelectionState(time_sec=5.0, drone=_make_drone(), tracks=tracks, mission=mission)


def _make_mode_ended(rule_mode: str, outcome: str, target_track: str | None) -> SelectionEvent:
    return SelectionEvent(kind=EVENT_MODE_ENDED, mode_result=ModeResult(rule_mode, outcome, target_track))


PERIODIC_CHECK = SelectionEvent(kind=EVENT_PERIODIC_CHECK, mode_result=None)


def _select(state: SelectionState, event: SelectionEvent) -> tuple[str, object]:
    result = RuleSelector(ParamDefaults()).select(state, event)
    return result.rule_mode, result.params.get("target_track")


def _get_target_labels(rule_mode: str, state: SelectionState, event: SelectionEvent) -> list[str]:
    return [track.track_id for track in get_target_tracks(rule_mode, state, event)]


# --- CR-01: 사다리 2줄 approach 의 대상 = 가장 가까운 확정 track ---


def _make_cr01_state() -> SelectionState:
    # 임무 person/red, T1 은 빨강 50 m, T2 는 색 미상 10 m
    tracks = (_make_track("T1", 50.0, upper_color="red"), _make_track("T2", 10.0))
    return _make_state(tracks, target_upper_color="red")


def test_nearer_unknown_candidate_is_observed_not_approached():
    assert _select(_make_cr01_state(), PERIODIC_CHECK) == ("observe", "T2")


def test_observe_success_does_not_loop_into_approaching_unknown_candidate():
    event = _make_mode_ended("observe", "success", "T2")

    assert _select(_make_cr01_state(), event) != ("approach", "T2")


def test_far_confirmed_track_is_approached_when_it_is_nearest_candidate():
    tracks = (_make_track("T1", 30.0, upper_color="red"), _make_track("T2", 60.0))

    assert _select(_make_state(tracks, target_upper_color="red"), PERIODIC_CHECK) == ("approach", "T1")


# --- WR-04 (a): aim_and_fire 거리 제한 ---


def test_aim_and_fire_removed_when_nearest_confirmed_is_beyond_aim_ready_distance():
    state = _make_state((_make_track("T1", AIM_READY_DISTANCE_M + 0.1),))

    assert "aim_and_fire" not in filter_options(state, PERIODIC_CHECK, STOP_DISTANCE_M)


def test_aim_and_fire_kept_at_exact_aim_ready_distance():
    state = _make_state((_make_track("T1", AIM_READY_DISTANCE_M),))

    assert "aim_and_fire" in filter_options(state, PERIODIC_CHECK, STOP_DISTANCE_M)


def test_aim_ready_distance_follows_given_stop_distance():
    state = _make_state((_make_track("T1", 40.0),))

    assert "aim_and_fire" in filter_options(state, PERIODIC_CHECK, 40.0 - AIM_READY_MARGIN_M)
    assert "aim_and_fire" not in filter_options(state, PERIODIC_CHECK, 30.0)


def test_filter_options_requires_stop_distance():
    with pytest.raises(TypeError):
        filter_options(_make_state(()), PERIODIC_CHECK)


# --- WR-04 (b) + WR-01: 방금 실패한 (mode, track) 제외 ---


@pytest.mark.parametrize("outcome", ["failure", "timeout"])
@pytest.mark.parametrize("rule_mode", ["approach", "aim_and_fire", "observe"])
def test_just_failed_mode_and_track_is_excluded_from_that_mode_only(rule_mode, outcome):
    state = _make_state((_make_track("T1", 3.0), _make_track("T2", 6.0)))
    event = _make_mode_ended(rule_mode, outcome, "T1")

    for target_mode in ("approach", "aim_and_fire", "observe"):
        expected = ["T2"] if target_mode == rule_mode else ["T1", "T2"]
        assert _get_target_labels(target_mode, state, event) == expected


def test_success_does_not_exclude_target():
    state = _make_state((_make_track("T1", 3.0),))
    event = _make_mode_ended("approach", "success", "T1")

    assert _get_target_labels("approach", state, event) == ["T1"]


def test_mode_without_remaining_target_drops_from_options():
    state = _make_state((_make_track("T3", 6.0),))
    event = _make_mode_ended("aim_and_fire", "failure", "T3")

    options = filter_options(state, event, STOP_DISTANCE_M)

    assert "aim_and_fire" not in options
    assert "approach" in options and "observe" in options


def test_aim_distance_uses_remaining_target_not_failed_one():
    # 실패한 T3 는 가깝지만 빠지고, 남은 T4 는 멀어 조준 보기에 남지 않는다
    state = _make_state((_make_track("T3", 6.0), _make_track("T4", 50.0)))
    event = _make_mode_ended("aim_and_fire", "failure", "T3")

    assert "aim_and_fire" not in filter_options(state, event, STOP_DISTANCE_M)


def test_fill_params_skips_just_failed_target():
    state = _make_state((_make_track("T1", 3.0), _make_track("T2", 6.0)))
    event = _make_mode_ended("approach", "timeout", "T1")

    assert fill_params("approach", state, event, ParamDefaults())["target_track"] == "T2"


def test_fill_params_rejects_mode_whose_only_target_just_failed():
    state = _make_state((_make_track("T1", 3.0),))
    event = _make_mode_ended("observe", "failure", "T1")

    with pytest.raises(ValueError, match="needs a target track"):
        fill_params("observe", state, event, ParamDefaults())


def test_rule_failure_moves_down_ladder_instead_of_holding():
    # 조준 실패 뒤 hold 가 아니라 다음 칸(observe)으로 내려간다
    state = _make_state((_make_track("T3", 6.0),))
    event = _make_mode_ended("aim_and_fire", "failure", "T3")

    assert _select(state, event) == ("observe", "T3")


def test_rule_failure_moves_to_next_candidate():
    state = _make_state((_make_track("T1", 30.0), _make_track("T2", 40.0)))
    event = _make_mode_ended("approach", "failure", "T1")

    assert _select(state, event) == ("approach", "T2")


def test_rule_and_filter_agree_on_options():
    state = _make_state((_make_track("T1", 30.0),))
    event = _make_mode_ended("approach", "failure", "T1")

    result = RuleSelector(ParamDefaults()).select(state, event)

    assert result.options == filter_options(state, event, STOP_DISTANCE_M)
    assert result.rule_mode in result.options


# --- IN-01: 라벨에 앞자리 0 금지 ---


@pytest.mark.parametrize("label", ["T01", "T00", "T007"])
def test_track_label_with_leading_zero_is_rejected(label):
    with pytest.raises(ValueError, match="track_id"):
        _make_track(label, 10.0)


@pytest.mark.parametrize("label", ["T0", "T1", "T10", "T105"])
def test_canonical_track_label_is_accepted(label):
    assert _make_track(label, 10.0).track_id == label


def test_mode_result_rejects_leading_zero_target_label():
    with pytest.raises(ValueError, match="last_mode.track"):
        ModeResult("approach", "failure", "T01")
