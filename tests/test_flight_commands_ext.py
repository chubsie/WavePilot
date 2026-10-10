"""명령 4종 추가(속도·yaw·짐벌·하트) + 시뮬레이션 GPS 텔레메트리 + mock 확장 (D-06..D-08, D-13)."""

import math

import pytest

from modules.flight_control.interface import (
    HOME_GPS_FIX,
    HOME_LATITUDE_DEG,
    HOME_LONGITUDE_DEG,
    FireHeart,
    GoTo,
    GpsFix,
    Land,
    ReturnToHome,
    SetGimbal,
    SetVelocity,
    SetYaw,
    TakeOff,
    Telemetry,
    command_from_dict,
    ned_to_gps,
)
from modules.flight_control.mock import MockFlightControl
from modules.simulation.clock import SimClock

# --- 새 명령 4종 ---


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        (SetVelocity(1.0, -2.0, 0.5, 1.5), "set_velocity"),
        (SetYaw(90.0), "set_yaw"),
        (SetGimbal(-20.0, 10.0), "set_gimbal"),
        (FireHeart(7), "fire_heart"),
    ],
)
def test_new_commands_round_trip_through_dict(command, kind: str) -> None:
    data = command.to_dict()

    assert data["kind"] == kind
    assert command_from_dict(data) == command


def test_set_velocity_dict_uses_field_names() -> None:
    assert SetVelocity(1.0, -2.0, 0.5, 1.5).to_dict() == {
        "kind": "set_velocity",
        "north_mps": 1.0,
        "east_mps": -2.0,
        "down_mps": 0.5,
        "duration_sec": 1.5,
    }


@pytest.mark.parametrize("duration_sec", [0, -1.0])
def test_set_velocity_rejects_non_positive_duration_from_dict(duration_sec: float) -> None:
    data = {"kind": "set_velocity", "north_mps": 1.0, "east_mps": 0.0, "down_mps": 0.0, "duration_sec": duration_sec}

    with pytest.raises(ValueError, match="duration_sec"):
        command_from_dict(data)


def test_set_velocity_rejects_zero_duration_on_construction() -> None:
    with pytest.raises(ValueError, match="duration_sec"):
        SetVelocity(1.0, 0.0, 0.0, duration_sec=0.0)


@pytest.mark.parametrize("track_id", [True, 1.5, "3"])
def test_fire_heart_rejects_non_int_track_id(track_id: object) -> None:
    with pytest.raises(ValueError, match="track_id"):
        command_from_dict({"kind": "fire_heart", "track_id": track_id})


def test_set_gimbal_requires_yaw() -> None:
    with pytest.raises(ValueError, match="yaw_deg"):
        command_from_dict({"kind": "set_gimbal", "pitch_deg": -20})


def test_unknown_kind_still_rejected() -> None:
    with pytest.raises(ValueError):
        command_from_dict({"kind": "fly"})


# --- 텔레메트리 GPS ---


def test_telemetry_without_gps_serializes_null(hover_telemetry: Telemetry) -> None:
    assert hover_telemetry.gps is None
    assert hover_telemetry.to_dict()["gps"] is None


def test_telemetry_with_gps_round_trips(hover_telemetry: Telemetry) -> None:
    gps = GpsFix(37.5, 127.0, 40.0)
    telemetry = Telemetry(**{**_telemetry_fields(hover_telemetry), "gps": gps})

    data = telemetry.to_dict()

    assert data["gps"] == {"latitude_deg": 37.5, "longitude_deg": 127.0, "altitude_m": 40.0}
    assert Telemetry.from_dict(data) == telemetry


def test_telemetry_from_dict_accepts_missing_or_null_gps(hover_telemetry: Telemetry) -> None:
    data = hover_telemetry.to_dict()
    missing = {key: value for key, value in data.items() if key != "gps"}

    assert Telemetry.from_dict(missing) == hover_telemetry
    assert Telemetry.from_dict({**data, "gps": None}) == hover_telemetry


def test_telemetry_from_dict_rejects_non_dict_gps(hover_telemetry: Telemetry) -> None:
    with pytest.raises(ValueError, match="gps"):
        Telemetry.from_dict({**hover_telemetry.to_dict(), "gps": [37.5, 127.0, 40.0]})


def test_telemetry_from_dict_rejects_incomplete_gps(hover_telemetry: Telemetry) -> None:
    with pytest.raises(ValueError, match="altitude_m"):
        Telemetry.from_dict({**hover_telemetry.to_dict(), "gps": {"latitude_deg": 37.5, "longitude_deg": 127.0}})


# --- ned_to_gps ---


def test_ned_origin_is_home() -> None:
    assert ned_to_gps(0, 0, 0) == GpsFix(37.5665, 126.9780, 0.0)
    assert ned_to_gps(0, 0, 0) == HOME_GPS_FIX
    assert (HOME_LATITUDE_DEG, HOME_LONGITUDE_DEG) == (37.5665, 126.9780)


def test_ned_north_adds_latitude() -> None:
    assert ned_to_gps(111.32, 0, 0).latitude_deg == pytest.approx(37.5675)


def test_ned_up_adds_altitude() -> None:
    assert ned_to_gps(0, 0, -10).altitude_m == 10.0


def test_ned_east_is_scaled_by_cos_latitude() -> None:
    expected_deg = 100.0 / (111_320.0 * math.cos(math.radians(37.5665)))

    gps = ned_to_gps(0, 100.0, 0)

    assert gps.longitude_deg - 126.9780 == pytest.approx(expected_deg)
    assert gps.latitude_deg == 37.5665


# --- MockFlightControl ---


def test_mock_accepts_all_eight_kinds_and_keeps_mode_for_attitude_commands() -> None:
    flight_control = MockFlightControl(SimClock())
    commands = [
        TakeOff(40.0),
        GoTo(1.0, 2.0, 40.0),
        Land(),
        ReturnToHome(),
        SetVelocity(1.0, 0.0, 0.0, 2.0),
        SetYaw(90.0),
        SetGimbal(-30.0, 0.0),
        FireHeart(3),
    ]

    acks = [flight_control.send_command(command) for command in commands]

    assert all(ack.accepted for ack in acks)
    assert [command for _, command in flight_control.commands_received] == commands
    assert flight_control.get_telemetry().flight_mode == "moving"


def test_mock_set_velocity_switches_to_moving() -> None:
    flight_control = MockFlightControl(SimClock())

    flight_control.send_command(SetVelocity(1.0, 0.0, 0.0, 1.0))

    assert flight_control.get_telemetry().flight_mode == "moving"


def test_mock_telemetry_carries_gps() -> None:
    flight_control = MockFlightControl(SimClock(), hover_north_m=50.0, hover_east_m=-20.0, hover_altitude_m=30.0)

    telemetry = flight_control.get_telemetry()

    assert telemetry.gps == ned_to_gps(50.0, -20.0, -30.0)
    assert telemetry.gps.altitude_m == 30.0


def _telemetry_fields(telemetry: Telemetry) -> dict:
    return {key: value for key, value in telemetry.to_dict().items() if key != "gps"}
