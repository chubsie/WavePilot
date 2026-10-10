"""요청 상태 v2 — rule_mode 선택기가 보는 "지금 상황" 한 장 (D-08).

v1(modules/llm_report/state.py) 규약을 decision 패키지 안으로 옮겨 쓴다. interface/ 는 다른 모듈 구현을
import 할 수 없으므로(import 경계) llm_report 구현을 import 하지 않고 패턴을 복사·적응했다.

v2 에서 더한 것:
1. 사건 종류 — 모드 종료 / 새 대상 발견 / 주기 확인 (event.kind)
2. 직전 rule_mode 와 그 결과 — 성공 / 실패 / 시간 초과 + 대상 (event.last_mode)
3. 임무 요약 — 대상 클래스·상체 색, 하트를 이미 보낸 track 목록 (mission)
그리고 Phase 4 상체 색 속성이 들어갈 자리(tracks[].upper_color, 지금은 null 가능)를 미리 둔다.

지키는 규약 4개:
- 고정 키 순서와 track 순서(라벨 숫자 오름차순) — 같은 상태는 같은 바이트(프롬프트 캐시).
- 반올림 — 위치·각도·시각·배터리 소수 1자리, 신뢰도 2자리. -0.0 은 0.0 으로 바꾼다.
- 자유 텍스트 금지 — 모든 문자열 값은 짧은 토큰이어야 한다(프롬프트 주입 방지).
- target_ 접두 회피 — dataclass 필드명은 CONTRACTS 대로 두고 JSON 키만 class/upper_color/track 으로 내보낸다.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from modules.decision.interface.menu import RULE_MODES

SELECTION_STATE_SCHEMA_VERSION = 2
POSITION_DECIMALS = 1  # 0.1 m 이면 판단에 충분하고 토큰이 준다
ANGLE_DECIMALS = 1
TIME_DECIMALS = 1
BATTERY_DECIMALS = 1
CONFIDENCE_DECIMALS = 2  # 신뢰도는 경계 판단에 쓰므로 한 자리 더 둔다
TRACK_LABEL_PATTERN = re.compile(r"^T(0|[1-9]\d*)$")  # 앞자리 0 금지 — T01 과 T1 이 같은 정렬 키가 되지 않게
CLASS_NAME_PATTERN = re.compile(r"^[a-z][a-z_]{0,31}$")  # class 와 upper_color 모두에 쓴다
SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,32}$")  # 출력 최종 검사 — 모든 문자열 값이 짧은 토큰
FORBIDDEN_KEY_PREFIX = "target_"

# interface/ 는 표준 라이브러리만 import 하므로 flight_control.interface.FLIGHT_MODES 를 복사한다.
# 두 값이 어긋나지 않는지는 tests/test_decision_state_v2.py 가 지킨다
DRONE_FLIGHT_MODES = ("grounded", "hover", "moving", "landing", "returning")

REQUEST_TOP_LEVEL_KEYS = ("schema_version", "time_sec", "event", "drone", "tracks", "mission")
STATE_TOP_LEVEL_KEYS = ("schema_version", "time_sec", "drone", "tracks", "mission")
DRONE_KEYS = (
    "position_ned_m", "altitude_m", "attitude_deg", "gimbal_deg", "battery_pct", "flight_mode",
)
DRONE_NUMBER_FIELDS = (
    "north_m", "east_m", "down_m", "altitude_m", "roll_deg", "pitch_deg", "yaw_deg",
    "gimbal_pitch_deg", "gimbal_yaw_deg", "battery_pct",
)
TRACK_KEYS = (
    "track_id", "class", "position_ned_m", "first_seen_sec", "last_seen_sec",
    "observation_count", "mean_confidence", "is_active", "upper_color",
)
MISSION_KEYS = ("class", "upper_color", "hearted_tracks")
EVENT_KEYS = ("kind", "last_mode")
MODE_RESULT_KEYS = ("rule_mode", "outcome", "track")

EVENT_MODE_ENDED = "mode_ended"
EVENT_NEW_TARGET = "new_target"
EVENT_PERIODIC_CHECK = "periodic_check"
EVENT_KINDS = (EVENT_MODE_ENDED, EVENT_NEW_TARGET, EVENT_PERIODIC_CHECK)

MODE_OUTCOMES = ("success", "failure", "timeout")


# --- 사건과 직전 모드 결과 ---


@dataclass(frozen=True, slots=True)
class ModeResult:
    rule_mode: str
    outcome: str  # MODE_OUTCOMES 중 하나
    target_track: str | None  # JSON 에서는 "track" 키. 대상 없는 모드면 None

    def __post_init__(self) -> None:
        if self.rule_mode not in RULE_MODES:
            raise ValueError(f"field 'rule_mode' must be one of {RULE_MODES}, got {self.rule_mode!r}")
        if self.outcome not in MODE_OUTCOMES:
            raise ValueError(f"field 'outcome' must be one of {MODE_OUTCOMES}, got {self.outcome!r}")
        if self.target_track is not None:
            _validate_track_label(self.target_track, "last_mode.track")

    def to_dict(self) -> dict:
        return {"rule_mode": self.rule_mode, "outcome": self.outcome, "track": self.target_track}

    @classmethod
    def from_dict(cls, data: object) -> "ModeResult":
        _require_exact_keys(data, MODE_RESULT_KEYS, "last_mode")

        target_track = data["track"]
        if target_track is not None:
            target_track = _require_str(data, "track", "last_mode")

        return cls(
            rule_mode=_require_str(data, "rule_mode", "last_mode"),
            outcome=_require_str(data, "outcome", "last_mode"),
            target_track=target_track,
        )


@dataclass(frozen=True, slots=True)
class SelectionEvent:
    kind: str  # EVENT_KINDS 중 하나
    mode_result: ModeResult | None  # kind == mode_ended 일 때만 있다

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"field 'kind' must be one of {EVENT_KINDS}, got {self.kind!r}")

        # 사건 의미가 섞이지 않게: 모드 종료만 결과를 갖고, 나머지는 결과가 없어야 한다
        if self.kind == EVENT_MODE_ENDED and self.mode_result is None:
            raise ValueError("event 'mode_ended' requires a mode_result")
        if self.kind != EVENT_MODE_ENDED and self.mode_result is not None:
            raise ValueError(f"event {self.kind!r} must not carry a mode_result")

    def to_dict(self) -> dict:
        last_mode = self.mode_result.to_dict() if self.mode_result is not None else None
        return {"kind": self.kind, "last_mode": last_mode}


# --- 상태 ---


@dataclass(frozen=True, slots=True)
class TrackState:
    track_id: str  # "T3" 형식 라벨
    class_name: str  # JSON 에서는 "class" 키 (파이썬 예약어 회피는 필드명만)
    north_m: float
    east_m: float
    first_seen_sec: float
    last_seen_sec: float
    observation_count: int
    mean_confidence: float
    is_active: bool
    upper_color: str | None  # Phase 4 상체 색 자리. 아직 모르면 None

    def __post_init__(self) -> None:
        name = f"tracks.{self.track_id}"
        _validate_track_label(self.track_id, "tracks.track_id")
        _validate_class_token(self.class_name, f"{name}.class")
        if self.upper_color is not None:
            _validate_class_token(self.upper_color, f"{name}.upper_color")

        for field_name in ("north_m", "east_m", "first_seen_sec", "last_seen_sec", "mean_confidence"):
            _validate_finite(getattr(self, field_name), f"{name}.{field_name}")
        if self.first_seen_sec > self.last_seen_sec:
            raise ValueError(f"field '{name}.first_seen_sec' must be <= last_seen_sec")
        if not 0.0 <= self.mean_confidence <= 1.0:
            raise ValueError(f"field '{name}.mean_confidence' must be in [0, 1], got {self.mean_confidence}")

        if not isinstance(self.observation_count, int) or isinstance(self.observation_count, bool):
            raise ValueError(f"field '{name}.observation_count' must be int, got {self.observation_count!r}")
        if self.observation_count < 1:
            raise ValueError(f"field '{name}.observation_count' must be >= 1, got {self.observation_count}")
        if not isinstance(self.is_active, bool):
            raise ValueError(f"field '{name}.is_active' must be bool, got {self.is_active!r}")

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "class": self.class_name,
            "position_ned_m": {"north": self.north_m, "east": self.east_m},
            "first_seen_sec": self.first_seen_sec,
            "last_seen_sec": self.last_seen_sec,
            "observation_count": self.observation_count,
            "mean_confidence": self.mean_confidence,
            "is_active": self.is_active,
            "upper_color": self.upper_color,
        }

    @classmethod
    def from_dict(cls, data: object, index: int) -> "TrackState":
        name = f"tracks[{index}]"
        _require_exact_keys(data, TRACK_KEYS, name)

        position = data["position_ned_m"]
        _require_exact_keys(position, ("north", "east"), f"{name}.position_ned_m")

        upper_color = data["upper_color"]
        if upper_color is not None:
            upper_color = _require_str(data, "upper_color", name)

        return cls(
            track_id=_require_str(data, "track_id", name),
            class_name=_require_str(data, "class", name),
            north_m=_require_number(position, "north", f"{name}.position_ned_m"),
            east_m=_require_number(position, "east", f"{name}.position_ned_m"),
            first_seen_sec=_require_number(data, "first_seen_sec", name),
            last_seen_sec=_require_number(data, "last_seen_sec", name),
            observation_count=_require_int(data, "observation_count", name),
            mean_confidence=_require_number(data, "mean_confidence", name),
            is_active=data["is_active"],
            upper_color=upper_color,
        )


@dataclass(frozen=True, slots=True)
class DroneState:
    north_m: float
    east_m: float
    down_m: float
    altitude_m: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    gimbal_pitch_deg: float
    gimbal_yaw_deg: float
    battery_pct: float
    flight_mode: str

    def __post_init__(self) -> None:
        for field_name in DRONE_NUMBER_FIELDS:
            _validate_finite(getattr(self, field_name), f"drone.{field_name}")

        if self.flight_mode not in DRONE_FLIGHT_MODES:
            raise ValueError(f"field 'drone.flight_mode' must be one of {DRONE_FLIGHT_MODES}, got {self.flight_mode!r}")

    def to_dict(self) -> dict:
        return {
            "position_ned_m": {"north": self.north_m, "east": self.east_m, "down": self.down_m},
            "altitude_m": self.altitude_m,
            "attitude_deg": {"roll": self.roll_deg, "pitch": self.pitch_deg, "yaw": self.yaw_deg},
            "gimbal_deg": {"pitch": self.gimbal_pitch_deg, "yaw": self.gimbal_yaw_deg},
            "battery_pct": self.battery_pct,
            "flight_mode": self.flight_mode,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DroneState":
        _require_exact_keys(data, DRONE_KEYS, "drone")

        position = data["position_ned_m"]
        attitude = data["attitude_deg"]
        gimbal = data["gimbal_deg"]
        _require_exact_keys(position, ("north", "east", "down"), "drone.position_ned_m")
        _require_exact_keys(attitude, ("roll", "pitch", "yaw"), "drone.attitude_deg")
        _require_exact_keys(gimbal, ("pitch", "yaw"), "drone.gimbal_deg")

        return cls(
            north_m=_require_number(position, "north", "drone.position_ned_m"),
            east_m=_require_number(position, "east", "drone.position_ned_m"),
            down_m=_require_number(position, "down", "drone.position_ned_m"),
            altitude_m=_require_number(data, "altitude_m", "drone"),
            roll_deg=_require_number(attitude, "roll", "drone.attitude_deg"),
            pitch_deg=_require_number(attitude, "pitch", "drone.attitude_deg"),
            yaw_deg=_require_number(attitude, "yaw", "drone.attitude_deg"),
            gimbal_pitch_deg=_require_number(gimbal, "pitch", "drone.gimbal_deg"),
            gimbal_yaw_deg=_require_number(gimbal, "yaw", "drone.gimbal_deg"),
            battery_pct=_require_number(data, "battery_pct", "drone"),
            flight_mode=_require_str(data, "flight_mode", "drone"),
        )


@dataclass(frozen=True, slots=True)
class MissionSummary:
    target_class: str  # JSON 에서는 "class" 키
    target_upper_color: str | None  # JSON 에서는 "upper_color" 키. 색 조건이 없으면 None
    hearted_tracks: tuple[str, ...]  # 하트를 이미 보낸 track 라벨, 라벨 숫자 오름차순

    def __post_init__(self) -> None:
        _validate_class_token(self.target_class, "mission.class")
        if self.target_upper_color is not None:
            _validate_class_token(self.target_upper_color, "mission.upper_color")

        for label in self.hearted_tracks:
            _validate_track_label(label, "mission.hearted_tracks")
        _reject_duplicate_labels(self.hearted_tracks, "mission.hearted_tracks")

        object.__setattr__(self, "hearted_tracks", tuple(sorted(self.hearted_tracks, key=_get_label_number)))

    def to_dict(self) -> dict:
        return {
            "class": self.target_class,
            "upper_color": self.target_upper_color,
            "hearted_tracks": list(self.hearted_tracks),
        }

    @classmethod
    def from_dict(cls, data: object) -> "MissionSummary":
        _require_exact_keys(data, MISSION_KEYS, "mission")

        upper_color = data["upper_color"]
        if upper_color is not None:
            upper_color = _require_str(data, "upper_color", "mission")

        hearted_tracks = data["hearted_tracks"]
        if not isinstance(hearted_tracks, list):
            raise ValueError(f"field 'mission.hearted_tracks' must be a list, got {type(hearted_tracks).__name__}")
        for label in hearted_tracks:
            if not isinstance(label, str):
                raise ValueError(f"field 'mission.hearted_tracks' items must be str, got {label!r}")

        return cls(
            target_class=_require_str(data, "class", "mission"),
            target_upper_color=upper_color,
            hearted_tracks=tuple(hearted_tracks),
        )


@dataclass(frozen=True, slots=True)
class SelectionState:
    time_sec: float
    drone: DroneState
    tracks: tuple[TrackState, ...]  # 라벨 숫자 오름차순으로 고정된다
    mission: MissionSummary

    def __post_init__(self) -> None:
        _validate_finite(self.time_sec, "time_sec")
        _reject_duplicate_labels([track.track_id for track in self.tracks], "tracks")

        object.__setattr__(self, "tracks", _sort_tracks(self.tracks))

    def to_dict(self) -> dict:
        """픽스처·from_dict 왕복용 모양. 반올림하지 않고 event 도 없다."""
        return {
            "schema_version": SELECTION_STATE_SCHEMA_VERSION,
            "time_sec": self.time_sec,
            "drone": self.drone.to_dict(),
            "tracks": [track.to_dict() for track in self.tracks],
            "mission": self.mission.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "SelectionState":
        """기록·합성 상태 dict 를 검증한다. 반올림 자릿수는 검사하지 않는다."""
        _require_exact_keys(data, STATE_TOP_LEVEL_KEYS, "state")

        schema_version = data["schema_version"]
        if type(schema_version) is not int or schema_version != SELECTION_STATE_SCHEMA_VERSION:
            raise ValueError(f"field 'schema_version' must be {SELECTION_STATE_SCHEMA_VERSION}, got {schema_version!r}")

        raw_tracks = data["tracks"]
        if not isinstance(raw_tracks, list):
            raise ValueError(f"field 'tracks' must be a list, got {type(raw_tracks).__name__}")

        return cls(
            time_sec=_require_number(data, "time_sec", "state"),
            drone=DroneState.from_dict(data["drone"]),
            tracks=tuple(TrackState.from_dict(raw_track, index) for index, raw_track in enumerate(raw_tracks)),
            mission=MissionSummary.from_dict(data["mission"]),
        )


# --- 요청 상태 v2 ---


def build_request_state(state: SelectionState, event: SelectionEvent) -> dict:
    """선택기에 보낼 v2 dict. 키 순서는 REQUEST_TOP_LEVEL_KEYS 로 고정한다."""
    request = {
        "schema_version": SELECTION_STATE_SCHEMA_VERSION,
        "time_sec": _round_value(state.time_sec, TIME_DECIMALS),
        "event": event.to_dict(),
        "drone": _round_drone(state.drone).to_dict(),
        "tracks": [_round_track(track).to_dict() for track in state.tracks],
        "mission": state.mission.to_dict(),
    }

    # 생성자 검증을 뚫은 값이 있어도 프롬프트로 새지 않게 하는 마지막 문(주입 방지)
    _validate_request_tokens(request)

    return request


# --- 모듈 내부 도우미 ---


def _round_drone(drone: DroneState) -> DroneState:
    return replace(
        drone,
        north_m=_round_value(drone.north_m, POSITION_DECIMALS),
        east_m=_round_value(drone.east_m, POSITION_DECIMALS),
        down_m=_round_value(drone.down_m, POSITION_DECIMALS),
        altitude_m=_round_value(drone.altitude_m, POSITION_DECIMALS),
        roll_deg=_round_value(drone.roll_deg, ANGLE_DECIMALS),
        pitch_deg=_round_value(drone.pitch_deg, ANGLE_DECIMALS),
        yaw_deg=_round_value(drone.yaw_deg, ANGLE_DECIMALS),
        gimbal_pitch_deg=_round_value(drone.gimbal_pitch_deg, ANGLE_DECIMALS),
        gimbal_yaw_deg=_round_value(drone.gimbal_yaw_deg, ANGLE_DECIMALS),
        battery_pct=_round_value(drone.battery_pct, BATTERY_DECIMALS),
    )


def _round_track(track: TrackState) -> TrackState:
    # 반올림은 단조라 first_seen_sec <= last_seen_sec, mean_confidence ∈ [0, 1] 이 그대로 유지된다
    return replace(
        track,
        north_m=_round_value(track.north_m, POSITION_DECIMALS),
        east_m=_round_value(track.east_m, POSITION_DECIMALS),
        first_seen_sec=_round_value(track.first_seen_sec, TIME_DECIMALS),
        last_seen_sec=_round_value(track.last_seen_sec, TIME_DECIMALS),
        mean_confidence=_round_value(track.mean_confidence, CONFIDENCE_DECIMALS),
    )


def _round_value(value: float, decimals: int) -> float:
    # round(-0.04, 1) 은 -0.0 이 되는데, 0.0 을 더하면 +0.0 으로 바뀐다 ("-0.0" 이 프롬프트에 새지 않게)
    return round(value, decimals) + 0.0


def _validate_request_tokens(value: object, path: str = "request") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.startswith(FORBIDDEN_KEY_PREFIX):
                raise ValueError(f"key '{path}.{key}' must not start with {FORBIDDEN_KEY_PREFIX!r}")
            _validate_request_tokens(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_request_tokens(item, f"{path}[{index}]")
    elif isinstance(value, str) and not SAFE_TOKEN_PATTERN.match(value):
        raise ValueError(f"value at '{path}' must match {SAFE_TOKEN_PATTERN.pattern}, got {value!r}")


def _validate_track_label(label: object, name: str) -> None:
    if not isinstance(label, str) or not TRACK_LABEL_PATTERN.match(label):
        raise ValueError(f"field '{name}' must match 'T<digits>', got {label!r}")


def _validate_class_token(value: object, name: str) -> None:
    if not isinstance(value, str) or not CLASS_NAME_PATTERN.match(value):
        raise ValueError(f"field '{name}' must match {CLASS_NAME_PATTERN.pattern}, got {value!r}")


def _validate_finite(value: object, name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")


def _reject_duplicate_labels(labels: Sequence[str], name: str) -> None:
    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    if duplicate_labels:
        raise ValueError(f"field '{name}' has duplicate track labels {duplicate_labels}")


def _get_label_number(label: str) -> int:
    # 정렬용 비공개 키. 라벨 형식은 생성자 검증이 보장한다
    return int(label[1:])


def _sort_tracks(tracks: Sequence[TrackState]) -> tuple[TrackState, ...]:
    return tuple(sorted(tracks, key=lambda track: _get_label_number(track.track_id)))


def _require_exact_keys(data: object, keys: Sequence[str], name: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a dict, got {type(data).__name__}")

    missing_keys = [key for key in keys if key not in data]
    if missing_keys:
        raise ValueError(f"{name} is missing fields {missing_keys}")

    unknown_keys = sorted(str(key) for key in data if key not in keys)
    if unknown_keys:
        raise ValueError(f"{name} has unknown fields {unknown_keys}")


def _require_number(data: dict, key: str, name: str) -> float:
    value = data[key]
    # bool 은 int 의 하위 타입이라 따로 막고, NaN·inf 는 JSON 표준 밖이라 거부한다
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"field '{name}.{key}' must be a finite number, got {value!r}")
    return float(value)


def _require_int(data: dict, key: str, name: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"field '{name}.{key}' must be int, got {value!r}")
    return value


def _require_str(data: dict, key: str, name: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"field '{name}.{key}' must be str, got {value!r}")
    return value
