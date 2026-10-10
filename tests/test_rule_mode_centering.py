"""D-08/D-09 — 중앙 맞춤 제어기 순수 함수, 놓침 타이머, params 파싱, RuleModeSettings 확장(C5)."""

import math

import pytest

from modules.flight_control.interface import CommandAck, SetGimbal, SetYaw, Telemetry
from modules.perception.interface import DEFAULT_CAMERA, Track
from modules.rule_mode.centering import (
    PITCH_GAIN,
    YAW_GAIN,
    TargetLossTimer,
    compute_centering,
    find_target_detection,
    find_target_track,
    parse_frame_count_param,
    parse_positive_param,
    parse_ratio_param,
    parse_target_track,
    send_centering,
)
from modules.rule_mode.interface import RuleModeSettings
from modules.yolo_detection.interface import Detection, Frame

WIDTH_PX = 1280
HEIGHT_PX = 720
BOX_HALF_PX = 20.0


def _make_telemetry(yaw_deg: float = 0.0, gimbal_pitch_deg: float = -30.0) -> Telemetry:
    return Telemetry(
        time_sec=0.0,
        north_m=0.0,
        east_m=0.0,
        down_m=-15.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=yaw_deg,
        gimbal_pitch_deg=gimbal_pitch_deg,
        gimbal_yaw_deg=0.0,
        battery_pct=100.0,
        flight_mode="hover",
    )


def _make_detection(center_u: float, center_v: float, track_id: int = 7) -> Detection:
    return Detection(
        class_id=0,
        class_name="person",
        confidence=0.9,
        xyxy=(center_u - BOX_HALF_PX, center_v - BOX_HALF_PX, center_u + BOX_HALF_PX, center_v + BOX_HALF_PX),
        track_id=track_id,
    )


def _make_frame(detections=(), width_px: int = WIDTH_PX, height_px: int = HEIGHT_PX) -> Frame:
    return Frame(1, 0.0, width_px, height_px, tuple(detections))


def _compute(
    center_u, center_v, yaw_deg=0.0, gimbal_pitch_deg=-30.0, ratio=0.05, width_px=WIDTH_PX, height_px=HEIGHT_PX
):
    detection = _make_detection(center_u, center_v)
    frame = _make_frame((detection,), width_px, height_px)
    telemetry = _make_telemetry(yaw_deg, gimbal_pitch_deg)

    return compute_centering(detection, frame, telemetry, DEFAULT_CAMERA, ratio)


# --- compute_centering: 부호·크기 ---


def test_box_right_of_center_turns_yaw_right_by_gain_times_angle():
    command = _compute(WIDTH_PX / 2 + 200.0, HEIGHT_PX / 2, yaw_deg=10.0)

    expected_deg = 10.0 + YAW_GAIN * math.degrees(math.atan(200.0 / DEFAULT_CAMERA.fx_px))
    assert command.yaw_deg == pytest.approx(expected_deg, abs=1e-6)
    assert command.error_x_px == pytest.approx(200.0)


def test_box_left_of_center_turns_yaw_left():
    command = _compute(WIDTH_PX / 2 - 200.0, HEIGHT_PX / 2, yaw_deg=10.0)

    assert command.yaw_deg < 10.0


def test_box_below_center_tilts_gimbal_further_down():
    command = _compute(WIDTH_PX / 2, HEIGHT_PX / 2 + 100.0, gimbal_pitch_deg=-30.0)

    expected_deg = -30.0 - PITCH_GAIN * math.degrees(math.atan(100.0 / DEFAULT_CAMERA.fy_px))
    assert command.gimbal_pitch_deg == pytest.approx(expected_deg, abs=1e-6)
    assert command.gimbal_pitch_deg < -30.0


def test_box_above_center_tilts_gimbal_up():
    command = _compute(WIDTH_PX / 2, HEIGHT_PX / 2 - 100.0, gimbal_pitch_deg=-30.0)

    assert -30.0 < command.gimbal_pitch_deg <= 0.0


def test_gimbal_pitch_is_clamped_to_minus_ninety_and_zero():
    assert _compute(WIDTH_PX / 2, HEIGHT_PX - 30.0, gimbal_pitch_deg=-88.0).gimbal_pitch_deg == -90.0
    assert _compute(WIDTH_PX / 2, 30.0, gimbal_pitch_deg=-1.0).gimbal_pitch_deg == 0.0


def test_yaw_wraps_into_half_open_range():
    command = _compute(WIDTH_PX - 30.0, HEIGHT_PX / 2, yaw_deg=179.0)

    assert -180.0 < command.yaw_deg < 0.0


# --- 중앙 판정 경계 (D-09) ---


def test_centered_boundary_is_inclusive_horizontally_and_vertically():
    tolerance_x_px = 0.05 * WIDTH_PX
    tolerance_y_px = 0.05 * HEIGHT_PX

    assert _compute(WIDTH_PX / 2 + tolerance_x_px, HEIGHT_PX / 2).is_centered
    assert not _compute(WIDTH_PX / 2 + tolerance_x_px + 1.0, HEIGHT_PX / 2).is_centered
    assert _compute(WIDTH_PX / 2, HEIGHT_PX / 2 - tolerance_y_px).is_centered
    assert not _compute(WIDTH_PX / 2, HEIGHT_PX / 2 - tolerance_y_px - 1.0).is_centered


def test_not_centered_when_only_vertical_error_is_outside():
    command = _compute(WIDTH_PX / 2 + 10.0, HEIGHT_PX / 2 + 0.05 * HEIGHT_PX + 5.0)

    assert abs(command.error_x_px) <= 0.05 * WIDTH_PX
    assert not command.is_centered


def test_smaller_frame_scales_pixels_to_camera_resolution():
    small = _compute(320.0 + 100.0, 180.0 + 50.0, width_px=640, height_px=360)
    full = _compute(640.0 + 200.0, 360.0 + 100.0)

    assert small.yaw_deg == pytest.approx(full.yaw_deg, abs=1e-6)
    assert small.gimbal_pitch_deg == pytest.approx(full.gimbal_pitch_deg, abs=1e-6)
    assert small.error_x_px == pytest.approx(100.0)
    # 판정은 프레임 자신의 크기 기준: 640 * 0.05 = 32 px
    assert not small.is_centered
    assert _compute(320.0 + 32.0, 180.0, width_px=640, height_px=360).is_centered


# --- send_centering ---


class _RecordingControl:
    def __init__(self, rejected_kinds=()):
        self.command_list = []
        self._rejected_kinds = set(rejected_kinds)

    def get_telemetry(self):
        return _make_telemetry()

    def send_command(self, command):
        self.command_list.append(command)
        if command.KIND in self._rejected_kinds:
            return CommandAck(False, "rejected for test")
        return CommandAck(True, "")


def test_send_centering_sends_yaw_and_gimbal_and_reports_acceptance():
    command = _compute(WIDTH_PX / 2 + 100.0, HEIGHT_PX / 2 + 50.0)
    control = _RecordingControl()

    assert send_centering(control, command, "T7", "observe") is True
    assert control.command_list == [SetYaw(command.yaw_deg), SetGimbal(command.gimbal_pitch_deg, 0.0)]


@pytest.mark.parametrize("rejected_kind", [SetYaw.KIND, SetGimbal.KIND])
def test_send_centering_returns_false_when_any_command_is_rejected(rejected_kind):
    command = _compute(WIDTH_PX / 2, HEIGHT_PX / 2)

    assert send_centering(_RecordingControl({rejected_kind}), command, "T7", "observe") is False


# --- 대상 찾기 ---


def test_find_target_detection_and_track():
    detection = _make_detection(100.0, 100.0, track_id=7)
    other = _make_detection(200.0, 200.0, track_id=8)
    frame = _make_frame((other, detection))
    track = Track(7, "person", 10.0, 0.0, 0.0, 1.0, 5, 0.9, True)

    assert find_target_detection(frame, 7) is detection
    assert find_target_detection(frame, 9) is None
    assert find_target_detection(None, 7) is None
    assert find_target_track((track,), 7) is track
    assert find_target_track((track,), 8) is None


# --- TargetLossTimer ---


def test_target_loss_timer_trips_only_after_grace_and_resets_when_seen():
    timer = TargetLossTimer(1.0)

    assert timer.update(0.0, True) is False
    assert timer.update(1.0, True) is False
    assert timer.update(1.1, True) is True
    assert timer.update(1.2, False) is False
    assert timer.update(1.3, True) is False
    assert timer.update(2.2, True) is False
    assert timer.update(2.4, True) is True


# --- params 파싱 ---


def test_parse_target_track_accepts_label_or_int():
    assert parse_target_track("T7", "observe") == 7
    assert parse_target_track(7, "observe") == 7

    for bad in ("X7", -1, None, True, "T"):
        with pytest.raises(ValueError, match="observe"):
            parse_target_track(bad, "observe")


def test_parse_numeric_params_use_defaults_and_reject_bad_values():
    assert parse_positive_param({}, "timeout_sec", 10.0, "observe") == 10.0
    assert parse_positive_param({"timeout_sec": 3}, "timeout_sec", 10.0, "observe") == 3.0
    assert parse_frame_count_param({}, "stable_frame_count", 5, "aim_and_fire") == 5
    assert parse_ratio_param({"center_tolerance_ratio": 0.5}, "center_tolerance_ratio", 0.05, "aim_and_fire") == 0.5

    for bad in (0, -1, math.nan, True):
        with pytest.raises(ValueError):
            parse_positive_param({"timeout_sec": bad}, "timeout_sec", 10.0, "observe")
    for bad in (0, 1.5, True, -2):
        with pytest.raises(ValueError):
            parse_frame_count_param({"stable_frame_count": bad}, "stable_frame_count", 5, "aim_and_fire")
    for bad in (0, 0.6, -0.1, math.inf):
        with pytest.raises(ValueError):
            parse_ratio_param({"center_tolerance_ratio": bad}, "center_tolerance_ratio", 0.05, "aim_and_fire")


# --- RuleModeSettings 확장 (C5) ---


def test_rule_mode_settings_new_fields_have_defaults():
    settings = RuleModeSettings()

    assert settings.target_upper_color is None
    assert settings.heart_burst_count == 10
    assert settings.heart_burst_interval_sec == 0.2
    assert settings.target_lost_grace_sec == 1.0
    assert RuleModeSettings(target_upper_color="red").target_upper_color == "red"


@pytest.mark.parametrize(
    "overrides",
    [
        {"heart_burst_count": 0},
        {"heart_burst_count": -1},
        {"heart_burst_count": True},
        {"heart_burst_count": 2.0},
        {"heart_burst_interval_sec": 0},
        {"heart_burst_interval_sec": -0.1},
        {"heart_burst_interval_sec": math.nan},
        {"target_lost_grace_sec": 0},
        {"target_lost_grace_sec": -1.0},
        {"target_lost_grace_sec": math.nan},
        {"target_upper_color": ""},
        {"target_upper_color": 1},
    ],
)
def test_rule_mode_settings_rejects_bad_new_fields(overrides):
    with pytest.raises(ValueError):
        RuleModeSettings(**overrides)
