"""비행 제어 인터페이스 — 텔레메트리(시뮬레이션 GPS 포함)와 상위 명령 8종.

좌표·단위 규약(자세한 내용은 modules/perception/interface/geometry.py):
- 위치는 로컬 NED [m]. 지면 down = 0 이므로 고도 = -down_m.
- 자세는 ZYX Euler [deg]: +roll 우측 하강, +pitch 기수 상승, +yaw 북 기준 시계방향.
- 짐벌 각은 기체(body) 기준 [deg]: gimbal_pitch_deg 아래로 음수, gimbal_yaw_deg 정면 0.
- height_above_ground_m 은 아래쪽 거리 센서(레인지파인더)의 선택적 AGL [m]: 드론 바로 밑 지면까지 거리, 없으면 None.

명령은 의도적으로 상위 수준 8종만 둔다 — 이착륙·이동·귀환·속도·yaw·짐벌·하트.
자세·모터 제어는 비행 제어기(ArduPilot 등) 몫이고, LLM 레이어는 명령을 직접 보낼 권한이 없다(Safety by design).
하트(FireHeart)는 별도 HeartControl 이 아니라 FlightControl 명령이다(D-08). 모든 명령은 "kind" 로 구분해 직렬화한다.
"""

import math
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

FLIGHT_MODES = ("grounded", "hover", "moving", "landing", "returning")

# 시뮬레이션 홈(NED 원점)의 GPS 좌표 — 서울시청 부근
HOME_LATITUDE_DEG = 37.5665
HOME_LONGITUDE_DEG = 126.9780
HOME_ALTITUDE_M = 0.0
METERS_PER_DEG_LATITUDE = 111_320.0


