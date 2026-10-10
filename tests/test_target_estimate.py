"""RM-05 — 대상 위치 추정(AGL 평면 지면 투영, 박스 높이 핀홀) 정확도 검증."""

import math
from dataclasses import replace

import pytest

from modules.perception.interface import DEFAULT_CAMERA, project_ned_to_pixel
from modules.rule_mode.target_estimate import (
    METHOD_BOX_HEIGHT,
    METHOD_GROUND_PROJECTION,
    PERSON_HEIGHT_M,
    estimate_by_box_height,
    estimate_ground_position,
    estimate_target_position,
    get_ground_plane_down_m,
)
from modules.yolo_detection.interface import Detection

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
BOX_WIDTH_RATIO = 0.4

YAW_LIST = (0.0, 30.0, -45.0)
ROLL_LIST = (0.0, 4.0)
PITCH_LIST = (0.0, -3.0)


def _rotate_by_yaw(north_m: float, east_m: float, yaw_deg: float) -> tuple[float, float]:
    yaw = math.radians(yaw_deg)

    return (
        north_m * math.cos(yaw) - east_m * math.sin(yaw),
        north_m * math.sin(yaw) + east_m * math.cos(yaw),
    )


def _make_detection(telemetry, north_m, east_m, ground_down_m, height_m=PERSON_HEIGHT_M, camera=DEFAULT_CAMERA):
    """발·머리 점을 투영해 박스를 만든다. 화면 밖이면 None. 밑변 중앙이 곧 발 픽셀이다."""
    feet = project_ned_to_pixel((north_m, east_m, ground_down_m), camera, telemetry)
    head = project_ned_to_pixel((north_m, east_m, ground_down_m - height_m), camera, telemetry)
    if feet is None or head is None:
        return None

    for u, v in (feet, head):
        if not 0.0 <= u <= IMAGE_WIDTH or not 0.0 <= v <= IMAGE_HEIGHT:
            return None

    half_width = BOX_WIDTH_RATIO * (feet[1] - head[1]) / 2.0

    return Detection(0, "person", 0.9, (feet[0] - half_width, head[1], feet[0] + half_width, feet[1]), 7)


def test_ground_projection_recovers_flat_ground_target_across_attitude_grid(hover_telemetry):
    checked_count = 0

    for yaw in YAW_LIST:
        for roll in ROLL_LIST:
            for pitch in PITCH_LIST:
                for gimbal in (-55.0, -70.0):
                    telemetry = replace(
                        hover_telemetry, yaw_deg=yaw, roll_deg=roll, pitch_deg=pitch, gimbal_pitch_deg=gimbal
                    )
                    for north in (15.0, 30.0, 50.0):
                        for east in (-10.0, 0.0, 10.0):
                            north_m, east_m = _rotate_by_yaw(north, east, yaw)
                            detection = _make_detection(telemetry, north_m, east_m, 0.0)
                            if detection is None:
                                continue

                            estimate = estimate_ground_position(detection, telemetry, DEFAULT_CAMERA)

                            assert estimate is not None
                            assert estimate[0] == pytest.approx(north_m, abs=0.1)
                            assert estimate[1] == pytest.approx(east_m, abs=0.1)
                            checked_count += 1

    assert checked_count >= 30


def test_box_height_estimate_is_within_ten_percent_at_shallow_angle(hover_telemetry):
    checked_count = 0

    for yaw in YAW_LIST:
        for roll in ROLL_LIST:
            for pitch in PITCH_LIST:
                telemetry = replace(
                    hover_telemetry,
                    down_m=-10.0,
                    gimbal_pitch_deg=-15.0,
                    yaw_deg=yaw,
                    roll_deg=roll,
                    pitch_deg=pitch,
                )
                for north in (30.0, 40.0, 50.0):
                    for east in (-10.0, 0.0, 10.0):
                        north_m, east_m = _rotate_by_yaw(north, east, yaw)
                        detection = _make_detection(telemetry, north_m, east_m, 0.0)
                        if detection is None:
                            continue

                        estimate = estimate_by_box_height(detection, telemetry, DEFAULT_CAMERA)

                        assert estimate is not None
                        horizontal_m = math.hypot(north_m, east_m)
                        assert math.hypot(estimate[0] - north_m, estimate[1] - east_m) <= 0.1 * horizontal_m
                        checked_count += 1

    assert checked_count > 0


def test_horizon_target_falls_back_to_box_height(hover_telemetry):
    # 드론 down -2, 수평 시선: 30 m 앞 능선(down -5)은 드론보다 높아 발 시선이 수평선 위다
    telemetry = replace(hover_telemetry, down_m=-2.0, gimbal_pitch_deg=0.0)
    detection = _make_detection(telemetry, 30.0, 0.0, -5.0)

    assert detection is not None
    assert detection.xyxy[3] < DEFAULT_CAMERA.cy_px
    assert estimate_ground_position(detection, telemetry, DEFAULT_CAMERA) is None

    estimate = estimate_target_position(detection, telemetry, DEFAULT_CAMERA)

    assert estimate is not None
    assert estimate.method == METHOD_BOX_HEIGHT
    assert estimate.ground_projection is None
    assert math.hypot(estimate.north_m - 30.0, estimate.east_m) <= 3.0


def test_agl_plane_recovers_hill_target_and_home_plane_misses(hover_telemetry):
    # 드론 down -20, AGL 15 -> 지면 down -5 (언덕)
    hill_telemetry = replace(hover_telemetry, down_m=-20.0, gimbal_pitch_deg=-45.0, height_above_ground_m=15.0)
    detection = _make_detection(hill_telemetry, 20.0, 0.0, -5.0)

    assert detection is not None
    assert get_ground_plane_down_m(hill_telemetry) == pytest.approx(-5.0)

    estimate = estimate_ground_position(detection, hill_telemetry, DEFAULT_CAMERA)

    assert estimate is not None
    assert estimate[0] == pytest.approx(20.0, abs=0.1)
    assert estimate[1] == pytest.approx(0.0, abs=0.1)

    no_agl_telemetry = replace(hill_telemetry, height_above_ground_m=None)
    fallback_estimate = estimate_ground_position(detection, no_agl_telemetry, DEFAULT_CAMERA)

    assert get_ground_plane_down_m(no_agl_telemetry) == 0.0
    assert fallback_estimate is not None
    assert math.hypot(fallback_estimate[0] - 20.0, fallback_estimate[1]) > 1.0


def test_describe_lists_both_estimates_and_degenerate_box_has_no_box_height(hover_telemetry):
    detection = _make_detection(hover_telemetry, 40.0, 3.0, 0.0)
    assert detection is not None

    estimate = estimate_target_position(detection, hover_telemetry, DEFAULT_CAMERA)

    assert estimate is not None
    assert estimate.method == METHOD_GROUND_PROJECTION
    assert estimate.gap_m is not None
    description = estimate.describe()
    assert "used=ground_projection" in description
    assert "box_height=" in description
    assert "gap=" in description

    x1, _, x2, y2 = detection.xyxy
    flat_detection = replace(detection, xyxy=(x1, y2, x2, y2))
    flat_estimate = estimate_target_position(flat_detection, hover_telemetry, DEFAULT_CAMERA)

    assert flat_estimate is not None
    assert flat_estimate.box_height is None
    assert flat_estimate.gap_m is None
    assert "box_height=none" in flat_estimate.describe()
