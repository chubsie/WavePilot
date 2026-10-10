"""헤드리스 kinematic 가짜 비행 제어 — 브라우저 없이 rule_mode 를 10 Hz 로 돌려 보기 위한 비행 모델.

tools/visualizer/sim_link.js 의 createAutopilot 의미를 따른다: 지면은 down_m = 0 의 평지이고,
수평은 max_speed_mps 로 직선 이동, 수직은 climb_rate_mps 로 이동하며, 목표에 가까워지면 hover 로 들어간다.
시간은 advance(dt_sec) 로만 흐른다 — 호출하는 쪽(테스트·미션 루프)이 시계를 쥔다.

reject_commands=True 면 모든 명령을 거부해 링크 끊김/수동 조종 같은 상황을 흉내 낸다 (D-04 failure 경로).
"""

import math

from modules.flight_control.interface import (
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

GOAL_REACHED_HORIZONTAL_M = 1.0
GOAL_REACHED_VERTICAL_M = 0.5
LAND_SPEED_MPS = 1.0
RTH_MIN_ALTITUDE_M = 10.0
FULL_BATTERY_PCT = 100.0
# 600 s(10 분)에 100 % 선형 소모
BATTERY_DRAIN_PCT_PER_SEC = 100.0 / 600.0
GIMBAL_PITCH_MIN_DEG = -90.0
GIMBAL_PITCH_MAX_DEG = 0.0
GIMBAL_YAW_LIMIT_DEG = 90.0
DEFAULT_GIMBAL_PITCH_DEG = -20.0


class KinematicFlightControl:
    def __init__(
        self,
        start_time_sec: float = 0.0,
        max_speed_mps: float = 5.0,
        climb_rate_mps: float = 2.0,
        yaw_rate_deg_per_sec: float = 90.0,
        start_north_m: float = 0.0,
        start_east_m: float = 0.0,
        start_altitude_m: float = 0.0,
        start_yaw_deg: float = 0.0,
        gimbal_pitch_deg: float = DEFAULT_GIMBAL_PITCH_DEG,
    ) -> None:
        self.reject_commands = False

        self._start_time_sec = start_time_sec
        self._elapsed_sec = 0.0
        self._max_speed_mps = max_speed_mps
        self._climb_rate_mps = climb_rate_mps
        self._yaw_rate_deg_per_sec = yaw_rate_deg_per_sec

        self._north_m = start_north_m
        self._east_m = start_east_m
        self._down_m = -start_altitude_m
        self._yaw_deg = self._wrap_yaw_deg(start_yaw_deg)
        self._gimbal_pitch_deg = gimbal_pitch_deg
        self._gimbal_yaw_deg = 0.0
        self._flight_mode = "grounded" if start_altitude_m == 0 else "hover"

        self._goal: tuple[float, float, float] | None = None  # (north, east, down)
        self._is_landing = False
        self._velocity: tuple[float, float, float, float] | None = None  # (north, east, down mps, 종료 시각)
        self._yaw_target_deg: float | None = None
        self._commands_received: list[tuple[float, Command]] = []

    @property
    def now_sec(self) -> float:
        return self._start_time_sec + self._elapsed_sec

    @property
    def commands_received(self) -> tuple[tuple[float, Command], ...]:
        """수락된 명령만 (받은 시각, 명령) 으로 돌려준다 — MockFlightControl 과 같은 모양."""
        return tuple(self._commands_received)

    def send_command(self, command: Command) -> CommandAck:
        if self.reject_commands:
            return CommandAck(False, "commands rejected")

        self._commands_received.append((self.now_sec, command))
        self._apply_command(command)

        return CommandAck(True, f"kinematic applied {command.KIND}")

    def advance(self, dt_sec: float) -> None:
        if not math.isfinite(dt_sec) or dt_sec <= 0:
            raise ValueError(f"dt_sec must be a finite number > 0, got {dt_sec!r}")

        previous_sec = self.now_sec
        self._elapsed_sec += dt_sec

        self._step_yaw(dt_sec)
        if self._is_landing:
            self._step_landing(dt_sec)
        elif self._velocity is not None:
            self._step_velocity(previous_sec, dt_sec)
        elif self._goal is not None:
            self._step_goal(dt_sec)

        # 평지 — 지면 아래로 내려가지 않는다
        self._down_m = min(0.0, self._down_m)

    def get_telemetry(self) -> Telemetry:
        battery_pct = FULL_BATTERY_PCT - self._elapsed_sec * BATTERY_DRAIN_PCT_PER_SEC

        return Telemetry(
            time_sec=self.now_sec,
            north_m=self._north_m,
            east_m=self._east_m,
            down_m=self._down_m,
            roll_deg=0.0,
            pitch_deg=0.0,
            yaw_deg=self._yaw_deg,
            gimbal_pitch_deg=self._gimbal_pitch_deg,
            gimbal_yaw_deg=self._gimbal_yaw_deg,
            battery_pct=max(0.0, min(FULL_BATTERY_PCT, battery_pct)),
            flight_mode=self._flight_mode,
            gps=ned_to_gps(self._north_m, self._east_m, self._down_m),
            height_above_ground_m=max(0.0, -self._down_m),  # 평지 — 지면은 down 0
        )

    def _apply_command(self, command: Command) -> None:
        if isinstance(command, TakeOff):
            self._start_goal(self._north_m, self._east_m, -command.altitude_m, "moving")
        elif isinstance(command, GoTo):
            self._start_goal(command.north_m, command.east_m, -command.altitude_m, "moving")
        elif isinstance(command, Land):
            self._clear_motion()
            self._is_landing = True
            self._flight_mode = "landing"
        elif isinstance(command, ReturnToHome):
            return_altitude_m = max(-self._down_m, RTH_MIN_ALTITUDE_M)
            self._start_goal(0.0, 0.0, -return_altitude_m, "returning")
        elif isinstance(command, SetVelocity):
            self._clear_motion()
            end_sec = self.now_sec + command.duration_sec
            self._velocity = (command.north_mps, command.east_mps, command.down_mps, end_sec)
            self._flight_mode = "moving"
        elif isinstance(command, SetYaw):
            self._yaw_target_deg = self._wrap_yaw_deg(command.yaw_deg)
        elif isinstance(command, SetGimbal):
            self._gimbal_pitch_deg = max(GIMBAL_PITCH_MIN_DEG, min(GIMBAL_PITCH_MAX_DEG, command.pitch_deg))
            self._gimbal_yaw_deg = max(-GIMBAL_YAW_LIMIT_DEG, min(GIMBAL_YAW_LIMIT_DEG, command.yaw_deg))
        elif isinstance(command, FireHeart):
            pass  # 수락만 하고 아무것도 하지 않는다 (발사는 Phase 4)

    def _start_goal(self, north_m: float, east_m: float, down_m: float, flight_mode: str) -> None:
        self._clear_motion()
        self._goal = (north_m, east_m, down_m)
        self._flight_mode = flight_mode

    def _clear_motion(self) -> None:
        self._goal = None
        self._is_landing = False
        self._velocity = None

    def _step_goal(self, dt_sec: float) -> None:
        assert self._goal is not None
        goal_north_m, goal_east_m, goal_down_m = self._goal

        delta_north_m = goal_north_m - self._north_m
        delta_east_m = goal_east_m - self._east_m
        horizontal_m = math.hypot(delta_north_m, delta_east_m)
        if horizontal_m > 0:
            step_m = min(horizontal_m, self._max_speed_mps * dt_sec)
            self._north_m += delta_north_m / horizontal_m * step_m
            self._east_m += delta_east_m / horizontal_m * step_m

        delta_down_m = goal_down_m - self._down_m
        vertical_step_m = min(abs(delta_down_m), self._climb_rate_mps * dt_sec)
        self._down_m += math.copysign(vertical_step_m, delta_down_m)

        remaining_horizontal_m = math.hypot(goal_north_m - self._north_m, goal_east_m - self._east_m)
        remaining_vertical_m = abs(goal_down_m - self._down_m)
        if remaining_horizontal_m <= GOAL_REACHED_HORIZONTAL_M and remaining_vertical_m <= GOAL_REACHED_VERTICAL_M:
            self._goal = None
            self._flight_mode = "hover"

    def _step_landing(self, dt_sec: float) -> None:
        self._down_m = min(0.0, self._down_m + LAND_SPEED_MPS * dt_sec)
        if self._down_m == 0.0:
            self._clear_motion()
            self._flight_mode = "grounded"

    def _step_velocity(self, previous_sec: float, dt_sec: float) -> None:
        assert self._velocity is not None
        north_mps, east_mps, down_mps, end_sec = self._velocity

        # 종료 시각을 넘은 부분은 적분하지 않는다
        moving_sec = max(0.0, min(dt_sec, end_sec - previous_sec))
        self._north_m += north_mps * moving_sec
        self._east_m += east_mps * moving_sec
        self._down_m += down_mps * moving_sec

        if self.now_sec >= end_sec:
            self._velocity = None
            self._flight_mode = "hover"

    def _step_yaw(self, dt_sec: float) -> None:
        if self._yaw_target_deg is None:
            return

        error_deg = self._wrap_yaw_deg(self._yaw_target_deg - self._yaw_deg)
        max_step_deg = self._yaw_rate_deg_per_sec * dt_sec
        if abs(error_deg) <= max_step_deg:
            self._yaw_deg = self._yaw_target_deg
            self._yaw_target_deg = None
        else:
            self._yaw_deg = self._wrap_yaw_deg(self._yaw_deg + math.copysign(max_step_deg, error_deg))

    @staticmethod
    def _wrap_yaw_deg(yaw_deg: float) -> float:
        """(-180, 180] 로 감싼다."""
        wrapped_deg = math.fmod(yaw_deg, 360.0)
        if wrapped_deg > 180.0:
            wrapped_deg -= 360.0
        elif wrapped_deg <= -180.0:
            wrapped_deg += 360.0

        return wrapped_deg