@dataclass(frozen=True, slots=True)
class GpsFix:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float

    def to_dict(self) -> dict:
        return {
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "altitude_m": self.altitude_m,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GpsFix":
        _require_dict(data, "gps")

        return cls(
            latitude_deg=_require_number(data, "latitude_deg"),
            longitude_deg=_require_number(data, "longitude_deg"),
            altitude_m=_require_number(data, "altitude_m"),
        )


HOME_GPS_FIX = GpsFix(HOME_LATITUDE_DEG, HOME_LONGITUDE_DEG, HOME_ALTITUDE_M)


def ned_to_gps(north_m: float, east_m: float, down_m: float, home: GpsFix = HOME_GPS_FIX) -> GpsFix:
    """로컬 NED [m] 를 GPS 로 바꾼다.

    평면 지구 근사다 — 홈에서 수 km 이내(시뮬레이터 맵 크기)에서만 오차가 무시할 만하고,
    극지방·장거리에서는 맞지 않는다. 경도 1° 의 길이는 홈 위도의 cos 로 보정한다.
    """
    meters_per_deg_longitude = METERS_PER_DEG_LATITUDE * math.cos(math.radians(home.latitude_deg))

    return GpsFix(
        latitude_deg=home.latitude_deg + north_m / METERS_PER_DEG_LATITUDE,
        longitude_deg=home.longitude_deg + east_m / meters_per_deg_longitude,
        altitude_m=home.altitude_m - down_m,
    )


@dataclass(frozen=True, slots=True)
class Telemetry:
    time_sec: float
    north_m: float
    east_m: float
    down_m: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    gimbal_pitch_deg: float
    gimbal_yaw_deg: float
    battery_pct: float
    flight_mode: str
    gps: GpsFix | None = None
    # 아래쪽 거리 센서(레인지파인더)가 잰 드론~바로 밑 지면 거리 [m], 범위 밖·센서 없음이면 None
    # 지면 투영 평면 down = down_m + 이 값 (D-09)
    height_above_ground_m: float | None = None

    @property
    def altitude_m(self) -> float:
        return -self.down_m

    def to_dict(self) -> dict:
        return {
            "time_sec": self.time_sec,
            "north_m": self.north_m,
            "east_m": self.east_m,
            "down_m": self.down_m,
            "roll_deg": self.roll_deg,
            "pitch_deg": self.pitch_deg,
            "yaw_deg": self.yaw_deg,
            "gimbal_pitch_deg": self.gimbal_pitch_deg,
            "gimbal_yaw_deg": self.gimbal_yaw_deg,
            "battery_pct": self.battery_pct,
            "flight_mode": self.flight_mode,
            "gps": self.gps.to_dict() if self.gps is not None else None,
            "height_above_ground_m": self.height_above_ground_m,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Telemetry":
        _require_dict(data, "telemetry")

        flight_mode = _require_str(data, "flight_mode")
        if flight_mode not in FLIGHT_MODES:
            raise ValueError(f"field 'flight_mode' must be one of {FLIGHT_MODES}, got {flight_mode!r}")

        gps = _parse_optional_gps(data.get("gps"))
        height_above_ground_m = _parse_optional_agl(data.get("height_above_ground_m"))

        return cls(
            time_sec=_require_number(data, "time_sec"),
            north_m=_require_number(data, "north_m"),
            east_m=_require_number(data, "east_m"),
            down_m=_require_number(data, "down_m"),
            roll_deg=_require_number(data, "roll_deg"),
            pitch_deg=_require_number(data, "pitch_deg"),
            yaw_deg=_require_number(data, "yaw_deg"),
            gimbal_pitch_deg=_require_number(data, "gimbal_pitch_deg"),
            gimbal_yaw_deg=_require_number(data, "gimbal_yaw_deg"),
            battery_pct=_require_number(data, "battery_pct"),
            flight_mode=flight_mode,
            gps=gps,
            height_above_ground_m=height_above_ground_m,
        )


def _parse_optional_gps(value: object) -> GpsFix | None:
    # gps 키가 없거나 null 이면 구버전 텔레메트리로 보고 받아들인다
    if value is None:
        return None
    if isinstance(value, dict):
        return GpsFix.from_dict(value)

    raise ValueError(f"field 'gps' must be a dict or null, got {value!r}")


def _parse_optional_agl(value: object) -> float | None:
    # 키가 없거나 null 이면 구버전 텔레메트리 또는 범위 밖으로 보고 받아들인다
    if value is None:
        return None
    if not _is_number(value):
        raise ValueError(f"field 'height_above_ground_m' must be a number or null, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"field 'height_above_ground_m' must be finite and >= 0, got {value!r}")

    return float(value)


@dataclass(frozen=True, slots=True)
class TakeOff:
    KIND: ClassVar[str] = "take_off"
    altitude_m: float

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "altitude_m": self.altitude_m}


@dataclass(frozen=True, slots=True)
class GoTo:
    KIND: ClassVar[str] = "go_to"
    north_m: float
    east_m: float
    altitude_m: float

    def to_dict(self) -> dict:
        return {
            "kind": self.KIND,
            "north_m": self.north_m,
            "east_m": self.east_m,
            "altitude_m": self.altitude_m,
        }


@dataclass(frozen=True, slots=True)
class Land:
    KIND: ClassVar[str] = "land"

    def to_dict(self) -> dict:
        return {"kind": self.KIND}


@dataclass(frozen=True, slots=True)
class ReturnToHome:
    KIND: ClassVar[str] = "return_to_home"

    def to_dict(self) -> dict:
        return {"kind": self.KIND}


# 속도·각도 상한 클램프는 여기서 하지 않는다 — 브라우저 시뮬레이터가 클램프한다(X-02, X-03)
@dataclass(frozen=True, slots=True)
class SetVelocity:
    """NED 속도 [m/s] 를 duration_sec 동안 유지한다."""

    KIND: ClassVar[str] = "set_velocity"
    north_mps: float
    east_mps: float
    down_mps: float
    duration_sec: float

    def __post_init__(self) -> None:
        if self.duration_sec <= 0:
            raise ValueError(f"field 'duration_sec' must be > 0, got {self.duration_sec!r}")

    def to_dict(self) -> dict:
        return {
            "kind": self.KIND,
            "north_mps": self.north_mps,
            "east_mps": self.east_mps,
            "down_mps": self.down_mps,
            "duration_sec": self.duration_sec,
        }


@dataclass(frozen=True, slots=True)
class SetYaw:
    """절대 heading [deg] — NED 북 기준 시계방향."""

    KIND: ClassVar[str] = "set_yaw"
    yaw_deg: float

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "yaw_deg": self.yaw_deg}


@dataclass(frozen=True, slots=True)
class SetGimbal:
    """기체 기준 짐벌 각 [deg] — pitch 는 아래가 음수, yaw 는 정면 0."""

    KIND: ClassVar[str] = "set_gimbal"
    pitch_deg: float
    yaw_deg: float

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "pitch_deg": self.pitch_deg, "yaw_deg": self.yaw_deg}


