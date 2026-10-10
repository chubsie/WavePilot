"""보기 필터 테스트 (D-02 표 1). 표의 행마다 테스트 하나."""

from modules.decision.interface.menu import RULE_MODES
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.options import filter_options, get_nearest_track

MINIMUM_OPTIONS = ("search", "hold", "return_home")
ALL_OPTIONS = RULE_MODES
NO_AIM_OPTIONS = ("search", "approach", "observe", "hold", "return_home")


# --- 상태 빌더 ---


def _make_drone(north_m: float = 0.0, east_m: float = 0.0) -> DroneState:
    return DroneState(
        north_m=north_m, east_m=east_m, down_m=-10.0, altitude_m=10.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0,
        battery_pct=90.0, flight_mode="hover",
    )


def _make_track(
    track_id: str = "T1",
    class_name: str = "person",
    north_m: float = 10.0,
    east_m: float = 0.0,
    is_active: bool = True,
    upper_color: str | None = None,
) -> TrackState:
    return TrackState(
        track_id=track_id, class_name=class_name, north_m=north_m, east_m=east_m,
        first_seen_sec=1.0, last_seen_sec=2.0, observation_count=3, mean_confidence=0.8,
        is_active=is_active, upper_color=upper_color,
    )


def _make_state(
    tracks: tuple[TrackState, ...] = (),
    target_class: str = "person",
    target_upper_color: str | None = None,
    hearted: tuple[str, ...] = (),
) -> SelectionState:
    mission = MissionSummary(target_class=target_class, target_upper_color=target_upper_color, hearted_tracks=hearted)
    return SelectionState(time_sec=5.0, drone=_make_drone(), tracks=tracks, mission=mission)


def _make_event(kind: str = EVENT_PERIODIC_CHECK, mode_result: ModeResult | None = None) -> SelectionEvent:
    return SelectionEvent(kind=kind, mode_result=mode_result)


# --- 표 1 ---


def test_no_candidate_keeps_only_search_hold_return_home():
    assert filter_options(_make_state(), _make_event()) == MINIMUM_OPTIONS


def test_candidate_without_color_requirement_keeps_all_modes_in_menu_order():
    state = _make_state(tracks=(_make_track(),))

    assert filter_options(state, _make_event()) == ALL_OPTIONS


def test_color_required_but_unknown_removes_only_aim_and_fire():
    state = _make_state(tracks=(_make_track(upper_color=None),), target_upper_color="red")

    assert filter_options(state, _make_event()) == NO_AIM_OPTIONS


def test_color_confirmed_track_keeps_all_modes():
    state = _make_state(tracks=(_make_track(upper_color="red"),), target_upper_color="red")

    assert filter_options(state, _make_event()) == ALL_OPTIONS


def test_color_mismatch_track_is_not_candidate():
    state = _make_state(tracks=(_make_track(upper_color="blue"),), target_upper_color="red")

    assert filter_options(state, _make_event()) == MINIMUM_OPTIONS


def test_inactive_track_is_not_candidate():
    state = _make_state(tracks=(_make_track(is_active=False),))

    assert filter_options(state, _make_event()) == MINIMUM_OPTIONS


def test_other_class_track_is_not_candidate():
    state = _make_state(tracks=(_make_track(class_name="car"),), target_class="person")

    assert filter_options(state, _make_event()) == MINIMUM_OPTIONS


def test_hearted_track_is_not_candidate():
    state = _make_state(tracks=(_make_track(track_id="T1"),), hearted=("T1",))

    assert filter_options(state, _make_event()) == MINIMUM_OPTIONS


def test_just_hearted_track_is_not_candidate():
    state = _make_state(tracks=(_make_track(track_id="T3"),))
    event = _make_event(EVENT_MODE_ENDED, ModeResult("aim_and_fire", "success", "T3"))

    assert filter_options(state, event) == MINIMUM_OPTIONS


def test_failed_aim_target_stays_candidate():
    state = _make_state(tracks=(_make_track(track_id="T3"),))
    event = _make_event(EVENT_MODE_ENDED, ModeResult("aim_and_fire", "failure", "T3"))

    assert filter_options(state, event) == ALL_OPTIONS


def test_options_are_ordered_subset_with_safe_modes_across_states():
    track_sets = [
        (),
        (_make_track(),),
        (_make_track(upper_color="blue"),),
        (_make_track(track_id="T1", is_active=False), _make_track(track_id="T2", upper_color="red")),
        (_make_track(track_id="T4", class_name="car"),),
    ]
    events = [
        _make_event(EVENT_PERIODIC_CHECK),
        _make_event(EVENT_NEW_TARGET),
        _make_event(EVENT_MODE_ENDED, ModeResult("aim_and_fire", "success", "T2")),
    ]

    for tracks in track_sets:
        for target_upper_color in (None, "red"):
            for event in events:
                options = filter_options(_make_state(tracks=tracks, target_upper_color=target_upper_color), event)

                assert list(options) == [mode for mode in RULE_MODES if mode in options]
                assert set(MINIMUM_OPTIONS) <= set(options)


# --- 가장 가까운 track ---


def test_nearest_track_picks_smallest_distance():
    drone = _make_drone()
    far = _make_track(track_id="T1", north_m=30.0)
    near = _make_track(track_id="T4", north_m=3.0, east_m=4.0)

    assert get_nearest_track(drone, (far, near)) is near


def test_nearest_track_tie_breaks_by_label_number():
    drone = _make_drone()
    t2 = _make_track(track_id="T2", north_m=5.0)
    t10 = _make_track(track_id="T10", north_m=-5.0)

    assert get_nearest_track(drone, (t10, t2)) is t2


def test_nearest_track_of_empty_is_none():
    assert get_nearest_track(_make_drone(), ()) is None
