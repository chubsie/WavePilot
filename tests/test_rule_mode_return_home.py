import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import GoTo
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings
from modules.rule_mode.return_home import ReturnHomeMode
from modules.simulation.kinematic_flight import KinematicFlightControl

TICK_SEC = 0.1
HOME_PARAMS = {"home_north_m": 0.0, "home_east_m": 0.0, "return_altitude_m": 10.0}


def _make_tick(control: KinematicFlightControl) -> ModeTick:
    return ModeTick(
        now_sec=control.now_sec,
        telemetry=control.get_telemetry(),
        tracks=(),
        frame=None,
        frame_telemetry=None,
        hearted_track_ids=frozenset(),
    )


def _run_until_result(mode, control: KinematicFlightControl, max_sec: float) -> tuple[ModeResult, float]:
    start_sec = control.now_sec
    while control.now_sec - start_sec <= max_sec:
        result = mode.step(_make_tick(control), control)
        if result is not None:
            return result, control.now_sec - start_sec
        control.advance(TICK_SEC)

    pytest.fail(f"mode did not finish within {max_sec} s")


def _distance_to(control: KinematicFlightControl, north_m: float, east_m: float) -> float:
    telemetry = control.get_telemetry()

    return math.hypot(telemetry.north_m - north_m, telemetry.east_m - east_m)


def test_satisfies_rule_mode_protocol():
    assert isinstance(ReturnHomeMode(HOME_PARAMS, RuleModeSettings()), RuleMode)


def test_flies_home_and_lands():
    control = KinematicFlightControl(start_north_m=40.0, start_east_m=30.0, start_altitude_m=15.0)
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings())

    result, _ = _run_until_result(mode, control, max_sec=60.0)

    telemetry = control.get_telemetry()
    kinds = [command.KIND for _, command in control.commands_received]
    assert result == ModeResult("return_home", "success", None)
    assert telemetry.flight_mode == "grounded"
    assert telemetry.down_m == 0.0
    assert _distance_to(control, 0.0, 0.0) <= 1.5
    assert kinds == ["go_to", "land"]
    assert control.commands_received[0][1] == GoTo(0.0, 0.0, 10.0)


def test_lands_at_non_origin_home():
    control = KinematicFlightControl(start_north_m=40.0, start_east_m=30.0, start_altitude_m=15.0)
    params = {"home_north_m": -20.0, "home_east_m": 15.0, "return_altitude_m": 10.0}
    mode = ReturnHomeMode(params, RuleModeSettings())

    result, _ = _run_until_result(mode, control, max_sec=60.0)

    assert result == ModeResult("return_home", "success", None)
    assert _distance_to(control, -20.0, 15.0) <= 1.5


def test_succeeds_when_starting_on_the_ground_away_from_home():
    control = KinematicFlightControl(start_north_m=30.0)
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings())

    result, _ = _run_until_result(mode, control, max_sec=60.0)

    assert result == ModeResult("return_home", "success", None)
    assert control.get_telemetry().flight_mode == "grounded"


def test_timeout_reports_without_cancelling_the_flight():
    control = KinematicFlightControl(start_north_m=400.0, start_altitude_m=15.0)
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings(), timeout_sec=5.0)

    result, elapsed_sec = _run_until_result(mode, control, max_sec=10.0)
    distance_at_timeout_m = _distance_to(control, 0.0, 0.0)
    for _ in range(20):
        control.advance(TICK_SEC)

    assert result == ModeResult("return_home", "timeout", None)
    assert elapsed_sec >= 5.0
    assert len(control.commands_received) == 1
    assert _distance_to(control, 0.0, 0.0) < distance_at_timeout_m


def test_rejected_first_command_fails_on_first_tick():
    control = KinematicFlightControl(start_north_m=40.0, start_altitude_m=15.0)
    control.reject_commands = True
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings())

    result, elapsed_sec = _run_until_result(mode, control, max_sec=10.0)

    assert result == ModeResult("return_home", "failure", None)
    assert elapsed_sec == 0.0


def test_rejected_land_fails_on_arrival():
    control = KinematicFlightControl(start_north_m=20.0, start_altitude_m=10.0)
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings())
    assert mode.step(_make_tick(control), control) is None
    control.reject_commands = True

    result, elapsed_sec = _run_until_result(mode, control, max_sec=30.0)

    assert result == ModeResult("return_home", "failure", None)
    assert elapsed_sec > 0.0
    assert len(control.commands_received) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"home_east_m": 0.0, "return_altitude_m": 10.0},
        {"home_north_m": math.nan, "home_east_m": 0.0, "return_altitude_m": 10.0},
        {"home_north_m": 0.0, "home_east_m": 0.0, "return_altitude_m": 0},
        {"home_north_m": True, "home_east_m": 0.0, "return_altitude_m": 10.0},
    ],
)
def test_invalid_params_raise(params):
    with pytest.raises(ValueError):
        ReturnHomeMode(params, RuleModeSettings())


def test_step_after_result_raises():
    control = KinematicFlightControl(start_altitude_m=10.0)
    mode = ReturnHomeMode(HOME_PARAMS, RuleModeSettings())
    _run_until_result(mode, control, max_sec=30.0)

    with pytest.raises(RuntimeError):
        mode.step(_make_tick(control), control)
