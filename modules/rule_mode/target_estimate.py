"""대상 지면 위치 추정 — approach 가 YOLO 박스 하나와 촬영 순간 텔레메트리로 대상 위치를 구한다 (D-08, D-09).

주 추정은 지면 투영이다: 박스 밑변 중앙(발)을 지나는 시선을 평면 down = 드론 down_m + height_above_ground_m 과
교차시킨다. 평면을 홈 지면(down 0)이 아니라 AGL 로 잡는 이유는 언덕 위에서도 발 아래 지면을 맞히기 위해서다 (D-09).
AGL 이 없으면(None) 홈 지면 down 0 으로 대체한다.

보조 추정은 박스 높이 핀홀이다: 사람 키 1.7 m 와 박스 높이로 거리를 잡는다. approach 가 두 추정을 로그에 함께 남겨
나중에 비교하는 용도(D-08)이고, 밑변 시선이 수평선 위라 지면 투영이 불가능할 때의 대체이기도 하다.

rule_mode 는 다른 모듈의 interface 패키지만 import 할 수 있어 perception/projection.py 를 쓰지 못한다.
그래서 pixel_to_ray_ned 로 시선을 얻고 평면 교차는 여기서 직접 계산한다 (projection.py 와 같은 수식).
"""

import math
from dataclasses import dataclass

from modules.flight_control.interface import Telemetry
from modules.perception.interface import CameraIntrinsics, pixel_to_ray_ned
from modules.yolo_detection.interface import Detection

PERSON_HEIGHT_M = 1.7
HORIZON_EPSILON = 1e-6  # 시선 down 성분이 이보다 작으면 수평선 위 / 수평으로 본다
MIN_BOX_HEIGHT_PX = 1.0

METHOD_GROUND_PROJECTION = "ground_projection"
METHOD_BOX_HEIGHT = "box_height"

GroundPoint = tuple[float, float]  # (north_m, east_m)


def get_ground_plane_down_m(telemetry: Telemetry) -> float:
    """대상이 서 있다고 보는 평면의 NED down [m]. AGL 이 없으면 홈 지면 0.0."""
    if telemetry.height_above_ground_m is None:
        return 0.0

    return telemetry.down_m + telemetry.height_above_ground_m


def estimate_ground_position(
    detection: Detection, frame_telemetry: Telemetry, camera: CameraIntrinsics
) -> GroundPoint | None:
    """박스 밑변 중앙 시선과 AGL 평면의 교점. 시선이 수평선 위거나 평면 뒤쪽이면 None."""
    x1, _, x2, y2 = detection.xyxy
    ray_north, ray_east, ray_down = pixel_to_ray_ned((x1 + x2) / 2.0, y2, camera, frame_telemetry)
    if ray_down <= HORIZON_EPSILON:
        return None

    distance_m = (get_ground_plane_down_m(frame_telemetry) - frame_telemetry.down_m) / ray_down
    if distance_m <= 0.0:
        return None

    return (
        frame_telemetry.north_m + distance_m * ray_north,
        frame_telemetry.east_m + distance_m * ray_east,
    )


def estimate_by_box_height(
    detection: Detection,
    frame_telemetry: Telemetry,
    camera: CameraIntrinsics,
    person_height_m: float = PERSON_HEIGHT_M,
) -> GroundPoint | None:
    """박스 높이 핀홀 거리로 박스 중앙 시선을 따라간 점. 비스듬한 단축은 일부러 보정하지 않는다."""
    x1, y1, x2, y2 = detection.xyxy
    box_height_px = y2 - y1
    if box_height_px < MIN_BOX_HEIGHT_PX:
        return None

    range_m = camera.fy_px * person_height_m / box_height_px
    ray_north, ray_east, _ = pixel_to_ray_ned((x1 + x2) / 2.0, (y1 + y2) / 2.0, camera, frame_telemetry)

    return (
        frame_telemetry.north_m + range_m * ray_north,
        frame_telemetry.east_m + range_m * ray_east,
    )


@dataclass(frozen=True, slots=True)
class TargetEstimate:
    north_m: float
    east_m: float
    method: str  # METHOD_GROUND_PROJECTION | METHOD_BOX_HEIGHT
    ground_projection: GroundPoint | None
    box_height: GroundPoint | None

    @property
    def gap_m(self) -> float | None:
        """두 추정 사이의 수평 거리. 하나라도 없으면 None."""
        if self.ground_projection is None or self.box_height is None:
            return None

        return math.hypot(
            self.ground_projection[0] - self.box_height[0],
            self.ground_projection[1] - self.box_height[1],
        )

    def describe(self) -> str:
        """approach 로그용 한 줄 — 두 추정을 함께 남겨 나중에 비교한다."""
        gap_m = self.gap_m
        gap_text = "none" if gap_m is None else f"{gap_m:.1f} m"

        return (
            f"used={self.method} ground_projection={_format_point(self.ground_projection)} "
            f"box_height={_format_point(self.box_height)} gap={gap_text}"
        )


def estimate_target_position(
    detection: Detection,
    frame_telemetry: Telemetry,
    camera: CameraIntrinsics,
    person_height_m: float = PERSON_HEIGHT_M,
) -> TargetEstimate | None:
    """지면 투영을 쓰고, 불가능하면(수평선 위) 박스 높이로 대체한다. 둘 다 안 되면 None."""
    ground_projection = estimate_ground_position(detection, frame_telemetry, camera)
    box_height = estimate_by_box_height(detection, frame_telemetry, camera, person_height_m)

    if ground_projection is not None:
        method, used = METHOD_GROUND_PROJECTION, ground_projection
    elif box_height is not None:
        method, used = METHOD_BOX_HEIGHT, box_height
    else:
        return None

    return TargetEstimate(
        north_m=used[0],
        east_m=used[1],
        method=method,
        ground_projection=ground_projection,
        box_height=box_height,
    )


def _format_point(point: GroundPoint | None) -> str:
    if point is None:
        return "none"

    return f"({point[0]:.1f}, {point[1]:.1f})"
