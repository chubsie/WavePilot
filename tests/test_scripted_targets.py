"""스크립트 월드 대상 탐지 소스 검증 — 투영, 가시성, 프레임별 텔레메트리, 추정 왕복, 운동 모델 AGL."""

import math
from dataclasses import replace

import pytest

from modules.flight_control.interface import CommandAck, TakeOff
from modules.perception.interface import DEFAULT_CAMERA, project_ned_to_pixel
from modules.rule_mode.target_estimate import estimate_ground_position
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource


class _FixedControl:
    """고정 텔레메트리만 돌려주는 FlightControl 스텁."""

    def __init__(self, telemetry) -> None:
        self.telemetry = telemetry

    def get_telemetry(self):
        return self.telemetry

    def send_command(self, command) -> CommandAck:
        return CommandAck(True, "ok")


@pytest.fixture
def drone_telemetry(hover_telemetry):
    return replace(hover_telemetry, down_m=-15.0, gimbal_pitch_deg=-30.0)


def test_visible_target_projects_feet_to_box_bottom_center_and_head_to_top(drone_telemetry):
    target = ScriptedTarget(7, "person", 30.0, 0.0)
    source = ScriptedTargetSource(_FixedControl(drone_telemetry), [target])

    frame = source.read_frame()

    assert len(frame.detections) == 1
    detection = frame.detections[0]
    feet = project_ned_to_pixel((30.0, 0.0, 0.0), DEFAULT_CAMERA, drone_telemetry)
    head = project_ned_to_pixel((30.0, 0.0, -1.7), DEFAULT_CAMERA, drone_telemetry)
    x1, y1, x2, y2 = detection.xyxy
    assert detection.class_id == 0
    assert detection.track_id == 7
    assert (x1 + x2) / 2.0 == pytest.approx(feet[0], abs=1e-6)
    assert y2 == pytest.approx(feet[1], abs=1e-6)
    assert y1 == pytest.approx(head[1], abs=1e-6)


def test_targets_behind_beside_or_cut_off_at_top_are_not_detected(hover_telemetry):
    close_telemetry = replace(hover_telemetry, down_m=0.0, gimbal_pitch_deg=0.0)
    targets = [
        ScriptedTarget(1, "person", -30.0, 0.0),
        ScriptedTarget(2, "person", 10.0, 60.0),
        ScriptedTarget(3, "person", 3.0, 0.0),
    ]

    frame = ScriptedTargetSource(_FixedControl(close_telemetry), targets).read_frame()

    assert frame.detections == ()


def test_frame_ids_increase_and_telemetry_for_returns_capture_time_telemetry(drone_telemetry):
    control = _FixedControl(drone_telemetry)
    source = ScriptedTargetSource(control, [ScriptedTarget(7, "person", 30.0, 0.0)])

    first_frame = source.read_frame()
    control.telemetry = replace(drone_telemetry, time_sec=0.1, north_m=2.0)
    second_frame = source.read_frame()
    third_frame = source.read_frame()

    assert [first_frame.frame_id, second_frame.frame_id, third_frame.frame_id] == [0, 1, 2]
    assert (first_frame.image_width, first_frame.image_height) == (1280, 720)
    assert source.telemetry_for(first_frame.timestamp_sec) == drone_telemetry
    assert source.telemetry_for(second_frame.timestamp_sec) == control.telemetry
    assert source.telemetry_for(123.456) is None


def test_moving_target_is_projected_at_its_position_for_the_frame_time(drone_telemetry):
    target = ScriptedTarget(7, "person", 20.0, 0.0, north_mps=1.0)
    late_telemetry = replace(drone_telemetry, time_sec=10.0)
    source = ScriptedTargetSource(_FixedControl(late_telemetry), [target])

    detection = source.read_frame().detections[0]

    feet = project_ned_to_pixel((30.0, 0.0, 0.0), DEFAULT_CAMERA, late_telemetry)
    x1, _, x2, y2 = detection.xyxy
    assert (x1 + x2) / 2.0 == pytest.approx(feet[0], abs=1e-6)
    assert y2 == pytest.approx(feet[1], abs=1e-6)


def test_remove_target_drops_detection_and_invalid_targets_raise(drone_telemetry):
    source = ScriptedTargetSource(_FixedControl(drone_telemetry), [ScriptedTarget(7, "person", 30.0, 0.0)])

    source.remove_target(99)
    assert len(source.read_frame().detections) == 1

    source.remove_target(7)
    assert source.read_frame().detections == ()
    assert source.targets == ()

    with pytest.raises(ValueError):
        ScriptedTarget(1, "unicorn", 0.0, 0.0)
    with pytest.raises(ValueError):
        ScriptedTargetSource(
            _FixedControl(drone_telemetry),
            [ScriptedTarget(1, "person", 20.0, 0.0), ScriptedTarget(1, "person", 30.0, 0.0)],
        )


def test_ground_projection_round_trip_recovers_scripted_target(drone_telemetry):
    target = ScriptedTarget(7, "person", 28.0, -6.0)
    source = ScriptedTargetSource(_FixedControl(drone_telemetry), [target])

    detection = source.read_frame().detections[0]
    estimate = estimate_ground_position(detection, drone_telemetry, DEFAULT_CAMERA)

    assert estimate is not None
    assert math.hypot(estimate[0] - 28.0, estimate[1] + 6.0) <= 0.1


def test_kinematic_flight_reports_flat_ground_agl():
    control = KinematicFlightControl()
    assert control.get_telemetry().height_above_ground_m == 0.0

    control.send_command(TakeOff(15.0))
    for _ in range(300):
        if control.get_telemetry().flight_mode == "hover":
            break
        control.advance(0.1)

    telemetry = control.get_telemetry()
    assert telemetry.flight_mode == "hover"
    assert telemetry.height_above_ground_m == pytest.approx(telemetry.altitude_m)


@pytest.mark.parametrize("upper_color", ["red", "other", None])
def test_person_target_upper_color_is_carried_to_detection(drone_telemetry, upper_color):
    target = ScriptedTarget(7, "person", 30.0, 0.0, upper_color=upper_color)

    frame = ScriptedTargetSource(_FixedControl(drone_telemetry), [target]).read_frame()

    assert frame.detections[0].upper_color == upper_color


def test_target_upper_color_defaults_to_none(drone_telemetry):
    target = ScriptedTarget(7, "person", 30.0, 0.0)

    frame = ScriptedTargetSource(_FixedControl(drone_telemetry), [target]).read_frame()

    assert target.upper_color is None
    assert frame.detections[0].upper_color is None


def test_target_rejects_unknown_upper_color_or_color_on_non_person():
    with pytest.raises(ValueError, match="upper_color"):
        ScriptedTarget(1, "person", 10.0, 0.0, upper_color="blue")
    with pytest.raises(ValueError, match="person"):
        ScriptedTarget(2, "car", 10.0, 0.0, upper_color="red")
