"""요청 상태 v2 테스트 (D-01, D-08, D-09).

rule_mode 메뉴 상수, SelectionState + SelectionEvent → v2 dict 변환, 외부 dict → 엄격한 검증(ValueError) 을 본다.
"""

import copy
import json
import math

import pytest

from modules.decision.interface.menu import RULE_MODE_DEFINITIONS, RULE_MODES, TARGET_MODES
from modules.decision.interface.state import (
    DRONE_FLIGHT_MODES,
    EVENT_KINDS,
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    FORBIDDEN_KEY_PREFIX,
    MODE_OUTCOMES,
    SAFE_TOKEN_PATTERN,
    SELECTION_STATE_SCHEMA_VERSION,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
    build_request_state,
)
from modules.flight_control.interface import FLIGHT_MODES

REQUEST_TOP_LEVEL_KEYS = ["schema_version", "time_sec", "event", "drone", "tracks", "mission"]


def _make_track(label: str = "T3", **overrides) -> TrackState:
    fields = {
        "track_id": label,
        "class_name": "person",
        "north_m": 12.0,
        "east_m": -4.0,
        "first_seen_sec": 1.0,
        "last_seen_sec": 9.5,
        "observation_count": 12,
        "mean_confidence": 0.82,
        "is_active": True,
        "upper_color": "red",
    }
    fields.update(overrides)
    return TrackState(**fields)


def _make_drone(**overrides) -> DroneState:
    fields = {
        "north_m": 0.0,
        "east_m": 0.0,
        "down_m": -20.0,
        "altitude_m": 20.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "yaw_deg": 90.0,
        "gimbal_pitch_deg": -45.0,
        "gimbal_yaw_deg": 0.0,
        "battery_pct": 87.5,
        "flight_mode": "hover",
    }
    fields.update(overrides)
    return DroneState(**fields)


def _make_state(tracks=None, time_sec: float = 12.3, drone=None, mission=None) -> SelectionState:
    return SelectionState(
        time_sec=time_sec,
        drone=drone if drone is not None else _make_drone(),
        tracks=tuple(tracks) if tracks is not None else (_make_track(),),
        mission=mission if mission is not None else MissionSummary("person", "red", ("T1",)),
    )


def _periodic_event() -> SelectionEvent:
    return SelectionEvent(EVENT_PERIODIC_CHECK, None)


