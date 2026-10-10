"""비행 제어 인터페이스 — 텔레메트리와 상위 명령 4종.

좌표·단위 규약(자세한 내용은 modules/perception/interface/geometry.py):
- 위치는 로컬 NED [m]. 지면 down = 0 이므로 고도 = -down_m.
- 자세는 ZYX Euler [deg]: +roll 우측 하강, +pitch 기수 상승, +yaw 북 기준 시계방향.
- 짐벌 각은 기체(body) 기준 [deg]: gimbal_pitch_deg 아래로 음수, gimbal_yaw_deg 정면 0.

명령은 의도적으로 상위 수준 4종만 둔다 — 자세·모터 제어는 비행 제어기(ArduPilot 등) 몫이고,
LLM 레이어는 명령을 직접 보낼 권한이 없다(Safety by design). 모든 명령은 "kind" 로 구분해 직렬화한다.
"""

from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

FLIGHT_MODES = ("grounded", "hover", "moving", "landing", "returning")


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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Telemetry":
        _require_dict(data, "telemetry")

        flight_mode = _require_str(data, "flight_mode")
        if flight_mode not in FLIGHT_MODES:
            raise ValueError(f"field 'flight_mode' must be one of {FLIGHT_MODES}, got {flight_mode!r}")

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
        )


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


# PEP 695 `type` 대신 일반 Union 으로 둔다 — isinstance(x, Command) 가 동작해야 수신측이 검사할 수 있다
Command = TakeOff | GoTo | Land | ReturnToHome


def command_from_dict(data: dict) -> Command:
    """화이트리스트 4종만 복원한다. 모르는 kind 는 ValueError (T-01-02)."""
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


def _require_str(data: dict, key: str) -> str:
    value = _require_key(data, key)
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' must be str, got {value!r}")
    return value
