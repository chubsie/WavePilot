"""modules.perception.interface.geometry 부호 규약 고정 테스트.

부호 버그는 알려진 값과 왕복(픽셀 → 광선 → 점 → 픽셀)에서만 잡힌다.
"""

import dataclasses
import math

import pytest

from modules.flight_control.interface.flight_control import Telemetry
from modules.perception.interface.geometry import (
    DEFAULT_CAMERA,
    CameraIntrinsics,
    pixel_to_ray_ned,
    project_ned_to_pixel,
    rotation_body_to_ned,
)

TOLERANCE = 1e-9
PIXEL_TOLERANCE = 1e-6


def _assert_vector_close(actual, expected, tolerance: float = TOLERANCE) -> None:
    for actual_value, expected_value in zip(actual, expected, strict=True):
        assert actual_value == pytest.approx(expected_value, abs=tolerance)


def _point_along(telemetry: Telemetry, ray, distance_m: float) -> tuple[float, float, float]:
    origin = (telemetry.north_m, telemetry.east_m, telemetry.down_m)
    return tuple(origin[i] + distance_m * ray[i] for i in range(3))


def test_default_camera_from_90_degree_hfov() -> None:
    assert DEFAULT_CAMERA.width_px == 1280
    assert DEFAULT_CAMERA.height_px == 720
    assert DEFAULT_CAMERA.fx_px == pytest.approx(640.0, abs=TOLERANCE)
    assert DEFAULT_CAMERA.fy_px == pytest.approx(640.0, abs=TOLERANCE)
    assert DEFAULT_CAMERA.cx_px == 640.0
    assert DEFAULT_CAMERA.cy_px == 360.0


def test_from_hfov_uses_half_width_over_tan() -> None:
    camera = CameraIntrinsics.from_hfov(400, 240, 60.0)

    assert camera.fx_px == pytest.approx(200.0 / math.tan(math.radians(30.0)), abs=TOLERANCE)


def test_zero_euler_is_identity() -> None:
    rotation = rotation_body_to_ned(0.0, 0.0, 0.0)

    for row_index, row in enumerate(rotation):
        expected_row = tuple(1.0 if column == row_index else 0.0 for column in range(3))
        _assert_vector_close(row, expected_row)


def test_yaw_90_points_forward_east() -> None:
    rotation = rotation_body_to_ned(0.0, 0.0, 90.0)

    forward_in_ned = tuple(rotation[row][0] for row in range(3))

    _assert_vector_close(forward_in_ned, (0.0, 1.0, 0.0))


def test_gimbal_straight_down_center_ray(hover_telemetry: Telemetry) -> None:
    telemetry = dataclasses.replace(hover_telemetry, gimbal_pitch_deg=-90.0)

    ray = pixel_to_ray_ned(DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, telemetry)

    _assert_vector_close(ray, (0.0, 0.0, 1.0))


def test_gimbal_minus_55_center_ray(hover_telemetry: Telemetry) -> None:
    ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, hover_telemetry
    )

    angle_rad = math.radians(55.0)
    _assert_vector_close(ray, (math.cos(angle_rad), 0.0, math.sin(angle_rad)))


def test_pixel_right_of_center_points_east(hover_telemetry: Telemetry) -> None:
    ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px + 200.0, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, hover_telemetry
    )

    assert ray[1] > 0.0


def test_pixel_below_center_is_steeper(hover_telemetry: Telemetry) -> None:
    center_ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, hover_telemetry
    )
    lower_ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px + 150.0, DEFAULT_CAMERA, hover_telemetry
    )

    assert lower_ray[2] > center_ray[2]


def test_project_point_on_center_ray(hover_telemetry: Telemetry) -> None:
    center_ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, hover_telemetry
    )
    point = _point_along(hover_telemetry, center_ray, 28.0)

    pixel = project_ned_to_pixel(point, DEFAULT_CAMERA, hover_telemetry)

    assert pixel is not None
    _assert_vector_close(pixel, (DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px), PIXEL_TOLERANCE)


def test_project_point_behind_camera_is_none(hover_telemetry: Telemetry) -> None:
    center_ray = pixel_to_ray_ned(
        DEFAULT_CAMERA.cx_px, DEFAULT_CAMERA.cy_px, DEFAULT_CAMERA, hover_telemetry
    )
    point_behind = _point_along(hover_telemetry, center_ray, -10.0)

    assert project_ned_to_pixel(point_behind, DEFAULT_CAMERA, hover_telemetry) is None


def test_pixel_ray_point_pixel_round_trip_on_5x5_grid(hover_telemetry: Telemetry) -> None:
    telemetry = dataclasses.replace(
        hover_telemetry,
        north_m=12.0,
        east_m=-7.0,
        roll_deg=5.0,
        pitch_deg=-3.0,
        yaw_deg=30.0,
        gimbal_pitch_deg=-55.0,
        gimbal_yaw_deg=10.0,
    )
    grid_steps = 5

    for row in range(grid_steps):
        for column in range(grid_steps):
            u = DEFAULT_CAMERA.width_px * column / (grid_steps - 1)
            v = DEFAULT_CAMERA.height_px * row / (grid_steps - 1)
            ray = pixel_to_ray_ned(u, v, DEFAULT_CAMERA, telemetry)
            point = _point_along(telemetry, ray, 50.0)

            pixel = project_ned_to_pixel(point, DEFAULT_CAMERA, telemetry)

            assert pixel is not None
            _assert_vector_close(pixel, (u, v), PIXEL_TOLERANCE)
