"""프레임 단위 상체 색 판별 — person 박스의 상체 영역 HSV 로 red / other / None(판단 불가)을 낸다 (D-01, D-03).

한 프레임 판정만 한다. 프레임 잡음을 거르는 track 단위 다수결은 perception(TrackAggregator)이 한다 (D-04).
cv2/numpy 는 함수 안에서만 import 한다 — yolo 그룹이 없는 base 환경에서도 이 모듈을 import 할 수 있어야 한다(test_yolo_isolation).
"""

from typing import Any

from modules.yolo_detection.interface.detection import UPPER_COLORS

RED, OTHER = UPPER_COLORS

# 상체 영역 (D-03): 박스 높이 위에서 20~50%, 가로 가운데 60% — ASSUMED, 실측 후 조정
UPPER_TOP_RATIO = 0.2
UPPER_BOTTOM_RATIO = 0.5
UPPER_SIDE_MARGIN_RATIO = 0.2
MIN_REGION_PIXELS = 64  # 이보다 작은 영역은 판단하지 않는다

# HSV 기준 (OpenCV: H 0~180, S·V 0~255) — ASSUMED, 실측 후 조정
MIN_VALUE = 50  # 이보다 어두운 픽셀은 판단 불가 픽셀
MIN_USABLE_PIXEL_RATIO = 0.3  # 판단 가능 픽셀이 이보다 적으면 명도 부족 → None
# 채도 부족(흰·회색 옷)은 판단 가능 픽셀로 세어 "other" 로 보낸다 — unknown 으로 두면 observe 가 끝나지 않는다
MIN_RED_SATURATION = 80
RED_HUE_LOW_MAX = 10  # 빨강은 Hue 양끝을 감싼다: H <= 10 또는 H >= 170
RED_HUE_HIGH_MIN = 170
MIN_RED_PIXEL_RATIO = 0.4  # 판단 가능 픽셀 중 빨강 비율이 이 이상이면 red


def classify_upper_color(image_bgr: Any, xyxy: tuple[float, float, float, float]) -> str | None:
    """BGR 이미지에서 박스 상체 영역의 색. 판단할 수 없으면 None (예외를 내지 않는다)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        # base 환경(yolo 그룹 없음)에서는 이미지를 해석할 수 없다
        return None

    image = np.asarray(image_bgr)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        return None

    height, width = image.shape[:2]
    region = _get_upper_region(xyxy, width, height)
    if region is None:
        return None

    x1, y1, x2, y2 = region
    hsv = cv2.cvtColor(np.ascontiguousarray(image[y1:y2, x1:x2]), cv2.COLOR_BGR2HSV)
    hue, saturation, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    usable = value >= MIN_VALUE
    usable_count = int(usable.sum())
    if usable_count < MIN_USABLE_PIXEL_RATIO * usable.size:
        return None

    is_red_hue = (hue <= RED_HUE_LOW_MAX) | (hue >= RED_HUE_HIGH_MIN)
    red_count = int((usable & (saturation >= MIN_RED_SATURATION) & is_red_hue).sum())
    return RED if red_count >= MIN_RED_PIXEL_RATIO * usable_count else OTHER


def _get_upper_region(
    xyxy: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """박스 상체 영역을 이미지 경계로 자른 정수 픽셀 (x1, y1, x2, y2). 비었거나 너무 작으면 None."""
    box_x1, box_y1, box_x2, box_y2 = xyxy
    box_width = box_x2 - box_x1
    box_height = box_y2 - box_y1

    x1 = max(0, int(round(box_x1 + UPPER_SIDE_MARGIN_RATIO * box_width)))
    x2 = min(width, int(round(box_x2 - UPPER_SIDE_MARGIN_RATIO * box_width)))
    y1 = max(0, int(round(box_y1 + UPPER_TOP_RATIO * box_height)))
    y2 = min(height, int(round(box_y1 + UPPER_BOTTOM_RATIO * box_height)))

    if x2 <= x1 or y2 <= y1 or (x2 - x1) * (y2 - y1) < MIN_REGION_PIXELS:
        return None
    return (x1, y1, x2, y2)
