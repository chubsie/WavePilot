"""hold 모드 — 현재 위치·고도에서 duration_sec 동안 호버링한다 (RM-04, D-12).

첫 step 에서 현재 위치로 GoTo 를 한 번만 보내 진행 중이던 이동을 멈추고, duration_sec 이 지나면 success 로 끝난다.
명령이 거부되면 failure (D-04). 끝난 뒤 step 을 다시 부르면 RuntimeError ("정확히 한 번" 계약).
"""

import math

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import FlightControl, GoTo
from modules.rule_mode.interface import ModeParams, ModeTick, RuleModeSettings


class HoldMode:
    rule_mode = "hold"

    def __init__(self, params: ModeParams, settings: RuleModeSettings) -> None:
        duration_sec = _require_finite_param(params, "duration_sec", self.rule_mode)
        if duration_sec <= 0:
            raise ValueError(f"hold param 'duration_sec' must be > 0, got {duration_sec!r}")

        self._duration_sec = duration_sec
        self._settings = settings
        self._start_sec: float | None = None
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            raise RuntimeError("hold mode already finished")

        if self._start_sec is None:
            self._start_sec = tick.now_sec
            telemetry = tick.telemetry
            ack = control.send_command(GoTo(telemetry.north_m, telemetry.east_m, telemetry.altitude_m))
            if not ack.accepted:
                return self._finish("failure")

        if tick.now_sec - self._start_sec >= self._duration_sec:
            return self._finish("success")

        return None

    def _finish(self, outcome: str) -> ModeResult:
        self._is_finished = True

        return ModeResult(self.rule_mode, outcome, None)


def _require_finite_param(params: ModeParams, key: str, rule_mode: str) -> float:
    value = params.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{rule_mode} param {key!r} must be a finite number, got {value!r}")

    return float(value)
