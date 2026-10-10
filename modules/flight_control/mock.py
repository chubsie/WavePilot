"""mock 비행 제어 — 감시 위치에서 호버링하고, 받은 명령을 시각과 함께 기록한다 (MOCK-02, IF-02).

비행 물리는 없다. 명령은 flight_mode 만 바꾸고 위치는 그대로다 — Phase 1 의 목적은
"운용자 명령이 링크를 타고 비행 제어 인터페이스까지 도착했는가"를 기록으로 보이는 것이다.

모든 명령을 받아들인다. 착륙 중 Land, 귀환 중 TakeOff 처럼 실제 비행 제어기라면 거절할 명령도
Phase 1 에서는 의도를 기록하는 데 의미가 있으므로 accepted=True 로 둔다(거절 규칙은 v2 실제 FC 몫).
SetVelocity 는 moving 으로 바꾸고, SetYaw·SetGimbal·FireHeart 는 현재 모드를 유지한 채 기록만 한다.
텔레메트리 GPS 는 호버 위치를 ned_to_gps 로 바꿔 채운다.
"""

import logging
from typing import Protocol

from modules.flight_control.interface.flight_control import (
    Command,
    CommandAck,
    FireHeart,
    GoTo,
    Land,
    ReturnToHome,
    SetGimbal,
    SetVelocity,
    SetYaw,
    TakeOff,
    Telemetry,
    ned_to_gps,
)

HOVER_ALTITUDE_M = 40.0
GIMBAL_PITCH_DEG = -55.0
FULL_BATTERY_PCT = 100.0
# 선형 소모 — 40 s 시나리오 동안 배터리 값이 눈에 띄게(0.8 %p) 변해 텔레메트리가 살아 있음을 보인다
BATTERY_DRAIN_PCT_PER_SEC = 0.02

# 명령 종류 → 받은 뒤의 비행 모드. TakeOff 는 이륙을 마치면 호버로 들어간다
MODE_BY_COMMAND_KIND = {
    TakeOff.KIND: "hover",
    GoTo.KIND: "moving",
    Land.KIND: "landing",
    ReturnToHome.KIND: "returning",
    SetVelocity.KIND: "moving",
}
# 자세·짐벌·하트 명령은 비행 모드를 바꾸지 않는다
MODE_KEEPING_COMMAND_KINDS = frozenset({SetYaw.KIND, SetGimbal.KIND, FireHeart.KIND})

logger = logging.getLogger(__name__)


class ClockReader(Protocol):
    """mock 이 시계에서 읽는 것은 현재 시각뿐이다 — simulation.SimClock 이 이 모양을 만족한다."""

    @property
    def now_sec(self) -> float: ...


class MockFlightControl:
    def __init__(
        self,
        clock: ClockReader,
        hover_north_m: float = 0.0,
        hover_east_m: float = 0.0,
        hover_altitude_m: float = HOVER_ALTITUDE_M,
        gimbal_pitch_deg: float = GIMBAL_PITCH_DEG,
    ) -> None:
        self._clock = clock
        self._hover_north_m = hover_north_m
        self._hover_east_m = hover_east_m
        self._hover_altitude_m = hover_altitude_m
        self._gimbal_pitch_deg = gimbal_pitch_deg
        self._flight_mode = "hover"
        self._commands_received: list[tuple[float, Command]] = []

    @property
    def commands_received(self) -> tuple[tuple[float, Command], ...]:
        """(받은 sim 시각, 명령) 목록. 바깥에서 고칠 수 없게 튜플로 내준다."""
        return tuple(self._commands_received)

    def get_telemetry(self) -> Telemetry:
        time_sec = self._clock.now_sec
        battery_pct = _clamp_battery(FULL_BATTERY_PCT - time_sec * BATTERY_DRAIN_PCT_PER_SEC)
        down_m = -self._hover_altitude_m

        return Telemetry(
            time_sec=time_sec,
            north_m=self._hover_north_m,
            east_m=self._hover_east_m,
            down_m=down_m,
            roll_deg=0.0,
            pitch_deg=0.0,
            yaw_deg=0.0,
            gimbal_pitch_deg=self._gimbal_pitch_deg,
            gimbal_yaw_deg=0.0,
            battery_pct=battery_pct,
            flight_mode=self._flight_mode,
            gps=ned_to_gps(self._hover_north_m, self._hover_east_m, down_m),
        )

    def send_command(self, command: Command) -> CommandAck:
        self._commands_received.append((self._clock.now_sec, command))
        if command.KIND not in MODE_KEEPING_COMMAND_KINDS:
            self._flight_mode = MODE_BY_COMMAND_KIND[command.KIND]

        logger.info("command received: %s -> mode=%s", command.KIND, self._flight_mode)
        return CommandAck(accepted=True, reason=f"mock recorded {command.KIND}")


def _clamp_battery(battery_pct: float) -> float:
    return max(0.0, min(FULL_BATTERY_PCT, battery_pct))
