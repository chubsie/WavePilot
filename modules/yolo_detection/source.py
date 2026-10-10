"""YOLO 탐지 소스 — 실제 YOLO 추적 결과를 mock 과 같은 Frame 으로 내보낸다 (DET-01, SC1, SC2).

입력은 `Iterable[(timestamp_sec, BGR 이미지)]` 이고 이미지는 이 모듈에게 불투명한 객체다.
인스턴스 하나가 스트림 하나다 — ByteTrack 상태는 모델 객체에 붙고 `persist=True` 가 프레임 간 ID 를 유지한다.
ultralytics 는 `__init__` 안에서만 지연 import 한다. 그래야 yolo 그룹이 없는 base 환경에서도 이 모듈을 import 할 수 있다 (SC3).
프레임 크기는 `result.orig_shape` 에서 가져온다 — imgsz letterbox 와 무관하게 박스가 원본 픽셀 좌표로 나온다.
"""

from collections.abc import Iterable
from typing import Any

from modules.yolo_detection.interface.detection import Frame
from modules.yolo_detection.convert import TRACKED_CLASS_IDS, convert_boxes

DEFAULT_WEIGHTS_PATH = "models/yolo11n.pt"  # cwd 에 자동 다운로드되지 않게 gitignore 된 models/ 로 고정
DEFAULT_TRACKER = "bytetrack.yaml"
DEFAULT_CONFIDENCE_THRESHOLD = 0.25
DEFAULT_IMAGE_SIZE = 640
TIMESTAMP_DECIMALS = 3  # mock 과 같은 자릿수


class YoloDetectionSource:
    def __init__(
        self,
        image_source: Iterable[tuple[float, Any]],
        model: Any = None,
        weights: str = DEFAULT_WEIGHTS_PATH,
        device: str | int | None = None,
        tracker: str = DEFAULT_TRACKER,
        conf: float = DEFAULT_CONFIDENCE_THRESHOLD,
        imgsz: int = DEFAULT_IMAGE_SIZE,
    ) -> None:
        if not 0.0 < conf <= 1.0:
            raise ValueError(f"conf must be in (0, 1], got {conf}")
        if imgsz <= 0:
            raise ValueError(f"imgsz must be > 0, got {imgsz}")

        if model is None:
            # 모듈 상단에서 import 하면 ultralytics 가 없는 base 환경에서 수집 단계가 깨진다
            from ultralytics import YOLO
            model = YOLO(weights)

        self._model = model
        self._images = iter(image_source)
        self._track_kwargs = {
            "persist": True,
            "tracker": tracker,
            "classes": list(TRACKED_CLASS_IDS),
            "conf": conf,
            "imgsz": imgsz,
            "device": device,
            "verbose": False,
        }
        self._frame_index = 0

    def read_frame(self) -> Frame | None:
        item = next(self._images, None)
        if item is None:
            return None
        timestamp_sec, image = item

        result = self._model.track(image, **self._track_kwargs)[0]
        height, width = result.orig_shape

        frame = Frame(
            frame_id=self._frame_index,
            timestamp_sec=round(float(timestamp_sec), TIMESTAMP_DECIMALS),
            image_width=int(width),
            image_height=int(height),
            detections=convert_boxes(result.boxes),
        )
        self._frame_index += 1
        return frame
