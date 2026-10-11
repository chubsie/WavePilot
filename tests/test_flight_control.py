"""MockFlightControl — 감시 위치 호버링, 배터리 선형 소모, 받은 명령 기록 (MOCK-02, IF-02)."""

import pytest

from modules.flight_control.interface.flight_control import FlightControl, GoTo, Land, ReturnToHome, TakeOff
from modules.simulation.clock import SimClock
from modules.flight_control.mock import BATTERY_DRAIN_PCT_PER_SEC, MockFlightControl


@pytest.fixture
def clock() -> SimClock:
    return SimClock()


@pytest.fixture
def flight_control(clock: SimClock) -> MockFlightControl:
    return MockFlightControl(clock)


def test_initial_telemetry_hovers_at_watch_position(flight_control: MockFlightControl) -> None:
    telemetry = flight_control.get_telemetry()

    assert isinstance(flight_control, FlightControl)
    assert telemetry.time_sec == 0.0
    assert (telemetry.north_m, telemetry.east_m) == (0.0, 0.0)
    assert telemetry.down_m == -40.0
    assert telemetry.altitude_m == 40.0
    assert telemetry.gimbal_pitch_deg == -55.0
    assert telemetry.gimbal_yaw_deg == 0.0
    assert (telemetry.roll_deg, telemetry.pitch_deg, telemetry.yaw_deg) == (0.0, 0.0, 0.0)
    assert telemetry.flight_mode == "hover"
    assert telemetry.battery_pct == 100.0


def test_battery_drains_linearly_with_sim_time(clock: SimClock, flight_control: MockFlightControl) -> None:
    clock.advance_to(50.0)

    telemetry = flight_control.get_telemetry()

    assert telemetry.time_sec == 50.0
    assert telemetry.battery_pct == pytest.approx(100.0 - 50.0 * BATTERY_DRAIN_PCT_PER_SEC)


def test_battery_never_goes_below_zero(clock: SimClock, flight_control: MockFlightControl) -> None:
    clock.advance_to(1_000_000.0)

    assert flight_control.get_telemetry().battery_pct == 0.0


def test_custom_hover_parameters(clock: SimClock) -> None:
    flight_control = MockFlightControl(
        clock, hover_north_m=5.0, hover_east_m=-3.0, hover_altitude_m=60.0, gimbal_pitch_deg=-45.0
    )

    telemetry = flight_control.get_telemetry()

    assert (telemetry.north_m, telemetry.east_m, telemetry.down_m) == (5.0, -3.0, -60.0)
    assert telemetry.gimbal_pitch_deg == -45.0


def test_return_to_home_is_recorded(clock: SimClock, flight_control: MockFlightControl) -> None:
    clock.advance_to(12.5)

    ack = flight_control.send_command(ReturnToHome())

    assert ack.accepted is True
    assert ack.reason
    assert flight_control.get_telemetry().flight_mode == "returning"
    assert flight_control.commands_received[-1] == (12.5, ReturnToHome())


@pytest.mark.parametrize(
    ("command", "expected_mode"),
    [
        (GoTo(north_m=10.0, east_m=5.0, altitude_m=40.0), "moving"),
        (Land(), "landing"),
        (TakeOff(altitude_m=40.0), "hover"),
        (ReturnToHome(), "returning"),
    ],
)
def test_command_sets_mode_without_moving(flight_control: MockFlightControl, command, expected_mode) -> None:
    before = flight_control.get_telemetry()

    ack = flight_control.send_command(command)
    after = flight_control.get_telemetry()

    assert ack.accepted is True
    assert after.flight_mode == expected_mode
    assert (after.north_m, after.east_m, after.down_m) == (before.north_m, before.east_m, before.down_m)


def test_commands_received_keeps_order_and_time(clock: SimClock, flight_control: MockFlightControl) -> None:
    flight_control.send_command(GoTo(north_m=1.0, east_m=2.0, altitude_m=40.0))
    clock.advance_to(3.0)
    flight_control.send_command(ReturnToHome())
    clock.advance_to(4.0)
    flight_control.send_command(TakeOff(altitude_m=40.0))

    received = flight_control.commands_received

    assert isinstance(received, tuple)
    assert received == (
        (0.0, GoTo(north_m=1.0, east_m=2.0, altitude_m=40.0)),
        (3.0, ReturnToHome()),
        (4.0, TakeOff(altitude_m=40.0)),
    )
    assert flight_control.get_telemetry().flight_mode == "hover"
