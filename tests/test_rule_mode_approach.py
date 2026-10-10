"""RM-05 — approach 모드를 운동 모델 드론 + 스크립트 대상 + 실제 TrackAggregator 로 10 Hz 헤드리스 실행한다."""

import logging
import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import GoTo, SetGimbal, TakeOff
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.approach import ApproachMode
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource

TICK_SEC = 0.1
CRUISE_ALTITUDE_M = 15.0
APPROACH_PARAMS = {"target_track": "T7", "stop_distance_m": 5.0, "timeout_sec": 30.0}


def _run_approach(targets, params=APPROACH_PARAMS, setup=None, max_sec=60.0):
    """approach 를 10 Hz 로 돌린다. (결과, 시작 후 종료까지 걸린 초, control, source) 를 돌려준다.

    setup(elapsed_sec, control, source) 는 매 tick, 모드 step 직전에 불린다.
    """
    control = KinematicFlightControl()
    control.send_command(TakeOff(CRUISE_ALTITUDE_M))
    while control.get_telemetry().flight_mode != "hover":
        control.advance(TICK_SEC)

    control.send_command(SetGimbal(-30.0, 0.0))
    control.advance(1.0)

    source = ScriptedTargetSource(control, targets)
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    mode = ApproachMode(params, RuleModeSettings())
    start_sec = control.now_sec

    while control.now_sec - start_sec < max_sec:
        elapsed_sec = control.now_sec - start_sec
        if setup is not None:
            setup(elapsed_sec, control, source)

        frame = source.read_frame()
        frame_telemetry = source.telemetry_for(frame.timestamp_sec)
        aggregator.update(frame, frame_telemetry)
        tick = ModeTick(
            now_sec=control.now_sec,
            telemetry=control.get_telemetry(),
            tracks=tuple(aggregator.snapshot(control.now_sec)),
            frame=frame,
            frame_telemetry=frame_telemetry,
            hearted_track_ids=frozenset(),
        )

        result = mode.step(tick, control)
        if result is not None:
            return result, elapsed_sec, control, source

        control.advance(TICK_SEC)

    return None, max_sec, control, source


def _distance_to_target(control, target: ScriptedTarget) -> float:
    telemetry = control.get_telemetry()
    north_m, east_m, _ = target.get_position_ned(telemetry.time_sec)

    return math.hypot(north_m - telemetry.north_m, east_m - telemetry.east_m)


def test_approach_is_a_rule_mode_and_validates_params():
    mode = ApproachMode(APPROACH_PARAMS, RuleModeSettings())

    assert isinstance(mode, RuleMode)
    assert mode.rule_mode == "approach"

    for bad_params in (
        {**APPROACH_PARAMS, "target_track": "X7"},
        {**APPROACH_PARAMS, "target_track": None},
        {**APPROACH_PARAMS, "stop_distance_m": 0},
        {**APPROACH_PARAMS, "timeout_sec": -1},
    ):
        with pytest.raises(ValueError):
            ApproachMode(bad_params, RuleModeSettings())


def test_approach_stops_near_stationary_target_and_succeeds():
    target = ScriptedTarget(7, "person", 40.0, 5.0)

    result, _, control, _ = _run_approach([target])

    assert result == ModeResult("approach", "success", "T7")
    assert 4.0 <= _distance_to_target(control, target) <= 6.5
    assert control.get_telemetry().altitude_m == pytest.approx(CRUISE_ALTITUDE_M, abs=0.5)

    command_list = [command for _, command in control.commands_received]
    assert isinstance(command_list[-1], GoTo)
    assert sum(isinstance(command, GoTo) for command in command_list[:-1]) >= 2


def test_approach_follows_moving_target_and_succeeds():
    target = ScriptedTarget(7, "person", 40.0, 5.0, east_mps=0.3)

    result, _, control, _ = _run_approach([target])

    assert result == ModeResult("approach", "success", "T7")
    assert 4.0 <= _distance_to_target(control, target) <= 7.0


def test_approach_fails_when_target_track_is_lost_for_over_three_seconds():
    removed_at_sec = 3.0

    def remove_target_later(elapsed_sec, control, source):
        if elapsed_sec >= removed_at_sec:
            source.remove_target(7)

    result, end_sec, _, _ = _run_approach([ScriptedTarget(7, "person", 80.0, 5.0)], setup=remove_target_later)

    assert result == ModeResult("approach", "failure", "T7")
    # track 이 비활성이 되기까지 2.0 s (트래커) + 놓침 판정 3.0 s
    assert removed_at_sec + 2.0 + 3.0 <= end_sec <= removed_at_sec + 5.5


def test_approach_times_out_and_hovers():
    params = {**APPROACH_PARAMS, "timeout_sec": 3.0}

    result, end_sec, control, _ = _run_approach([ScriptedTarget(7, "person", 100.0, 0.0)], params=params)

    assert result == ModeResult("approach", "timeout", "T7")
    assert 3.0 <= end_sec <= 3.3
    assert isinstance(control.commands_received[-1][1], GoTo)


def test_approach_fails_when_commands_are_rejected_at_start():
    def reject_from_start(elapsed_sec, control, source):
        control.reject_commands = True

    result, end_sec, _, _ = _run_approach([ScriptedTarget(7, "person", 40.0, 5.0)], setup=reject_from_start)

    assert result == ModeResult("approach", "failure", "T7")
    assert end_sec == pytest.approx(0.0, abs=1e-6)


def test_approach_fails_within_regoal_period_when_commands_are_rejected_in_flight():
    rejected_at_sec = 2.0

    def reject_later(elapsed_sec, control, source):
        if elapsed_sec >= rejected_at_sec:
            control.reject_commands = True

    result, end_sec, _, _ = _run_approach([ScriptedTarget(7, "person", 60.0, 0.0)], setup=reject_later)

    assert result == ModeResult("approach", "failure", "T7")
    assert rejected_at_sec <= end_sec <= rejected_at_sec + 1.2


def test_approach_logs_both_estimates(caplog):
    with caplog.at_level(logging.INFO, logger="modules.rule_mode.approach"):
        result, _, _, _ = _run_approach([ScriptedTarget(7, "person", 40.0, 5.0)])

    assert result is not None
    assert "ground_projection=" in caplog.text
    assert "box_height=" in caplog.text
