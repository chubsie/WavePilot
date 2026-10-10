"""픽셀 → 지면 투영 (PERC-01).

v1 은 지면을 down = 0 인 평면으로 가정한다(평지). 시선 벡터 계산은 interfaces.geometry 의
pixel_to_ray_ned 를 그대로 쓴다 — mocks 의 월드 → 픽셀 정방향 모델과 같은 회전을 공유해야
부호 규약이 어긋나지 않는다. numpy 없이 순수 파이썬.
"""

from interfaces.flight_control import Telemetry
from interfaces.geometry import CameraIntrinsics, pixel_to_ray_ned

# 시선의 down 성분이 이보다 작으면 수평선 이상으로 보고 지면 교점을 만들지 않는다 (Pitfall 3)
HORIZON_EPSILON = 1e-6


def ground_anchor(xyxy: tuple[float, float, float, float]) -> tuple[float, float]:
    """박스의 지면 접점 픽셀 — 하단 중앙.

    박스 하단이 물체가 땅에 닿는 지점에 가깝다. 박스 중심을 쓰면 물체 높이만큼
    시선이 더 멀리 가서 물체 뒤쪽 지면과 만난다.
    """
    x1, _, x2, y2 = xyxy
    return ((x1 + x2) / 2.0, float(y2))


def pixel_to_ground(
    u: float,
    v: float,
    camera: CameraIntrinsics,
    telemetry: Telemetry,
    ground_down_m: float = 0.0,
) -> tuple[float, float] | None:
    """픽셀 (u, v) 시선과 평지(down = ground_down_m)의 교점 (north_m, east_m).

    수평선 이상을 보는 시선이거나 교점이 카메라 뒤쪽이면 예외 대신 None.
    """
    ray_north, ray_east, ray_down = pixel_to_ray_ned(u, v, camera, telemetry)
    if ray_down <= HORIZON_EPSILON:
        return None

    distance_along_ray = (ground_down_m - telemetry.down_m) / ray_down
    if distance_along_ray <= 0.0:
        return None

    return (
        telemetry.north_m + distance_along_ray * ray_north,
        telemetry.east_m + distance_along_ray * ray_east,
    )
