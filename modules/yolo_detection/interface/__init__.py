"""탐지 계약 — YOLO 형식 프레임·탐지와 DetectionSource Protocol."""

from modules.yolo_detection.interface.detection import COCO_CLASS_IDS, Detection, DetectionSource, Frame

__all__ = ["COCO_CLASS_IDS", "Detection", "DetectionSource", "Frame"]
