import pytest

from modules.decision.interface.state import EVENT_PERIODIC_CHECK, SelectionEvent, build_request_state
from modules.drone.selection_state import build_selection_state
from modules.flight_control.interface import Telemetry
from modules.perception.interface import Track


def _make_telemetry(**overrides) -> Telemetry:
    fields = dict(
        time_sec=12.0, north_m=12.345, east_m=-0.04, down_m=-15.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=90.0,
        gimbal_pitch_deg=-20.0, gimbal_yaw_deg=0.0, battery_pct=88.0, flight_mode="hover",
    )
    fields.update(overrides)

    return Telemetry(**fields)


def _make_track(track_id: int, class_name: str = "person", **overrides) -> Track:
    fields = dict(
        track_id=track_id, class_name=class_name, north_m=50.0, east_m=2.0, first_seen_sec=1.0, last_seen_sec=5.0,
        observation_count=4, mean_confidence=0.9, is_active=True,
    )
    fields.update(overrides)

    return Track(**fields)


def test_copies_drone_fields():
    state = build_selection_state(12.0, _make_telemetry(), (), "person")

    assert state.time_sec == 12.0
    assert state.drone.altitude_m == 15.0
    assert state.drone.down_m == -15.0
    assert state.drone.yaw_deg == 90.0
    assert state.drone.gimbal_pitch_deg == -20.0
    assert state.drone.battery_pct == 88.0
    assert state.drone.flight_mode == "hover"


def test_tracks_use_labels_sorted_numerically_and_keep_fields():
    tracks = (_make_track(12), _make_track(7, is_active=False))

    state = build_selection_state(12.0, _make_telemetry(), tracks, "person")

    assert [track.track_id for track in state.tracks] == ["T7", "T12"]
    assert [track.is_active for track in state.tracks] == [False, True]
    assert all(track.upper_color is None for track in state.tracks)


def test_class_names_become_safe_tokens_and_bad_ones_are_dropped():
    tracks = (
        _make_track(1, "traffic light"),
        _make_track(2, "Person"),
        _make_track(3, "3d"),
        _make_track(4, "fire-hydrant"),
    )

    state = build_selection_state(12.0, _make_telemetry(), tracks, "person")

    assert {track.track_id: track.class_name for track in state.tracks} == {
        "T1": "traffic_light",
        "T2": "person",
        "T4": "fire_hydrant",
    }


def test_mean_confidence_is_clamped_to_unit_range():
    state = build_selection_state(12.0, _make_telemetry(), (_make_track(1, mean_confidence=1.0000000002),), "person")

    assert state.tracks[0].mean_confidence == 1.0


def test_hearted_labels_are_sorted_and_color_is_none():
    state = build_selection_state(12.0, _make_telemetry(), (), "person", ["T12", "T3"])

    assert state.mission.target_class == "person"
    assert state.mission.target_upper_color is None
    assert state.mission.hearted_tracks == ("T3", "T12")


def test_request_state_rounds_but_state_keeps_raw_values():
    state = build_selection_state(12.0, _make_telemetry(), (), "person")

    request = build_request_state(state, SelectionEvent(EVENT_PERIODIC_CHECK, None))

    assert request["drone"]["position_ned_m"]["north"] == 12.3
    assert request["drone"]["position_ned_m"]["east"] == 0.0
    assert str(request["drone"]["position_ned_m"]["east"]) == "0.0"
    assert state.drone.north_m == 12.345


@pytest.mark.parametrize("height_above_ground_m", [None, 14.5])
def test_works_with_height_above_ground(height_above_ground_m):
    telemetry = _make_telemetry(height_above_ground_m=height_above_ground_m)

    state = build_selection_state(12.0, telemetry, (_make_track(1),), "person")

    assert state.drone.altitude_m == 15.0
