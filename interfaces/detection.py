"""탐지 인터페이스 — YOLO(Ultralytics) 추적 결과와 1:1 로 맞춘 프레임·탐지 계약.

필드는 Ultralytics `results[0].boxes` 와 그대로 대응한다:
- class_id   <- boxes.cls  (COCO 클래스 번호)
- confidence <- boxes.conf (0~1)
- xyxy       <- boxes.xyxy (픽셀, 좌상단 (x1, y1) / 우하단 (x2, y2))
- track_id   <- boxes.id   (트래커가 아직 ID 를 주지 않았으면 None)

mock 이든 실제 YOLO 어댑터든 같은 Frame 을 내므로, 인식 계층은 구현체를 몰라도 된다.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Ultralytics COCO 번호 그대로 — 실제 모델로 교체해도 숫자가 바뀌지 않게
COCO_CLASS_IDS: dict[str, int] = {"person": 0, "car": 2, "bus": 5, "truck": 7}


@dataclass(frozen=True, slots=True)
class Detection:
    class_id: int
    class_name: str
    confidence: float
    xyxy: tuple[float, float, float, float]
    track_id: int | None

    def to_dict(self) -> dict:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "confidence": self.confidence,
            "xyxy": list(self.xyxy),
            "track_id": self.track_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Detection":
        _require_dict(data, "detection")

        confidence = _require_number(data, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"field 'confidence' must be in [0, 1], got {confidence}")

        xyxy = _require_xyxy(data, "xyxy")

        track_id = _require_key(data, "track_id")
        if track_id is not None and not _is_int(track_id):
            raise ValueError(f"field 'track_id' must be int or null, got {track_id!r}")

        return cls(
            class_id=_require_int(data, "class_id"),
            class_name=_require_str(data, "class_name"),
            confidence=confidence,
            xyxy=xyxy,
            track_id=track_id,
        )


@dataclass(frozen=True, slots=True)
class Frame:
    frame_id: int
    timestamp_sec: float
    image_width: int
    image_height: int
    detections: tuple[Detection, ...]

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "timestamp_sec": self.timestamp_sec,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "detections": [detection.to_dict() for detection in self.detections],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Frame":
        _require_dict(data, "frame")

        raw_detections = data.get("detections")
        if not isinstance(raw_detections, list):
            raise ValueError("field 'detections' must be a list")

        return cls(
            frame_id=_require_int(data, "frame_id"),
            timestamp_sec=_require_number(data, "timestamp_sec"),
            image_width=_require_int(data, "image_width"),
            image_height=_require_int(data, "image_height"),
            detections=tuple(Detection.from_dict(item) for item in raw_detections),
        )


@runtime_checkable
class DetectionSource(Protocol):
    def read_frame(self) -> Frame | None:
        """다음 프레임. None 이면 시나리오/스트림이 끝났다."""
        ...


# --- 모듈 내부 검증 도우미 (from_dict 는 프로세스 밖에서 온 JSON 을 받으므로 엄격히 검사) ---


def _is_int(value: object) -> bool:
    # bool 은 int 의 하위 타입이라 따로 막는다
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(data: object, name: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a dict, got {type(data).__name__}")


def _require_key(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"missing field '{key}'")
    return data[key]


def _require_int(data: dict, key: str) -> int:
    value = _require_key(data, key)
    if not _is_int(value):
        raise ValueError(f"field '{key}' must be int, got {value!r}")
    return value


def _require_number(data: dict, key: str) -> float:
    value = _require_key(data, key)
    if not (_is_int(value) or isinstance(value, float)):
        raise ValueError(f"field '{key}' must be a number, got {value!r}")
    return float(value)


def _require_str(data: dict, key: str) -> str:
    value = _require_key(data, key)
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be str, got {value!r}")
    return value


def _require_xyxy(data: dict, key: str) -> tuple[float, float, float, float]:
    value = _require_key(data, key)
    is_number_list = isinstance(value, (list, tuple)) and all(
        _is_int(item) or isinstance(item, float) for item in value
    )
    if not is_number_list or len(value) != 4:
        raise ValueError(f"field '{key}' must be 4 numbers, got {value!r}")

    x1, y1, x2, y2 = (float(item) for item in value)
    if x1 > x2 or y1 > y2:
        raise ValueError(f"field '{key}' must satisfy x1<=x2 and y1<=y2, got {value!r}")
    return (x1, y1, x2, y2)
