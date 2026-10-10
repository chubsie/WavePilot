"""JPEG 바이트 스트림 -> YoloDetectionSource (SIM-01, D-12).

image_source.py 가 녹화 디렉터리·비디오를 `(timestamp_sec, BGR ndarray)` 로 내듯이, 이 모듈은 브라우저 시뮬레이터가
보낸 `(timestamp_sec, jpeg_bytes)` 라이브 스트림을 디코드해 YoloDetectionSource 에 넣는다.

디코드를 modules/simulation/browser/ 가 아니라 여기에 두는 이유: cv2·numpy 는 yolo 어댑터(modules/yolo_detection/) 밖에서
import 할 수 없고(tests/test_yolo_isolation.py), simulation 은 yolo_detection 의 interface 만 import 할 수 있다
(tests/test_import_boundaries.py). 그래서 simulation 쪽은 JPEG 바이트만 흘리고, 조립은 modules/drone/ 컴포지션 루트가 한다.
cv2·numpy·torch 는 함수 안에서만 지연 import 한다 — base 환경에서도 이 모듈을 import 할 수 있다 (SC3).
"""

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from modules.yolo_detection.run_yolo import parse_device, resolve_auto_device
from modules.yolo_detection.source import DEFAULT_WEIGHTS_PATH, YoloDetectionSource

JpegDecoder = Callable[[bytes], Any]


def decode_jpeg_bgr(jpeg_bytes: bytes) -> Any:
    """JPEG 바이트 -> BGR ndarray. 깨진 JPEG 이면 None."""
    # 모듈 상단에서 import 하면 yolo 그룹이 없는 base 환경에서 이 모듈 import 가 깨진다
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)


def iter_decoded_images(
    jpeg_items: Iterable[tuple[float, bytes]], decode: JpegDecoder = decode_jpeg_bgr
) -> Iterator[tuple[float, Any]]:
    # 디코드 못 한 프레임은 건너뛴다 — 브라우저가 보낸 프레임 하나가 깨져도 스트림은 계속된다
    for timestamp_sec, jpeg_bytes in jpeg_items:
        image = decode(jpeg_bytes)
        if image is None:
            continue
        yield timestamp_sec, image


def create_jpeg_yolo_source(
    jpeg_items: Iterable[tuple[float, bytes]],
    device: str = "auto",
    model: Any = None,
    decode: JpegDecoder = decode_jpeg_bgr,
    weights: str = DEFAULT_WEIGHTS_PATH,
) -> YoloDetectionSource:
    parsed_device = parse_device(device)
    if parsed_device is None and model is None:
        # 가짜 모델을 주입한 테스트는 torch 없이 돌아야 하므로 실제 모델을 만들 때만 장치를 고른다
        parsed_device = resolve_auto_device()

    return YoloDetectionSource(
        iter_decoded_images(jpeg_items, decode), model=model, weights=weights, device=parsed_device
    )
