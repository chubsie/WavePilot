"""track 계약 — 여러 프레임의 탐지를 track_id 별로 모아 월드 좌표로 요약한 것.

perception 이 만들고 llm_report 가 받는다 (D-06). LLM 레이어는 픽셀·프레임을 보지 않고
이 목록과 Telemetry 만으로 상황을 판단한다. track_id 는 YOLO 와 같은 int 이고,
상태 JSON·보고에서는 "T{track_id}" 문자열 라벨로 바꿔 쓴다.
상체 색 세 필드는 기본값이 있어 이 키가 없는 옛 dict 도 그대로 읽힌다(하위 호환).
"""

from dataclasses import dataclass

from modules.yolo_detection.interface.detection import UPPER_COLORS


@dataclass(frozen=True, slots=True)
class Track:
    track_id: int
    class_name: str
    north_m: float
    east_m: float
    first_seen_sec: float
    last_seen_sec: float
    observation_count: int
    mean_confidence: float
    is_active: bool
    upper_color: str | None = None  # 최근 창 다수결로 확정된 상체 색, 미확정이면 None (D-04)
    upper_color_ratio: float | None = None  # 창 안 1위 색 비율 0~1, 표가 0개면 None
    upper_color_vote_count: int = 0  # 창 안 unknown 아닌 표 수

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "class_name": self.class_name,
            "north_m": self.north_m,
            "east_m": self.east_m,
            "first_seen_sec": self.first_seen_sec,
            "last_seen_sec": self.last_seen_sec,
            "observation_count": self.observation_count,
            "mean_confidence": self.mean_confidence,
            "is_active": self.is_active,
            "upper_color": self.upper_color,
            "upper_color_ratio": self.upper_color_ratio,
            "upper_color_vote_count": self.upper_color_vote_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        if not isinstance(data, dict):
            raise ValueError(f"track must be a dict, got {type(data).__name__}")

        # track 은 최소 한 번 관측돼야 생긴다
        observation_count = _require_int(data, "observation_count")
        if observation_count < 1:
            raise ValueError(f"field 'observation_count' must be >= 1, got {observation_count}")

        is_active = _require_key(data, "is_active")
        if not isinstance(is_active, bool):
            raise ValueError(f"field 'is_active' must be bool, got {is_active!r}")

        class_name = _require_key(data, "class_name")
        if not isinstance(class_name, str):
            raise ValueError(f"field 'class_name' must be str, got {class_name!r}")

        upper_color = _read_optional_upper_color(data)
        upper_color_ratio = _read_optional_ratio(data, "upper_color_ratio")
        upper_color_vote_count = data.get("upper_color_vote_count", 0)
        if not _is_int(upper_color_vote_count) or upper_color_vote_count < 0:
            raise ValueError(f"field 'upper_color_vote_count' must be int >= 0, got {upper_color_vote_count!r}")

        return cls(
            track_id=_require_int(data, "track_id"),
            class_name=class_name,
            north_m=_require_number(data, "north_m"),
            east_m=_require_number(data, "east_m"),
            first_seen_sec=_require_number(data, "first_seen_sec"),
            last_seen_sec=_require_number(data, "last_seen_sec"),
            observation_count=observation_count,
            mean_confidence=_require_number(data, "mean_confidence"),
            is_active=is_active,
            upper_color=upper_color,
            upper_color_ratio=upper_color_ratio,
            upper_color_vote_count=upper_color_vote_count,
        )


# --- 모듈 내부 검증 도우미 ---


def _is_int(value: object) -> bool:
    # bool 은 int 의 하위 타입이라 따로 막는다
    return isinstance(value, int) and not isinstance(value, bool)


def _read_optional_upper_color(data: dict) -> str | None:
    value = data.get("upper_color")
    if value is None:
        return None
    if not isinstance(value, str) or value not in UPPER_COLORS:
        raise ValueError(f"field 'upper_color' must be one of {UPPER_COLORS} or null, got {value!r}")
    return value


def _read_optional_ratio(data: dict, key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if not (_is_int(value) or isinstance(value, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"field '{key}' must be a number in [0, 1] or null, got {value!r}")
    return float(value)


def _require_key(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"missing field '{key}'")
    return data[key]


def _require_int(data: dict, key: str) -> int:
    value = _require_key(data, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"field '{key}' must be int, got {value!r}")
    return value


def _require_number(data: dict, key: str) -> float:
    value = _require_key(data, key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"field '{key}' must be a number, got {value!r}")
    return float(value)
