"""정방향 카메라 모델 — 월드의 3D 객체 상자를 YOLO 형 픽셀 박스(xyxy)로 바꾼다 (MOCK-01).

축 정렬 3D 상자(지면 down=0 부터 높이까지)의 꼭짓점 8개를 interfaces.geometry 의
project_ned_to_pixel 로 투영하고, 그 최소·최대가 박스가 된다. 인식 계층(perception)과
같은 투영 함수를 쓰므로 부호 규약이 어긋날 수 없다.

비스듬히 내려다보면 박스 아래 변은 정확한 지면 접점이 아니다 — 상자 앞쪽 아래 모서리가
화면에서 가장 낮게 찍히므로, 박스 하단 중앙을 지면으로 되돌리면 객체 중심보다 카메라 쪽으로
조금 당겨진 위치가 나온다. 실제 YOLO 박스도 같은 성질을 가지므로 일부러 보정하지 않고,
허용 오차는 01-10 정확도 테스트가 정한다.
"""

from interfaces.flight_control import Telemetry
from interfaces.geometry import CameraIntrinsics, project_ned_to_pixel

MIN_VISIBLE_AREA_RATIO = 0.5  # 절반 이상 잘린 객체는 실제 탐지기도 대개 놓친다
MIN_BOX_SIZE_PX = 4.0  # 이보다 작은 박스는 탐지기 해상도 한계 아래로 본다


def project_object_box(
    north_m: float,
    east_m: float,
    size_m: tuple[float, float, float],
    camera: CameraIntrinsics,
    telemetry: Telemetry,
) -> tuple[float, float, float, float] | None:
    """객체 상자의 픽셀 박스 (x1, y1, x2, y2). 카메라 뒤·화면 밖·대부분 잘림이면 None."""
    corners_ned = _get_box_corners_ned(north_m, east_m, size_m)
    pixels = [project_ned_to_pixel(corner, camera, telemetry) for corner in corners_ned]
    if any(pixel is None for pixel in pixels):
        return None

    raw_x1 = min(u for u, _v in pixels)
    raw_x2 = max(u for u, _v in pixels)
    raw_y1 = min(v for _u, v in pixels)
    raw_y2 = max(v for _u, v in pixels)
    raw_area = (raw_x2 - raw_x1) * (raw_y2 - raw_y1)

    x1 = _clamp(raw_x1, 0.0, camera.width_px)
    x2 = _clamp(raw_x2, 0.0, camera.width_px)
    y1 = _clamp(raw_y1, 0.0, camera.height_px)
    y2 = _clamp(raw_y2, 0.0, camera.height_px)
    clipped_area = (x2 - x1) * (y2 - y1)

    is_too_small = (x2 - x1) < MIN_BOX_SIZE_PX or (y2 - y1) < MIN_BOX_SIZE_PX
    if raw_area <= 0 or is_too_small:
        return None
    if clipped_area / raw_area < MIN_VISIBLE_AREA_RATIO:
        return None

    return (round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1))


def _get_box_corners_ned(
    north_m: float, east_m: float, size_m: tuple[float, float, float]
) -> list[tuple[float, float, float]]:
    # 길이는 north, 폭은 east 축 (heading 무시 — mocks/scenario.py 참고). 지면 down=0, 위로는 음수
    length_m, width_m, height_m = size_m
    half_length_m = length_m / 2.0
    half_width_m = width_m / 2.0

    return [
        (north_m + sign_n * half_length_m, east_m + sign_e * half_width_m, down_m)
        for sign_n in (-1.0, 1.0)
        for sign_e in (-1.0, 1.0)
        for down_m in (0.0, -height_m)
    ]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