@dataclass(frozen=True, slots=True)
class FireHeart:
    """track_id 대상에게 하트를 쏜다."""

    KIND: ClassVar[str] = "fire_heart"
    track_id: int

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "track_id": self.track_id}


# PEP 695 `type` 대신 일반 Union 으로 둔다 — isinstance(x, Command) 가 동작해야 수신측이 검사할 수 있다
Command = TakeOff | GoTo | Land | ReturnToHome | SetVelocity | SetYaw | SetGimbal | FireHeart


def command_from_dict(data: dict) -> Command:
    """화이트리스트 8종만 복원한다. 모르는 kind 는 ValueError (T-01-02, T-02-01)."""
    _require_dict(data, "command")

    kind = _require_str(data, "kind")
    if kind == TakeOff.KIND:
        return TakeOff(altitude_m=_require_number(data, "altitude_m"))
    if kind == GoTo.KIND:
        return GoTo(
            north_m=_require_number(data, "north_m"),
            east_m=_require_number(data, "east_m"),
            altitude_m=_require_number(data, "altitude_m"),
        )
    if kind == Land.KIND:
        return Land()
    if kind == ReturnToHome.KIND:
        return ReturnToHome()
    if kind == SetVelocity.KIND:
        return SetVelocity(
            north_mps=_require_number(data, "north_mps"),
            east_mps=_require_number(data, "east_mps"),
            down_mps=_require_number(data, "down_mps"),
            duration_sec=_require_number(data, "duration_sec"),
        )
    if kind == SetYaw.KIND:
        return SetYaw(yaw_deg=_require_number(data, "yaw_deg"))
    if kind == SetGimbal.KIND:
        return SetGimbal(
            pitch_deg=_require_number(data, "pitch_deg"),
            yaw_deg=_require_number(data, "yaw_deg"),
        )
    if kind == FireHeart.KIND:
        return FireHeart(track_id=_require_int(data, "track_id"))

    raise ValueError(f"field 'kind' has unknown command kind {kind!r}")


@dataclass(frozen=True, slots=True)
class CommandAck:
    accepted: bool
    reason: str


@runtime_checkable
class FlightControl(Protocol):
    def get_telemetry(self) -> Telemetry: ...

    def send_command(self, command: Command) -> CommandAck: ...


# --- 모듈 내부 검증 도우미 ---


def _is_number(value: object) -> bool:
    # bool 은 int 의 하위 타입이라 따로 막는다
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_dict(data: object, name: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a dict, got {type(data).__name__}")


def _require_key(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"missing field '{key}'")
    return data[key]


def _require_number(data: dict, key: str) -> float:
    value = _require_key(data, key)
    if not _is_number(value):
        raise ValueError(f"field '{key}' must be a number, got {value!r}")
    return float(value)


def _require_int(data: dict, key: str) -> int:
    value = _require_key(data, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"field '{key}' must be an int, got {value!r}")
    return value


def _require_str(data: dict, key: str) -> str:
    value = _require_key(data, key)
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be str, got {value!r}")
    return value
