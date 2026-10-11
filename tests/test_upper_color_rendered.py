"""classify_upper_color 를 브라우저 FPV 렌더 색으로 검증 — 작은 박스의 빨간 셔츠는 red, 다른 셔츠는 red 가 아니다.

three.js 렌더(MeshStandardMaterial + 조명 + 안개 + sRGB 출력)를 근사해 셔츠 픽셀 색을 만들고,
FPV 프레임처럼 JPEG 로 한 번 압축한 합성 보행자 이미지로 판별한다. 색은 tools/mission_demo/humanoid.js SHIRT_COLORS.
"""

import itertools

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from modules.yolo_detection.upper_color import classify_upper_color  # noqa: E402

RED_SHIRT = 0xC83C32
DECOY_SHIRTS = {
    "blue": 0x286BD1,
    "green": 0x2F8F4E,
    "rust": 0xB5562A,
    "purple": 0x7A3F9A,
    "navy": 0x1C2A44,
    "pink": 0xD86A8C,
    "teal": 0x1F7A7A,
    "dark_gray": 0x3A3A3A,
    "gray": 0x808080,
}
FOG_COLOR = 0x65706B  # world.js URBAN.fog

SIDEWALK_BGR = (148, 167, 172)  # 실측 FPV 보도 색
HAIR_BGR = (40, 60, 90)
PANTS_BGR = (48, 42, 36)
JPEG_QUALITY = 80
IMAGE_SIZE_PX = 120
PERSON_LEFT_PX, PERSON_TOP_PX = 50, 30

# 렌더 조건: 재질 색을 sRGB 로 볼지 선형으로 볼지 × 조명 세기(그림자~직사광) × 안개 섞임
COLOR_MODES = ("srgb", "linear")
LIGHT_LEVELS = (0.35, 0.6, 1.0, 1.4)
FOG_RATIOS = (0.0, 0.15)
BOX_HEIGHTS_PX = (18, 24, 40, 60)
RENDER_CASES = list(itertools.product(COLOR_MODES, LIGHT_LEVELS, FOG_RATIOS, BOX_HEIGHTS_PX))


def _decode_srgb(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _encode_srgb(channel: float) -> float:
    channel = min(1.0, max(0.0, channel))
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def _get_rgb(hex_color: int) -> tuple[float, float, float]:
    return ((hex_color >> 16 & 255) / 255, (hex_color >> 8 & 255) / 255, (hex_color & 255) / 255)


def _render_bgr(hex_color: int, mode: str, light: float, fog_ratio: float) -> tuple[int, int, int]:
    """재질 색 → 선형 공간에서 조명 곱·안개 섞기 → sRGB 출력 8bit BGR."""
    material = _get_rgb(hex_color)
    linear = [_decode_srgb(c) for c in material] if mode == "srgb" else list(material)
    fog = [_decode_srgb(c) for c in _get_rgb(FOG_COLOR)]

    lit = [c * light * (1 - fog_ratio) + f * fog_ratio for c, f in zip(linear, fog, strict=True)]
    red, green, blue = (round(_encode_srgb(c) * 255) for c in lit)
    return (blue, green, red)


def _make_rendered_person(shirt_bgr: tuple[int, int, int], box_height: int):
    """보도 위 보행자(머리·셔츠·바지)를 그리고 JPEG 로 왕복한 이미지와 YOLO 같은 여유 1 px 박스."""
    box_width = max(6, round(box_height * 0.55))
    head_bottom = PERSON_TOP_PX + round(box_height * 0.15)
    shirt_bottom = PERSON_TOP_PX + round(box_height * 0.55)
    left, right = PERSON_LEFT_PX, PERSON_LEFT_PX + box_width

    image = np.full((IMAGE_SIZE_PX, IMAGE_SIZE_PX, 3), SIDEWALK_BGR, np.uint8)
    image[PERSON_TOP_PX:head_bottom, left + box_width // 4 : right - box_width // 4] = HAIR_BGR
    image[head_bottom:shirt_bottom, left:right] = shirt_bgr
    image[shirt_bottom : PERSON_TOP_PX + box_height, left + box_width // 5 : right - box_width // 5] = PANTS_BGR

    _, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    xyxy = (left - 1.0, PERSON_TOP_PX - 1.0, right + 1.0, PERSON_TOP_PX + box_height + 1.0)
    return cv2.imdecode(jpeg, cv2.IMREAD_COLOR), xyxy


@pytest.mark.parametrize("mode, light, fog_ratio, box_height", RENDER_CASES)
def test_rendered_red_shirt_is_red(mode, light, fog_ratio, box_height) -> None:
    image, xyxy = _make_rendered_person(_render_bgr(RED_SHIRT, mode, light, fog_ratio), box_height)

    assert classify_upper_color(image, xyxy) == "red"


@pytest.mark.parametrize("decoy", sorted(DECOY_SHIRTS))
@pytest.mark.parametrize("mode, light, fog_ratio, box_height", RENDER_CASES)
def test_rendered_decoy_shirt_is_not_red(decoy, mode, light, fog_ratio, box_height) -> None:
    shirt_bgr = _render_bgr(DECOY_SHIRTS[decoy], mode, light, fog_ratio)
    image, xyxy = _make_rendered_person(shirt_bgr, box_height)

    assert classify_upper_color(image, xyxy) != "red"


@pytest.mark.parametrize("decoy", ["blue", "green", "gray"])
@pytest.mark.parametrize("box_height", BOX_HEIGHTS_PX)
def test_lit_common_decoy_is_decided_other(decoy, box_height) -> None:
    # None 이면 observe 가 끝나지 않는다 — 밝은 조명의 파랑·초록·회색은 판단 가능한 "other" 여야 한다
    image, xyxy = _make_rendered_person(_render_bgr(DECOY_SHIRTS[decoy], "srgb", 1.0, 0.0), box_height)

    assert classify_upper_color(image, xyxy) == "other"


def test_box_just_below_min_height_is_unknown() -> None:
    image, (x1, y1, x2, _) = _make_rendered_person(_render_bgr(RED_SHIRT, "srgb", 1.0, 0.0), 18)

    assert classify_upper_color(image, (x1, y1, x2, y1 + 15.0)) is None


def test_measured_fpv_red_shirt_pixel_is_red() -> None:
    # 실측 FPV 프레임(m00690)의 빨간 셔츠 중심 픽셀 HSV 3/176/243 을 BGR 로 되돌린 단색 패치
    shirt_bgr = tuple(int(c) for c in cv2.cvtColor(np.uint8([[[3, 176, 243]]]), cv2.COLOR_HSV2BGR)[0, 0])
    image, xyxy = _make_rendered_person(shirt_bgr, 18)

    assert classify_upper_color(image, xyxy) == "red"
