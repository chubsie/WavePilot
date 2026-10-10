"""카메라 기하 — 모든 좌표·부호 규약을 이 파일 한 곳에 고정한다.

규약 (SKELETON.md "좌표계"):
- 월드: 로컬 NED [m] (north, east, down). 지면은 down = 0, 고도 = -down.
- 기체(body): FRD — x 전방, y 우측, z 아래.
- 자세: Euler ZYX(yaw → pitch → roll) [deg]. +roll 우측 하강, +pitch 기수 상승, +yaw 북 기준 시계방향.
- 짐벌: 기체 기준 [deg]. gimbal_yaw_deg 는 body z 축 회전(정면 0), gimbal_pitch_deg 는 그 다음 body y 축 회전이며
  아래를 보면 음수(-90 = 직하).
- 카메라: OpenCV — x 오른쪽, y 아래, z 광축(전방). 짐벌 0/0 이면 광축 = body x.

perception(픽셀 → 지면)과 simulation(월드 → 픽셀)가 같은 함수를 써야 부호가 어긋나지 않는다.
그래서 구현체끼리 import 하지 않도록 공용 수학을 perception/interface 에 둔다. numpy 없이 순수 파이썬(D-07).
"""

import math
from dataclasses import dataclass

from modules.flight_control.interface.flight_control import Telemetry

DEFAULT_HFOV_DEG = 90.0
MIN_DEPTH_M = 0.1  # 이보다 가까우면(또는 뒤면) 투영이 발산하므로 화면 밖으로 본다

type Vector3 = tuple[float, float, float]
type Matrix3 = tuple[Vector3, Vector3, Vector3]

# 카메라 축 → body 축: body x = camera z(광축), body y = camera x(오른쪽), body z = camera y(아래)
CAMERA_TO_BODY: Matrix3 = (
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)


@dataclass(frozen=True, slots=True)
class CameraIntrinsics:
    width_px: int
    height_px: int
    fx_px: float
    fy_px: float
    cx_px: float
    cy_px: float

    @classmethod
    def from_hfov(cls, width_px: int, height_px: int, hfov_deg: float) -> "CameraIntrinsics":
        # 정사각 픽셀 가정: fy = fx, 주점은 영상 중심
        fx_px = (width_px / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
        return cls(
            width_px=width_px,
            height_px=height_px,
            fx_px=fx_px,
            fy_px=fx_px,
            cx_px=width_px / 2.0,
            cy_px=height_px / 2.0,
        )


DEFAULT_CAMERA = CameraIntrinsics.from_hfov(1280, 720, DEFAULT_HFOV_DEG)


def rotation_body_to_ned(roll_deg: float, pitch_deg: float, yaw_deg: float) -> Matrix3:
    """body 벡터를 NED 로 돌리는 행렬. ZYX = Rz(yaw) · Ry(pitch) · Rx(roll)."""
    roll, pitch, yaw = math.radians(roll_deg), math.radians(pitch_deg), math.radians(yaw_deg)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)

    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def rotation_camera_to_ned(telemetry: Telemetry) -> Matrix3:
    """카메라 벡터 → NED. R_body · R_gimbal · CAMERA_TO_BODY."""
    body_to_ned = rotation_body_to_ned(telemetry.roll_deg, telemetry.pitch_deg, telemetry.yaw_deg)
    # 짐벌은 기체 위에서 yaw(body z) 다음 pitch(body y) — 기체 자세 회전과 같은 Z→Y 순서
    gimbal_to_body = rotation_body_to_ned(0.0, telemetry.gimbal_pitch_deg, telemetry.gimbal_yaw_deg)

    return _mat_mul(_mat_mul(body_to_ned, gimbal_to_body), CAMERA_TO_BODY)


def pixel_to_ray_ned(
    u: float, v: float, camera: CameraIntrinsics, telemetry: Telemetry
) -> Vector3:
    """픽셀 (u, v) 를 지나는 시선의 NED 단위 벡터."""
    ray_camera = ((u - camera.cx_px) / camera.fx_px, (v - camera.cy_px) / camera.fy_px, 1.0)

    ray_ned = _mat_vec(rotation_camera_to_ned(telemetry), ray_camera)

    length = math.sqrt(sum(component * component for component in ray_ned))
    return (ray_ned[0] / length, ray_ned[1] / length, ray_ned[2] / length)


def project_ned_to_pixel(
    point_ned: Vector3, camera: CameraIntrinsics, telemetry: Telemetry
) -> tuple[float, float] | None:
    """NED 점을 픽셀로 투영. 카메라 깊이가 MIN_DEPTH_M 이하(뒤쪽 포함)면 None."""
    drone_ned = (telemetry.north_m, telemetry.east_m, telemetry.down_m)
    relative_ned = tuple(point_ned[i] - drone_ned[i] for i in range(3))
    # 회전 행렬의 역은 전치
    point_camera = _mat_vec(_transpose(rotation_camera_to_ned(telemetry)), relative_ned)

    depth_m = point_camera[2]
    if depth_m <= MIN_DEPTH_M:
        return None

    u = camera.fx_px * point_camera[0] / depth_m + camera.cx_px
    v = camera.fy_px * point_camera[1] / depth_m + camera.cy_px
    return (u, v)


# --- 3x3 행렬 도우미 (numpy 미사용) ---


def _mat_vec(matrix: Matrix3, vector) -> Vector3:
    return tuple(sum(matrix[row][k] * vector[k] for k in range(3)) for row in range(3))


def _transpose(matrix: Matrix3) -> Matrix3:
    return tuple(tuple(matrix[row][column] for row in range(3)) for column in range(3))


def _mat_mul(left: Matrix3, right: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )
