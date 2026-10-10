import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import GoTo
from modules.rule_mode.hold import HoldMode
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings
from modules.simulation.kinematic_flight import KinematicFlightControl

TICK_SEC = 0.1


def _run_until_result(mode, control: KinematicFlightControl, max_sec: float) -> tuple[ModeResult, float]:
    start_sec = control.now_sec
    while control.now_sec - start_sec <= max_sec:
        tick = ModeTick(
            now_sec=control.now_sec,
            telemetry=control.get_telemetry(),
            tracks=(),
            frame=None,
            frame_telemetry=None,
            hearted_track_ids=frozenset(),
        )
        result = mode.step(tick, control)
        if result is not None:
            return result, control.now_sec - start_sec
        control.advance(TICK_SEC)

    pytest.fail(f"mode did not finish within {max_sec} s")


def _make_control() -> KinematicFlightControl:
    return KinematicFlightControl(start_north_m=10.0, start_east_m=5.0, start_altitude_m=15.0)


def test_satisfies_rule_mode_protocol():
    assert isinstance(HoldMode({"duration_sec": 1.0}, RuleModeSettings()), RuleMode)


def test_hovers_in_place_then_succeeds():
    control = _make_control()
    mode = HoldMode({"duration_sec": 3.0}, RuleModeSettings())

    result, elapsed_sec = _run_until_result(mode, control, max_sec=10.0)

    telemetry = control.get_telemetry()
    assert result == ModeResult("hold", "success", None)
    assert 3.0 <= elapsed_sec <= 3.2
    assert [command for _, command in control.commands_received] == [GoTo(10.0, 5.0, 15.0)]
    assert math.hypot(telemetry.north_m - 10.0, telemetry.east_m - 5.0) <= 0.5
    assert abs(telemetry.altitude_m - 15.0) <= 0.5


def test_rejected_command_fails_on_first_tick():
    control = _make_control()
    control.reject_commands = True
    mode = HoldMode({"duration_sec": 3.0}, RuleModeSettings())

    result, elapsed_sec = _run_until_result(mode, control, max_sec=10.0)

    assert result == ModeResult("hold", "failure", None)
    assert elapsed_sec == 0.0


@pytest.mark.parametrize("params", [{"duration_sec": 0}, {"duration_sec": -1}, {"duration_sec": True}, {}])
def test_invalid_params_raise(params):
    with pytest.raises(ValueError):
        HoldMode(params, RuleModeSettings())


def test_step_after_result_raises():
    control = _make_control()
    mode = HoldMode({"duration_sec": 1.0}, RuleModeSettings())
    _run_until_result(mode, control, max_sec=5.0)

    tick = ModeTick(
        now_sec=control.now_sec, telemetry=control.get_telemetry(), tracks=(), frame=None,
        frame_telemetry=None, hearted_track_ids=frozenset(),
    )
    with pytest.raises(RuntimeError):
        mode.step(tick, control)
