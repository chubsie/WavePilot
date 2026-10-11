"""공유 파라미터 채움 테스트 (D-03, D-04 표 3)."""

import dataclasses
import math

import pytest

from modules.decision.interface.menu import RULE_MODES
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
from modules.decision.params import PARAM_KEYS_BY_MODE, ParamDefaults, fill_params


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


# --- ParamDefaults ---


def test_defaults_match_table_and_are_frozen():
    defaults = ParamDefaults()

    assert dataclasses.asdict(defaults) == {
        "search_timeout_sec": 160.0,
        "search_area_north_min_m": 20.0,
        "search_area_north_max_m": 120.0,
        "search_area_east_min_m": -50.0,
        "search_area_east_max_m": 50.0,
        "search_altitude_m": 15.0,
        "search_lane_spacing_m": 25.0,
        "approach_stop_distance_m": 8.0,
        "approach_timeout_sec": 30.0,
        "aim_center_tolerance_ratio": 0.08,
        "aim_stable_frame_count": 2,
        "aim_timeout_sec": 15.0,
        "observe_timeout_sec": 10.0,
        "hold_duration_sec": 5.0,
        "home_north_m": 0.0,
        "home_east_m": 0.0,
        "return_altitude_m": 10.0,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.hold_duration_sec = 1.0


@pytest.mark.parametrize("value", [0, -1.0, math.nan, math.inf, True])
def test_non_positive_or_non_finite_duration_is_rejected(value):
    with pytest.raises(ValueError, match="hold_duration_sec"):
        ParamDefaults(hold_duration_sec=value)


def test_search_area_defaults_are_north_of_home():
    defaults = ParamDefaults()

    assert defaults.search_area_north_min_m == 20.0 > 0
    assert defaults.search_area_east_min_m == -50.0 < 0 < defaults.search_area_east_max_m == 50.0
    assert defaults.search_altitude_m == 15.0
    assert defaults.search_lane_spacing_m == 25.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"search_area_north_min_m": 120.0, "search_area_north_max_m": 120.0}, "search_area_north_min_m"),
        ({"search_area_east_min_m": 10.0, "search_area_east_max_m": -10.0}, "search_area_east_min_m"),
    ],
)
def test_search_area_min_not_below_max_is_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ParamDefaults(**kwargs)


@pytest.mark.parametrize(
    "name",
    ["search_area_north_min_m", "search_area_north_max_m", "search_area_east_min_m", "search_area_east_max_m"],
)
def test_bad_search_area_bound_is_rejected(name):
    with pytest.raises(ValueError, match=name):
        ParamDefaults(**{name: math.nan})


@pytest.mark.parametrize("name", ["search_altitude_m", "search_lane_spacing_m"])
@pytest.mark.parametrize("value", [0, -5.0, True])
def test_non_positive_search_altitude_or_spacing_is_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        ParamDefaults(**{name: value})


def test_search_params_unchanged_by_area_defaults():
    params = fill_params("search", _make_state(), _make_event(), ParamDefaults(search_lane_spacing_m=40.0))

    assert params == {"timeout_sec": 160.0}


@pytest.mark.parametrize("value", [0, True, 2.0])
def test_bad_stable_frame_count_is_rejected(value):
    with pytest.raises(ValueError, match="aim_stable_frame_count"):
        ParamDefaults(aim_stable_frame_count=value)


@pytest.mark.parametrize("value", [0.0, 0.6])
def test_center_tolerance_out_of_range_is_rejected(value):
    with pytest.raises(ValueError, match="aim_center_tolerance_ratio"):
        ParamDefaults(aim_center_tolerance_ratio=value)


def test_home_position_allows_negative_and_zero_but_must_be_finite():
    defaults = ParamDefaults(home_north_m=-12.5, home_east_m=0.0)

    assert defaults.home_north_m == -12.5
    with pytest.raises(ValueError, match="home_east_m"):
        ParamDefaults(home_east_m=math.nan)


# --- fill_params ---


def test_key_set_and_order_match_table_for_every_mode():
    state = _make_state(tracks=(_make_track(),))
    event = _make_event()

    assert PARAM_KEYS_BY_MODE.keys() == set(RULE_MODES)
    for rule_mode in RULE_MODES:
        params = fill_params(rule_mode, state, event, ParamDefaults())

        assert tuple(params) == PARAM_KEYS_BY_MODE[rule_mode]


@pytest.mark.parametrize("rule_mode", ["approach", "observe"])
def test_target_is_nearest_candidate(rule_mode):
    state = _make_state(tracks=(_make_track(track_id="T1", north_m=40.0), _make_track(track_id="T4", north_m=8.0)))

    params = fill_params(rule_mode, state, _make_event(), ParamDefaults())

    assert params["target_track"] == "T4"


def test_aim_target_is_color_confirmed_track_not_nearer_unknown_one():
    near_unknown = _make_track(track_id="T1", north_m=5.0, upper_color=None)
    far_confirmed = _make_track(track_id="T2", north_m=30.0, upper_color="red")
    state = _make_state(tracks=(near_unknown, far_confirmed), target_upper_color="red")

    params = fill_params("aim_and_fire", state, _make_event(), ParamDefaults())

    assert params["target_track"] == "T2"


def test_hearted_track_is_never_target():
    near_hearted = _make_track(track_id="T1", north_m=3.0)
    far_open = _make_track(track_id="T2", north_m=30.0)
    state = _make_state(tracks=(near_hearted, far_open), hearted=("T1",))

    for rule_mode in ("approach", "aim_and_fire", "observe"):
        assert fill_params(rule_mode, state, _make_event(), ParamDefaults())["target_track"] == "T2"


def test_just_hearted_track_is_never_target():
    state = _make_state(tracks=(_make_track(track_id="T1", north_m=3.0), _make_track(track_id="T2", north_m=30.0)))
    event = _make_event(EVENT_MODE_ENDED, ModeResult("aim_and_fire", "success", "T1"))

    assert fill_params("aim_and_fire", state, event, ParamDefaults())["target_track"] == "T2"


def test_values_are_str_int_float_or_none_not_bool():
    state = _make_state(tracks=(_make_track(),))

    for rule_mode in RULE_MODES:
        for value in fill_params(rule_mode, state, _make_event(), ParamDefaults()).values():
            assert not isinstance(value, bool)
            assert value is None or isinstance(value, (str, int, float))


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="rule_mode"):
        fill_params("dance", _make_state(), _make_event(), ParamDefaults())


@pytest.mark.parametrize("rule_mode", ["approach", "aim_and_fire", "observe"])
def test_target_mode_without_eligible_track_is_rejected(rule_mode):
    with pytest.raises(ValueError, match="needs a target track"):
        fill_params(rule_mode, _make_state(), _make_event(), ParamDefaults())


def test_fill_params_is_deterministic():
    state = _make_state(tracks=(_make_track(track_id="T2", north_m=5.0), _make_track(track_id="T7", north_m=-5.0)))

    for rule_mode in RULE_MODES:
        first = fill_params(rule_mode, state, _make_event(), ParamDefaults())
        second = fill_params(rule_mode, state, _make_event(), ParamDefaults())

        assert first == second
        assert list(first) == list(second)
