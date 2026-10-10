"""YOLO 구현 선택 — 컴포지션 루트가 --yolo local|orin 을 YoloFactory 하나로 바꾸는 유일한 곳 (D-05).

- local: Mac 에서 yolo11n 을 직접 돌린다 (create_jpeg_yolo_source, yolo 그룹 필요)
- orin:  JPEG 를 Orin YOLO 서버로 보내 Frame 을 받는다 (create_remote_yolo_source, 표준 라이브러리만)

두 구현은 함수 안에서 늦게 import 한다 — 이 모듈을 import 해도 cv2·ultralytics 가 딸려 오지 않는다.
"""

import functools
from collections.abc import Callable, Iterable

from modules.yolo_detection.interface import DetectionSource
from modules.yolo_detection.remote.protocol import DEFAULT_PORT

YOLO_LOCAL = "local"
YOLO_ORIN = "orin"
YOLO_CHOICES = (YOLO_LOCAL, YOLO_ORIN)
DEFAULT_YOLO_URL = f"http://127.0.0.1:{DEFAULT_PORT}"

# modules.simulation.browser.detection_source.YoloFactory 와 같은 모양 — 그 모듈을 끌어오지 않으려 여기서 다시 적는다
YoloFactory = Callable[[Iterable[tuple[float, bytes]]], DetectionSource]


def create_yolo_factory(kind: str, device: str, url: str) -> YoloFactory:
    if kind == YOLO_ORIN:
        from modules.yolo_detection.remote.client import create_remote_yolo_source

        return functools.partial(create_remote_yolo_source, base_url=url)

    if kind == YOLO_LOCAL:
        from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source

        return functools.partial(create_jpeg_yolo_source, device=device)

    raise ValueError(f"unknown yolo kind {kind!r}, expected one of {YOLO_CHOICES}")
