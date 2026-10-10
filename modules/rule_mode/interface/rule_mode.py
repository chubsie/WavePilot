"""rule_mode 공유 계약 — search / approach / hold / return_home 과 미션 루프가 함께 쓰는 입력·설정·Protocol.

모드는 tick(10 Hz)마다 step 으로 한 번씩 불린다. 모드는 FlightControl 로 상위 명령만 보내고, 끝나면
ModeResult(decision.interface.state)를 정확히 한 번 돌려준다 (RM-07).

interface/ 는 다른 모듈의 interface 패키지와 표준 라이브러리만 import 한다 (import 경계).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import FlightControl, Telemetry
from modules.perception.interface import DEFAULT_CAMERA, CameraIntrinsics, Track
from modules.yolo_detection.interface import Frame

# 모든 모드 생성자의 파라미터 타입 — decision/params.py 의 Params 값과 같다
ModeParams = Mapping[str, str | int | float | None]


def _require_finite(name: str, value: object) -> None:
    # bool 은 int 의 하위 타입이라 따로 막는다
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")


def _require_positive(name: str, value: object) -> None:
    _require_finite(name, value)
    if value <= 0:  # type: ignore[operator]
        raise ValueError(f"field '{name}' must be > 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class SearchArea:
    """수색 직사각형 (NED [m])."""

    north_min_m: float
    north_max_m: float
    east_min_m: float
    east_max_m: float

    def __post_init__(self) -> None:
        for name in ("north_min_m", "north_max_m", "east_min_m", "east_max_m"):
            _require_finite(name, getattr(self, name))

        if self.north_min_m >= self.north_max_m:
            raise ValueError(
                f"field 'north_min_m' must be < 'north_max_m', got {self.north_min_m!r} >= {self.north_max_m!r}"
            )
        if self.east_min_m >= self.east_max_m:
            raise ValueError(
                f"field 'east_min_m' must be < 'east_max_m', got {self.east_min_m!r} >= {self.east_max_m!r}"
            )


# D-05: 홈 북쪽 직사각형
DEFAULT_SEARCH_AREA = SearchArea(north_min_m=20.0, north_max_m=120.0, east_min_m=-50.0, east_max_m=50.0)


@dataclass(frozen=True, slots=True)
class RuleModeSettings:
    target_class: str = "person"
    search_area: SearchArea = DEFAULT_SEARCH_AREA
    search_altitude_m: float = 15.0
    search_lane_spacing_m: float = 25.0
    camera: CameraIntrinsics = DEFAULT_CAMERA
    arrival_tolerance_m: float = 1.5

    def __post_init__(self) -> None:
        if not isinstance(self.target_class, str) or not self.target_class:
            raise ValueError(f"field 'target_class' must be a non-empty str, got {self.target_class!r}")
        if not isinstance(self.search_area, SearchArea):
            raise ValueError(f"field 'search_area' must be a SearchArea, got {self.search_area!r}")

        _require_positive("search_altitude_m", self.search_altitude_m)
        _require_positive("search_lane_spacing_m", self.search_lane_spacing_m)
        _require_positive("arrival_tolerance_m", self.arrival_tolerance_m)


@dataclass(frozen=True, slots=True)
class ModeTick:
    """한 tick 에 모드가 보는 입력 — 읽기 전용 스냅샷."""

    now_sec: float
    telemetry: Telemetry  # 최신
    tracks: tuple[Track, ...]  # TrackAggregator.snapshot
    frame: Frame | None  # 이번 tick 의 최신 YOLO 프레임 (없을 수 있다)
    frame_telemetry: Telemetry | None  # frame.timestamp_sec 시점의 텔레메트리
    hearted_track_ids: frozenset[int]

    def __post_init__(self) -> None:
        _require_finite("now_sec", self.now_sec)

        if not isinstance(self.tracks, tuple):
            raise ValueError(f"field 'tracks' must be a tuple, got {type(self.tracks).__name__}")
        if not isinstance(self.hearted_track_ids, frozenset):
            raise ValueError(
                f"field 'hearted_track_ids' must be a frozenset, got {type(self.hearted_track_ids).__name__}"
            )


@runtime_checkable
class RuleMode(Protocol):
    rule_mode: str

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        """tick 한 번을 처리한다.

        - 첫 호출: 모드의 초기 명령을 보내고, tick.now_sec 에서 모드의 시계를 시작한다.
        - 실행 중에는 None 을 돌려준다.
        - 끝나면 ModeResult 를 정확히 한 번 돌려준다. 호출자는 그 뒤 step 을 다시 부르지 않는다.
        - 거부된 명령(CommandAck.accepted 가 False)은 모드를 outcome "failure" 로 끝낸다 (D-04).
          단 timeout 이후의 return_home 은 취소 없이 계속 비행한다.
        """
        ...


def track_label(track_id: int) -> str:
    """ModeResult.target_track 형식("T7")으로 바꾼다."""
    if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id < 0:
        raise ValueError(f"field 'track_id' must be an int >= 0, got {track_id!r}")

    return f"T{track_id}"