def _walk(value, visit_key, visit_str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            visit_key(key)
            _walk(item, visit_key, visit_str)
    elif isinstance(value, list):
        for item in value:
            _walk(item, visit_key, visit_str)
    elif isinstance(value, str):
        visit_str(value)


# --- 메뉴 (D-01, D-09) ---


def test_rule_modes_are_fixed_six_in_order() -> None:
    assert RULE_MODES == ("search", "approach", "aim_and_fire", "observe", "hold", "return_home")


def test_rule_mode_definitions_cover_menu_with_one_ascii_line_each() -> None:
    assert set(RULE_MODE_DEFINITIONS) == set(RULE_MODES)
    assert tuple(RULE_MODE_DEFINITIONS) == RULE_MODES

    for definition in RULE_MODE_DEFINITIONS.values():
        assert definition.strip()
        assert "\n" not in definition
        assert definition.isascii()


def test_target_modes_are_subset_of_menu() -> None:
    assert TARGET_MODES == ("approach", "aim_and_fire", "observe")
    assert set(TARGET_MODES) <= set(RULE_MODES)


def test_event_and_outcome_constants() -> None:
    assert EVENT_KINDS == (EVENT_MODE_ENDED, EVENT_NEW_TARGET, EVENT_PERIODIC_CHECK)
    assert EVENT_KINDS == ("mode_ended", "new_target", "periodic_check")
    assert MODE_OUTCOMES == ("success", "failure", "timeout")


def test_drone_flight_modes_match_flight_control() -> None:
    assert DRONE_FLIGHT_MODES == FLIGHT_MODES


# --- build_request_state (D-08) ---


def test_build_request_state_has_fixed_top_level_keys() -> None:
    request = build_request_state(_make_state(), _periodic_event())

    assert list(request) == REQUEST_TOP_LEVEL_KEYS
    assert request["schema_version"] == SELECTION_STATE_SCHEMA_VERSION == 2
    assert request["event"] == {"kind": "periodic_check", "last_mode": None}


def test_build_request_state_matches_v2_shape() -> None:
    request = build_request_state(_make_state(), _periodic_event())

    assert list(request["drone"]) == [
        "position_ned_m", "altitude_m", "attitude_deg", "gimbal_deg", "battery_pct", "flight_mode",
    ]
    assert request["drone"]["position_ned_m"] == {"north": 0.0, "east": 0.0, "down": -20.0}
    assert request["drone"]["attitude_deg"] == {"roll": 0.0, "pitch": 0.0, "yaw": 90.0}
    assert request["drone"]["gimbal_deg"] == {"pitch": -45.0, "yaw": 0.0}
    assert list(request["tracks"][0]) == [
        "track_id", "class", "position_ned_m", "first_seen_sec", "last_seen_sec",
        "observation_count", "mean_confidence", "is_active", "upper_color",
    ]
    assert request["mission"] == {"class": "person", "upper_color": "red", "hearted_tracks": ["T1"]}


def test_build_request_state_mode_ended_carries_last_mode() -> None:
    event = SelectionEvent(EVENT_MODE_ENDED, ModeResult("approach", "success", "T3"))

    request = build_request_state(_make_state(), event)

    assert request["event"] == {
        "kind": "mode_ended",
        "last_mode": {"rule_mode": "approach", "outcome": "success", "track": "T3"},
    }


def test_build_request_state_mode_ended_without_track() -> None:
    event = SelectionEvent(EVENT_MODE_ENDED, ModeResult("search", "timeout", None))

    request = build_request_state(_make_state(), event)

    assert request["event"]["last_mode"] == {"rule_mode": "search", "outcome": "timeout", "track": None}


def test_build_request_state_rounds_and_removes_negative_zero() -> None:
    track = _make_track(north_m=1.26, east_m=-0.04, mean_confidence=0.876)
    state = _make_state(tracks=[track], time_sec=12.34, drone=_make_drone(north_m=-0.04, battery_pct=87.46))

    request = build_request_state(state, _periodic_event())

    assert request["time_sec"] == 12.3
    assert request["tracks"][0]["position_ned_m"] == {"north": 1.3, "east": 0.0}
    assert request["tracks"][0]["mean_confidence"] == 0.88
    assert request["drone"]["position_ned_m"]["north"] == 0.0
    assert request["drone"]["battery_pct"] == 87.5
    assert "-0.0" not in json.dumps(request)


def test_build_request_state_sorts_tracks_and_hearted_by_label_number() -> None:
    mission = MissionSummary("person", None, ("T10", "T2"))
    state = _make_state(tracks=[_make_track("T10"), _make_track("T2")], mission=mission)

    request = build_request_state(state, _periodic_event())

    assert [track["track_id"] for track in request["tracks"]] == ["T2", "T10"]
    assert request["mission"]["hearted_tracks"] == ["T2", "T10"]


def test_build_request_state_has_no_target_keys_and_only_safe_tokens() -> None:
    event = SelectionEvent(EVENT_MODE_ENDED, ModeResult("approach", "failure", "T3"))
    request = build_request_state(_make_state(), event)
    keys: list[str] = []
    strings: list[str] = []

    _walk(request, keys.append, strings.append)

    assert keys
    assert not [key for key in keys if key.startswith(FORBIDDEN_KEY_PREFIX)]
    assert strings
    assert all(SAFE_TOKEN_PATTERN.match(value) for value in strings)


def test_build_request_state_is_deterministic() -> None:
    first = build_request_state(_make_state(), _periodic_event())
    second = build_request_state(_make_state(), _periodic_event())

    assert json.dumps(first, separators=(",", ":")) == json.dumps(second, separators=(",", ":"))


def test_build_request_state_allows_null_upper_color() -> None:
    state = _make_state(tracks=[_make_track(upper_color=None)], mission=MissionSummary("person", None, ()))

    request = build_request_state(state, _periodic_event())

    assert request["tracks"][0]["upper_color"] is None
    assert request["mission"] == {"class": "person", "upper_color": None, "hearted_tracks": []}


# --- SelectionState.to_dict / from_dict ---


def test_selection_state_round_trips() -> None:
    state = _make_state(tracks=[_make_track("T10"), _make_track("T2", upper_color=None)])

    data = state.to_dict()

    assert list(data) == ["schema_version", "time_sec", "drone", "tracks", "mission"]
    assert SelectionState.from_dict(data) == state
    assert SelectionState.from_dict(json.loads(json.dumps(data))) == state


def _break_missing_key(data: dict) -> None:
    del data["mission"]


def _break_unknown_key(data: dict) -> None:
    data["extra"] = 1


def _break_schema_version_1(data: dict) -> None:
    data["schema_version"] = 1


def _break_schema_version_bool(data: dict) -> None:
    data["schema_version"] = True


def _break_track_label(data: dict) -> None:
    data["tracks"][0]["track_id"] = "3"


def _break_class(data: dict) -> None:
    data["tracks"][0]["class"] = "Person!"


def _break_upper_color(data: dict) -> None:
    data["tracks"][0]["upper_color"] = "red shirt"


def _break_duplicate_track(data: dict) -> None:
    data["tracks"].append(copy.deepcopy(data["tracks"][0]))


def _break_flight_mode(data: dict) -> None:
    data["drone"]["flight_mode"] = "flying"


def _break_hearted_tracks(data: dict) -> None:
    data["mission"]["hearted_tracks"] = ["X1"]


def _break_observation_count(data: dict) -> None:
    data["tracks"][0]["observation_count"] = 0


def _break_mean_confidence(data: dict) -> None:
    data["tracks"][0]["mean_confidence"] = 1.5


def _break_seen_order(data: dict) -> None:
    data["tracks"][0]["first_seen_sec"] = 99.0


def _break_nan_number(data: dict) -> None:
    data["drone"]["altitude_m"] = math.nan


def _break_tracks_not_list(data: dict) -> None:
    data["tracks"] = {}


def _break_mission_class(data: dict) -> None:
    data["mission"]["class"] = "a person"


def _break_track_unknown_key(data: dict) -> None:
    data["tracks"][0]["target_id"] = "T3"


@pytest.mark.parametrize(
    "break_data",
    [
        _break_missing_key,
        _break_unknown_key,
        _break_schema_version_1,
        _break_schema_version_bool,
        _break_track_label,
        _break_class,
        _break_upper_color,
        _break_duplicate_track,
        _break_flight_mode,
        _break_hearted_tracks,
        _break_observation_count,
        _break_mean_confidence,
        _break_seen_order,
        _break_nan_number,
        _break_tracks_not_list,
        _break_mission_class,
        _break_track_unknown_key,
    ],
)
def test_selection_state_from_dict_rejects_invalid(break_data) -> None:
    data = copy.deepcopy(_make_state().to_dict())
    break_data(data)

    with pytest.raises(ValueError):
        SelectionState.from_dict(data)


# --- 생성자 검증 ---


@pytest.mark.parametrize(
    "make_event",
    [
        lambda: SelectionEvent("mode_ended", None),
        lambda: SelectionEvent("periodic_check", ModeResult("search", "success", None)),
        lambda: SelectionEvent("new_target", ModeResult("search", "success", None)),
        lambda: SelectionEvent("unknown", None),
        lambda: ModeResult("fly", "success", None),
        lambda: ModeResult("search", "crashed", None),
        lambda: ModeResult("approach", "success", "3"),
    ],
)
def test_event_and_mode_result_constructors_reject_invalid(make_event) -> None:
    with pytest.raises(ValueError):
        make_event()


def test_selection_state_constructor_rejects_duplicate_tracks() -> None:
    with pytest.raises(ValueError):
        _make_state(tracks=[_make_track("T1"), _make_track("T1")])


def test_mission_summary_rejects_duplicate_hearted_tracks() -> None:
    with pytest.raises(ValueError):
        MissionSummary("person", None, ("T1", "T1"))


def test_mode_result_round_trips() -> None:
    result = ModeResult("observe", "failure", "T7")

    data = result.to_dict()

    assert data == {"rule_mode": "observe", "outcome": "failure", "track": "T7"}
    assert ModeResult.from_dict(data) == result


def test_mode_result_from_dict_rejects_unknown_key() -> None:
    with pytest.raises(ValueError):
        ModeResult.from_dict({"rule_mode": "hold", "outcome": "success", "track": None, "x": 1})


def test_selection_event_to_dict() -> None:
    assert _periodic_event().to_dict() == {"kind": "periodic_check", "last_mode": None}
