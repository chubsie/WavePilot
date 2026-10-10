"""classify_upper_color 검증 — 합성 BGR 이미지로 상체 영역(D-03)과 HSV 판별 규칙을 고정한다."""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from modules.yolo_detection.interface.detection import UPPER_COLORS  # noqa: E402
from modules.yolo_detection.upper_color import classify_upper_color  # noqa: E402

RED_BGR = (0, 0, 220)
BLUE_BGR = (220, 0, 0)
GRAY_BGR = (128, 128, 128)
DARK_BGR = (10, 10, 10)
PURPLE_RED_BGR = (60, 0, 220)  # OpenCV Hue 약 175 — 빨강 양끝 감싸기 쪽

IMAGE_HEIGHT, IMAGE_WIDTH = 300, 200
BOX_XYXY = (50.0, 50.0, 150.0, 250.0)  # 높이 200 x 폭 100


def _make_person_image(upper_bgr: tuple[int, int, int], rest_bgr: tuple[int, int, int]):
    """BOX_XYXY 박스의 상체 영역(위 20~50%, 가운데 60%)만 upper_bgr, 나머지 전부 rest_bgr."""
    image = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH, 3), np.uint8)
    image[:, :] = rest_bgr
    image[90:150, 70:130] = upper_bgr
    return image


def _make_filled_image(bgr: tuple[int, int, int]):
    return _make_person_image(bgr, bgr)


@pytest.mark.parametrize(
    "bgr, expected",
    [
        (RED_BGR, "red"),
        (BLUE_BGR, "other"),
        (GRAY_BGR, "other"),
        (DARK_BGR, None),
        (PURPLE_RED_BGR, "red"),
    ],
)
def test_filled_box_color(bgr, expected) -> None:
    result = classify_upper_color(_make_filled_image(bgr), BOX_XYXY)

    assert result == expected
    assert result is None or result in UPPER_COLORS


def test_only_upper_region_decides_red() -> None:
    assert classify_upper_color(_make_person_image(RED_BGR, BLUE_BGR), BOX_XYXY) == "red"


def test_only_upper_region_decides_other() -> None:
    assert classify_upper_color(_make_person_image(BLUE_BGR, RED_BGR), BOX_XYXY) == "other"


def test_too_small_box_is_unknown() -> None:
    assert classify_upper_color(_make_filled_image(RED_BGR), (60.0, 60.0, 68.0, 68.0)) is None


def test_box_partly_outside_image_is_clipped() -> None:
    image = np.zeros((IMAGE_HEIGHT, IMAGE_WIDTH, 3), np.uint8)
    image[:, :] = RED_BGR

    assert classify_upper_color(image, (-50.0, -40.0, 150.0, 260.0)) == "red"


def test_box_fully_outside_image_is_unknown() -> None:
    image = _make_filled_image(RED_BGR)

    assert classify_upper_color(image, (500.0, 500.0, 600.0, 700.0)) is None


@pytest.mark.parametrize("image", [object(), None, "image", np.zeros((300, 200), np.uint8)])
def test_non_image_input_is_unknown(image) -> None:
    assert classify_upper_color(image, BOX_XYXY) is None
