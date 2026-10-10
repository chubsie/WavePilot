"""PERC-01 — 픽셀 → 평지 월드 좌표 투영 검증."""

import math
from dataclasses import replace

import pytest

from interfaces.geometry import project_ned_to_pixel
from perception.projection import ground_anchor, pixel_to_ground

GROUND_NORTH_LIST = (10.0, 20.0, 35.0, 50.0, 60.0)
GROUND_EAST_LIST = (-20.0, -5.0, 5.0, 20.0)

ATTITUDE_COMBOS = [
    {"yaw_deg": yaw, "roll_deg": roll, "pitch_deg": pitch, "gimbal_pitch_deg": gimbal}
    for yaw in (0.0, 30.0, -45.0)
    for roll in (0.0, 4.0)
    for pitch in (0.0, -3.0)
    for gimbal in (-55.0, -70.0)
]


def test_ground_anchor_is_box_bottom_center():
    assert ground_anchor((100, 200, 140, 260)) == (120.0, 260.0)


def test_straight_down_center_maps_to_drone_position(hover_telemetry, default_camera):
    telemetry = replace(hover_telemetry, north_m=5.0, east_m=-3.0, gimbal_pitch_deg=-90.0)

    ground = pixel_to_ground(default_camera.cx_px, default_camera.cy_px, default_camera, telemetry)

    assert ground is not None
    assert ground[0] == pytest.approx(5.0, abs=1e-6)
    assert ground[1] == pytest.approx(-3.0, abs=1e-6)


def test_oblique_center_maps_ahead_by_altitude_over_tan(hover_telemetry, default_camera):
    ground = pixel_to_ground(default_camera.cx_px, default_camera.cy_px, default_camera, hover_telemetry)

    assert ground is not None
    assert ground[0] == pytest.approx(40.0 / math.tan(math.radians(55.0)), abs=1e-3)
    assert ground[1] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize("attitude", ATTITUDE_COMBOS)
def test_round_trip_with_forward_model(hover_telemetry, default_camera, attitude):
    telemetry = replace(hover_telemetry, **attitude)
    checked_count = 0

    for north_m in GROUND_NORTH_LIST:
        for east_m in GROUND_EAST_LIST:
            pixel = project_ned_to_pixel((north_m, east_m, 0.0), default_camera, telemetry)
            if pixel is None:
                continue

            ground = pixel_to_ground(pixel[0], pixel[1], default_camera, telemetry)

            assert ground is not None
            assert ground[0] == pytest.approx(north_m, abs=1e-6)
            assert ground[1] == pytest.approx(east_m, abs=1e-6)
            checked_count += 1

    assert checked_count >= 1


def test_round_trip_covers_at_least_20_points():
    assert len(GROUND_NORTH_LIST) * len(GROUND_EAST_LIST) >= 20
    assert len(ATTITUDE_COMBOS) >= 4


def test_ray_above_horizon_returns_none(hover_telemetry, default_camera):
    telemetry = replace(hover_telemetry, gimbal_pitch_deg=-5.0)

    assert pixel_to_ground(default_camera.cx_px, 0.0, default_camera, telemetry) is None


def test_ground_above_drone_returns_none(hover_telemetry, default_camera):
    # 지면이 기체보다 위에 있으면 아래 방향 시선은 지면과 만나지 않는다
    ground = pixel_to_ground(
        default_camera.cx_px, default_camera.cy_px, default_camera, hover_telemetry, ground_down_m=-50.0
    )

    assert ground is None
