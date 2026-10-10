import math

import pytest

from modules.flight_control.interface import (
    FireHeart,
    FlightControl,
    GoTo,
    Land,
    ReturnToHome,
    SetGimbal,
    TakeOff,
    ned_to_gps,
)
from modules.simulation.kinematic_flight import KinematicFlightControl

TICK_SEC = 0.1


def _advance(control: KinematicFlightControl, seconds: float) -> None:
    for _ in range(round(seconds / TICK_SEC)):
        control.advance(TICK_SEC)


def _advance_until(control: KinematicFlightControl, flight_mode: str, max_sec: float) -> float:
    start_sec = control.now_sec
    while control.get_telemetry().flight_mode != flight_mode:
        assert control.now_sec - start_sec < max_sec, f"never reached {flight_mode}"
        control.advance(TICK_SEC)

    return control.now_sec - start_sec


def test_satisfies_flight_control_protocol():
    assert isinstance(KinematicFlightControl(), FlightControl)


def test_initial_mode_depends_on_start_altitude():
    assert KinematicFlightControl().get_telemetry().flight_mode == "grounded"
    assert KinematicFlightControl(start_altitude_m=10.0).get_telemetry().flight_mode == "hover"


def test_take_off_climbs_then_hovers():
    control = KinematicFlightControl()

    control.send_command(TakeOff(15.0))

    assert control.get_telemetry().flight_mode == "moving"
    _advance_until(control, "hover", max_sec=10.0)
    assert abs(control.get_telemetry().altitude_m - 15.0) <= 0.5


def test_go_to_reaches_goal_and_hovers():
    control = KinematicFlightControl(start_altitude_m=15.0)

    control.send_command(GoTo(30.0, 40.0, 15.0))
    _advance_until(control, "hover", max_sec=12.0)

    telemetry = control.get_telemetry()
    assert math.hypot(telemetry.north_m - 30.0, telemetry.east_m - 40.0) <= 1.0


def test_land_ends_grounded_at_zero_down():
    control = KinematicFlightControl(start_altitude_m=10.0)

    control.send_command(Land())
    assert control.get_telemetry().flight_mode == "landing"
    _advance_until(control, "grounded", max_sec=12.0)

    assert control.get_telemetry().down_m == 0.0


def test_return_to_home_keeps_minimum_altitude():
    control = KinematicFlightControl(start_north_m=50.0, start_altitude_m=5.0)

    control.send_command(ReturnToHome())
    assert control.get_telemetry().flight_mode == "returning"
    _advance_until(control, "hover", max_sec=30.0)

    telemetry = control.get_telemetry()
    assert math.hypot(telemetry.north_m, telemetry.east_m) <= 1.0
    assert abs(telemetry.altitude_m - 10.0) <= 0.5


def test_reject_commands_returns_rejected_ack_and_changes_nothing():
    control = KinematicFlightControl(start_altitude_m=10.0)
    control.reject_commands = True

    ack = control.send_command(GoTo(30.0, 0.0, 10.0))
    _advance(control, 2.0)

    assert ack.accepted is False
    assert control.commands_received == ()
    assert control.get_telemetry().north_m == 0.0


def test_gimbal_is_clamped():
    control = KinematicFlightControl()

    control.send_command(SetGimbal(-120.0, 200.0))

    telemetry = control.get_telemetry()
    assert (telemetry.gimbal_pitch_deg, telemetry.gimbal_yaw_deg) == (-90.0, 90.0)


def test_fire_heart_is_accepted_without_mode_change():
    control = KinematicFlightControl(start_altitude_m=10.0)

    ack = control.send_command(FireHeart(3))

    assert ack.accepted is True
    assert control.get_telemetry().flight_mode == "hover"


@pytest.mark.parametrize("dt_sec", [0.0, -0.1, math.nan, math.inf])
def test_advance_rejects_invalid_dt(dt_sec):
    with pytest.raises(ValueError):
        KinematicFlightControl().advance(dt_sec)


def test_telemetry_time_follows_now_sec():
    control = KinematicFlightControl(start_time_sec=5.0)

    _advance(control, 1.0)

    assert control.get_telemetry().time_sec == control.now_sec
    assert control.now_sec == pytest.approx(6.0)


def _flying_control() -> KinematicFlightControl:
    control = KinematicFlightControl(start_north_m=20.0, start_east_m=-10.0, start_altitude_m=15.0)
    _advance(control, 10.0)

    return control


def test_fault_injection_defaults_keep_telemetry_unchanged():
    control = _flying_control()

    telemetry = control.get_telemetry()

    assert (control.gps_lost, control.gps_offset_m, control.battery_offset_pct) == (False, (0.0, 0.0), 0.0)
    assert telemetry.gps == ned_to_gps(20.0, -10.0, -15.0)
    assert telemetry.battery_pct == pytest.approx(100.0 - 10.0 * 100.0 / 600.0)


def test_gps_lost_hides_gps_but_keeps_true_ned():
    control = _flying_control()

    control.gps_lost = True
    lost = control.get_telemetry()
    control.gps_lost = False
    restored = control.get_telemetry()

    assert lost.gps is None
    assert (lost.north_m, lost.east_m) == (20.0, -10.0)
    assert lost.battery_pct == restored.battery_pct
    assert restored.gps == ned_to_gps(20.0, -10.0, -15.0)


def test_gps_offset_shifts_gps_only():
    control = _flying_control()

    control.gps_offset_m = (500.0, 0.0)
    telemetry = control.get_telemetry()

    assert telemetry.gps == ned_to_gps(520.0, -10.0, -15.0)
    assert telemetry.gps.latitude_deg > ned_to_gps(20.0, -10.0, -15.0).latitude_deg
    assert (telemetry.north_m, telemetry.east_m) == (20.0, -10.0)


def test_gps_lost_wins_over_gps_offset():
    control = _flying_control()

    control.gps_lost = True
    control.gps_offset_m = (500.0, 0.0)

    assert control.get_telemetry().gps is None


@pytest.mark.parametrize(
    ("offset_pct", "expected_pct"),
    [
        (30.0, 100.0 - 10.0 * 100.0 / 600.0 - 30.0),
        (150.0, 0.0),
        (-50.0, 100.0),
    ],
)
def test_battery_offset_is_subtracted_and_clamped(offset_pct, expected_pct):
    control = _flying_control()

    control.battery_offset_pct = offset_pct

    assert control.get_telemetry().battery_pct == pytest.approx(expected_pct)
