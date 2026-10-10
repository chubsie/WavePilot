"""return_home 모드 — 복귀 고도로 홈 위치까지 날아가 착륙한다 (RM-03, D-01..D-04).

GoTo(home, return_altitude_m) 로 이동하고(D-03 — 시뮬레이터의 복귀 전용 명령은 쓰지 않는다),
수평·수직 모두 arrival_tolerance_m 안에 들어오면 Land 를 보낸다. success 는 grounded 가 된 뒤에만 (D-01).
timeout 은 보고만 하고 아무 명령도 취소하지 않는다 — 드론은 마지막 명령대로 계속 홈으로 간다 (D-04).
명령이 거부되면 failure.
"""

import math

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import Command, FlightControl, GoTo, Land, Telemetry
from modules.rule_mode.interface import ModeParams, ModeTick, RuleModeSettings

RETURN_HOME_TIMEOUT_SEC = 120.0  # return_home 에는 timeout_sec 파라미터가 없다

PHASE_FLYING = "flying"
PHASE_LANDING = "landing"


class ReturnHomeMode:
    rule_mode = "return_home"

    def __init__(
        self,
        params: ModeParams,
        settings: RuleModeSettings,
        *,
        timeout_sec: float = RETURN_HOME_TIMEOUT_SEC,
    ) -> None:
        self._home_north_m = _require_finite_param(params, "home_north_m", self.rule_mode)
        self._home_east_m = _require_finite_param(params, "home_east_m", self.rule_mode)
        self._return_altitude_m = _require_finite_param(params, "return_altitude_m", self.rule_mode)
        if self._return_altitude_m <= 0:
            raise ValueError(f"return_home param 'return_altitude_m' must be > 0, got {self._return_altitude_m!r}")

        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError(f"timeout_sec must be a finite number > 0, got {timeout_sec!r}")

        self._timeout_sec = timeout_sec
        self._arrival_tolerance_m = settings.arrival_tolerance_m
        self._start_sec: float | None = None
        self._phase = PHASE_FLYING
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            raise RuntimeError("return_home mode already finished")

        if self._start_sec is None:
            self._start_sec = tick.now_sec
            go_to = GoTo(self._home_north_m, self._home_east_m, self._return_altitude_m)
            failure = self._send_or_fail(control, go_to)
            if failure is not None:
                return failure

        # 취소 명령은 보내지 않는다 (D-04)
        if tick.now_sec - self._start_sec >= self._timeout_sec:
            return self._finish("timeout")

        if self._phase == PHASE_FLYING and self._is_over_home(tick.telemetry):
            failure = self._send_or_fail(control, Land())
            if failure is not None:
                return failure
            self._phase = PHASE_LANDING
        elif self._phase == PHASE_LANDING and tick.telemetry.flight_mode == "grounded":
            return self._finish("success")

        return None

    def _is_over_home(self, telemetry: Telemetry) -> bool:
        horizontal_m = math.hypot(telemetry.north_m - self._home_north_m, telemetry.east_m - self._home_east_m)
        vertical_m = abs(telemetry.altitude_m - self._return_altitude_m)

        return horizontal_m <= self._arrival_tolerance_m and vertical_m <= self._arrival_tolerance_m

    def _send_or_fail(self, control: FlightControl, command: Command) -> ModeResult | None:
        ack = control.send_command(command)
        if ack.accepted:
            return None

        return self._finish("failure")

    def _finish(self, outcome: str) -> ModeResult:
        self._is_finished = True

        return ModeResult(self.rule_mode, outcome, None)


def _require_finite_param(params: ModeParams, key: str, rule_mode: str) -> float:
    value = params.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{rule_mode} param {key!r} must be a finite number, got {value!r}")

    return float(value)
