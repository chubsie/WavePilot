"""실제 YOLO DetectionSource 어댑터 패키지 (DET-01).

다른 모듈은 interface/ 만 import 하고 simulation·perception·llm_report 구현은 import 하지 않는다 — 컴포지션 루트가 mock 과 바꿔 끼운다.
이미지 소스(`modules.yolo_detection.image_source`, plan 01.1-02)는 cv2 가 필요해 여기서 재노출하지 않고 모듈 경로로 import 한다.
"""

from modules.yolo_detection.convert import TRACKED_CLASS_IDS, convert_boxes
from modules.yolo_detection.source import YoloDetectionSource

__all__ = ["TRACKED_CLASS_IDS", "YoloDetectionSource", "convert_boxes"]
