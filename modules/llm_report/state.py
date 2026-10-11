"""상태 JSON v1 — LLM 레이어가 보는 "지금 상황" 한 장 (RPT-01).

입력은 modules.perception.interface.track.Track 목록과 modules.flight_control.interface.flight_control.Telemetry 뿐이다(D-06).
프레임·픽셀은 보지 않는다. 모양은 SKELETON "페이로드 JSON 모양" 의 state 줄을 글자 그대로 따른다.

규약:
- track ID 는 상태 안에서 문자열 라벨 "T{track_id}" 로만 쓴다. 정수 ID 는 llm_report 밖으로 나가지 않는다.
- 숫자는 소수 1자리(위치·각도·시각·배터리), 신뢰도만 2자리로 반올림한다 — 프롬프트 토큰 절감,
  그리고 모델이 긴 소수를 베끼다 자릿수를 틀리는 위치 드리프트를 줄인다.
- 같은 상태는 같은 바이트가 되도록 키 순서와 track 순서(ID 숫자 오름차순)를 고정한다(프롬프트 캐시).
- 상태에는 자유 텍스트가 없다. class 는 정규식으로 막아 프롬프트 주입 표면을 없앤다.
- 필드명에 target_ 접두를 쓰지 않는다 — 모델이 보고에서 그 단어를 쓰도록 배우지 않게(AI-SPEC D4).
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from modules.flight_control.interface.flight_control import FLIGHT_MODES, Telemetry
from modules.perception.interface.track import Track

STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_TRACKS = 20  # -c 4096 안에서 track 당 약 60~80 토큰 가정 (AI-SPEC 4b.4)
POSITION_DECIMALS = 1  # 0.1 m 이면 보고 문장에 충분하고 토큰이 준다
ANGLE_DECIMALS = 1
TIME_DECIMALS = 1
BATTERY_DECIMALS = 1
CONFIDENCE_DECIMALS = 2  # 신뢰도는 0.5 경계 판단에 쓰므로 한 자리 더 둔다
TRACK_LABEL_PATTERN = re.compile(r"^T\d+$")
CLASS_NAME_PATTERN = re.compile(r"^[a-z][a-z_]{0,31}$")  # 자유 텍스트를 막아 프롬프트 주입 표면 제거

TOP_LEVEL_KEYS = (
    "schema_version", "time_sec", "drone", "tracks", "counts_by_class", "omitted_track_count",
)
DRONE_KEYS = (
    "position_ned_m", "altitude_m", "attitude_deg", "gimbal_deg", "battery_pct", "flight_mode",
)
TRACK_KEYS = (
    "track_id", "class", "position_ned_m", "first_seen_sec", "last_seen_sec",
    "observation_count", "mean_confidence", "is_active",
)


def format_track_label(track_id: int) -> str:
    return f"T{track_id}"


@dataclass(frozen=True, slots=True)
class TrackStateJson:
    track_id: str  # "T3" 형식 라벨
    class_name: str  # to_dict 에서는 "class" 키로 내보낸다 (파이썬 예약어 회피는 필드명만)
    north_m: float
    east_m: float
    first_seen_sec: float
    last_seen_sec: float
    observation_count: int
    mean_confidence: float
    is_active: bool

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
        }

    @classmethod
    def from_dict(cls, data: object, index: int) -> "TrackStateJson":
        name = f"tracks[{index}]"
        _require_exact_keys(data, TRACK_KEYS, name)

        track_id = _require_str(data, "track_id", name)
        if not TRACK_LABEL_PATTERN.match(track_id):
            raise ValueError(f"field '{name}.track_id' must match 'T<digits>', got {track_id!r}")

        class_name = _require_str(data, "class", name)
        if not CLASS_NAME_PATTERN.match(class_name):
            raise ValueError(f"field '{name}.class' must match {CLASS_NAME_PATTERN.pattern}, got {class_name!r}")

        position = data["position_ned_m"]
        _require_exact_keys(position, ("north", "east"), f"{name}.position_ned_m")

        first_seen_sec = _require_number(data, "first_seen_sec", name)
        last_seen_sec = _require_number(data, "last_seen_sec", name)
        if first_seen_sec > last_seen_sec:
            raise ValueError(f"field '{name}.first_seen_sec' must be <= last_seen_sec")

        observation_count = _require_int(data, "observation_count", name)
        if observation_count < 1:
            raise ValueError(f"field '{name}.observation_count' must be >= 1, got {observation_count}")

        mean_confidence = _require_number(data, "mean_confidence", name)
        if not 0.0 <= mean_confidence <= 1.0:
            raise ValueError(f"field '{name}.mean_confidence' must be in [0, 1], got {mean_confidence}")

        is_active = data["is_active"]
        if not isinstance(is_active, bool):
            raise ValueError(f"field '{name}.is_active' must be bool, got {is_active!r}")

        return cls(
            track_id=track_id,
            class_name=class_name,
            north_m=_require_number(position, "north", f"{name}.position_ned_m"),
            east_m=_require_number(position, "east", f"{name}.position_ned_m"),
            first_seen_sec=first_seen_sec,
            last_seen_sec=last_seen_sec,
            observation_count=observation_count,
            mean_confidence=mean_confidence,
            is_active=is_active,
        )


@dataclass(frozen=True, slots=True)
class DroneStateJson:
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
    def from_dict(cls, data: object) -> "DroneStateJson":
        _require_exact_keys(data, DRONE_KEYS, "drone")

        position = data["position_ned_m"]
        attitude = data["attitude_deg"]
        gimbal = data["gimbal_deg"]
        _require_exact_keys(position, ("north", "east", "down"), "modules.drone.position_ned_m")
        _require_exact_keys(attitude, ("roll", "pitch", "yaw"), "modules.drone.attitude_deg")
        _require_exact_keys(gimbal, ("pitch", "yaw"), "modules.drone.gimbal_deg")

        flight_mode = _require_str(data, "flight_mode", "drone")
        if flight_mode not in FLIGHT_MODES:
            raise ValueError(f"field 'modules.drone.flight_mode' must be one of {FLIGHT_MODES}, got {flight_mode!r}")

        return cls(
            north_m=_require_number(position, "north", "modules.drone.position_ned_m"),
            east_m=_require_number(position, "east", "modules.drone.position_ned_m"),
            down_m=_require_number(position, "down", "modules.drone.position_ned_m"),
            altitude_m=_require_number(data, "altitude_m", "drone"),
            roll_deg=_require_number(attitude, "roll", "modules.drone.attitude_deg"),
            pitch_deg=_require_number(attitude, "pitch", "modules.drone.attitude_deg"),
            yaw_deg=_require_number(attitude, "yaw", "modules.drone.attitude_deg"),
            gimbal_pitch_deg=_require_number(gimbal, "pitch", "modules.drone.gimbal_deg"),
            gimbal_yaw_deg=_require_number(gimbal, "yaw", "modules.drone.gimbal_deg"),
            battery_pct=_require_number(data, "battery_pct", "drone"),
            flight_mode=flight_mode,
        )


@dataclass(frozen=True, slots=True)
class StateJson:
    schema_version: int
    time_sec: float
    drone: DroneStateJson
    tracks: tuple[TrackStateJson, ...]  # track_id 숫자 오름차순
    counts_by_class: dict[str, int]  # 포함된 track(활성·비활성 모두)의 클래스별 개수
    omitted_track_count: int  # 컨텍스트 상한 때문에 잘라 낸 track 수

    @property
    def track_labels(self) -> tuple[str, ...]:
        return tuple(track.track_id for track in self.tracks)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "time_sec": self.time_sec,
            "drone": self.drone.to_dict(),
            "tracks": [track.to_dict() for track in self.tracks],
            "counts_by_class": dict(self.counts_by_class),
            "omitted_track_count": self.omitted_track_count,
        }

    @classmethod
    def from_dict(cls, data: object) -> "StateJson":
        """외부(시뮬레이터 담당자·별도 스크립트) dict 를 검증한다. 반올림 자릿수는 검사하지 않는다."""
        _require_exact_keys(data, TOP_LEVEL_KEYS, "state")

        schema_version = data["schema_version"]
        if type(schema_version) is not int or schema_version != STATE_SCHEMA_VERSION:
            raise ValueError(f"field 'schema_version' must be {STATE_SCHEMA_VERSION}, got {schema_version!r}")

        raw_tracks = data["tracks"]
        if not isinstance(raw_tracks, list):
            raise ValueError(f"field 'tracks' must be a list, got {type(raw_tracks).__name__}")
        tracks = [TrackStateJson.from_dict(raw_track, index) for index, raw_track in enumerate(raw_tracks)]

        labels = [track.track_id for track in tracks]
        duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
        if duplicate_labels:
            raise ValueError(f"field 'tracks' has duplicate track_id {duplicate_labels}")

        counts_by_class = _validate_counts_by_class(data["counts_by_class"], tracks)

        omitted_track_count = _require_int(data, "omitted_track_count", "state")
        if omitted_track_count < 0:
            raise ValueError(f"field 'omitted_track_count' must be >= 0, got {omitted_track_count}")

        return cls(
            schema_version=STATE_SCHEMA_VERSION,
            time_sec=_require_number(data, "time_sec", "state"),
            drone=DroneStateJson.from_dict(data["drone"]),
            tracks=_sort_tracks(tracks),
            counts_by_class=counts_by_class,
            omitted_track_count=omitted_track_count,
        )


def build_state_json(
    tracks: Sequence[Track], telemetry: Telemetry, max_tracks: int = DEFAULT_MAX_TRACKS
) -> StateJson:
    drone = DroneStateJson(
        north_m=_round_value(telemetry.north_m, POSITION_DECIMALS),
        east_m=_round_value(telemetry.east_m, POSITION_DECIMALS),
        down_m=_round_value(telemetry.down_m, POSITION_DECIMALS),
        altitude_m=_round_value(telemetry.altitude_m, POSITION_DECIMALS),
        roll_deg=_round_value(telemetry.roll_deg, ANGLE_DECIMALS),
        pitch_deg=_round_value(telemetry.pitch_deg, ANGLE_DECIMALS),
        yaw_deg=_round_value(telemetry.yaw_deg, ANGLE_DECIMALS),
        gimbal_pitch_deg=_round_value(telemetry.gimbal_pitch_deg, ANGLE_DECIMALS),
        gimbal_yaw_deg=_round_value(telemetry.gimbal_yaw_deg, ANGLE_DECIMALS),
        battery_pct=_round_value(telemetry.battery_pct, BATTERY_DECIMALS),
        flight_mode=telemetry.flight_mode,
    )

    track_states = [_convert_track(track) for track in tracks]

    full_state = StateJson(
        schema_version=STATE_SCHEMA_VERSION,
        time_sec=_round_value(telemetry.time_sec, TIME_DECIMALS),
        drone=drone,
        tracks=_sort_tracks(track_states),
        counts_by_class=_count_by_class(track_states),
        omitted_track_count=0,
    )
    truncated_state = truncate_state(full_state, max_tracks)

    return truncated_state


def truncate_state(state: StateJson, max_tracks: int) -> StateJson:
    """우선순위 상위 max_tracks 개만 남긴다. 결정적이어야 같은 상태가 같은 프롬프트가 된다.

    우선순위: 지금 보이는 것(is_active) → 최근에 본 것(last_seen_sec) → 확실한 것(mean_confidence)
    → 작은 ID. 잘린 track 은 개수만 omitted_track_count 에 남겨 모델이 "N 개 더" 라고 쓸 수 있게 한다.
    """
    if max_tracks < 1:
        raise ValueError(f"max_tracks must be >= 1, got {max_tracks}")

    if len(state.tracks) <= max_tracks:
        return state

    prioritized = sorted(state.tracks, key=_get_priority_key)
    kept_tracks = _sort_tracks(prioritized[:max_tracks])
    omitted_count = len(state.tracks) - max_tracks

    return replace(
        state,
        tracks=kept_tracks,
        counts_by_class=_count_by_class(kept_tracks),
        omitted_track_count=state.omitted_track_count + omitted_count,
    )


# --- 모듈 내부 도우미 ---


def _convert_track(track: Track) -> TrackStateJson:
    if not CLASS_NAME_PATTERN.match(track.class_name):
        raise ValueError(f"track {track.track_id} class_name {track.class_name!r} is not allowed in state JSON")

    return TrackStateJson(
        track_id=format_track_label(track.track_id),
        class_name=track.class_name,
        north_m=_round_value(track.north_m, POSITION_DECIMALS),
        east_m=_round_value(track.east_m, POSITION_DECIMALS),
        first_seen_sec=_round_value(track.first_seen_sec, TIME_DECIMALS),
        last_seen_sec=_round_value(track.last_seen_sec, TIME_DECIMALS),
        observation_count=track.observation_count,
        mean_confidence=_round_value(track.mean_confidence, CONFIDENCE_DECIMALS),
        is_active=track.is_active,
    )


def _round_value(value: float, decimals: int) -> float:
    # round(-0.04, 1) 은 -0.0 이 되는데, 0.0 을 더하면 +0.0 으로 바뀐다 ("-0.0" 이 프롬프트에 새지 않게)
    return round(value, decimals) + 0.0


def _get_label_number(label: str) -> int:
    # 정렬용 비공개 키. 라벨 형식은 from_dict 와 format_track_label 이 보장한다
    return int(label[1:])


def _sort_tracks(tracks: Sequence[TrackStateJson]) -> tuple[TrackStateJson, ...]:
    return tuple(sorted(tracks, key=lambda track: _get_label_number(track.track_id)))


def _get_priority_key(track: TrackStateJson) -> tuple:
    return (
        not track.is_active,
        -track.last_seen_sec,
        -track.mean_confidence,
        _get_label_number(track.track_id),
    )


def _count_by_class(tracks: Sequence[TrackStateJson]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for track in tracks:
        counts[track.class_name] = counts.get(track.class_name, 0) + 1
    return dict(sorted(counts.items()))


def _validate_counts_by_class(raw_counts: object, tracks: Sequence[TrackStateJson]) -> dict[str, int]:
    if not isinstance(raw_counts, dict):
        raise ValueError(f"field 'counts_by_class' must be a dict, got {type(raw_counts).__name__}")

    for class_name, count in raw_counts.items():
        if not isinstance(class_name, str):
            raise ValueError(f"field 'counts_by_class' keys must be str, got {class_name!r}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"field 'counts_by_class.{class_name}' must be int >= 0, got {count!r}")

    # 0 개 항목은 있어도 없어도 같은 뜻으로 본다
    declared_counts = {name: count for name, count in raw_counts.items() if count > 0}
    expected_counts = _count_by_class(tracks)
    if declared_counts != expected_counts:
        raise ValueError(f"field 'counts_by_class' {raw_counts} does not match tracks {expected_counts}")

    return expected_counts


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
