"""MANUAL/AI/RETURN 상태 기계 (C4, MIS-02, MIS-03).

순수 상태 객체다 — 로그·명령을 내지 않는다. 호출자(임무 루프)가 돌려받은 ModeTransition 을
로그 `mode_transition` 으로 남기고 비행 명령을 정한다(D-11).

전환 규칙 (현재 모드 × 입력 → 다음 모드):
    MANUAL + AI 요청              → AI      (operator)
    AI     + 요청 끊김            → MANUAL  (operator, D-07)
    AI     + 안전 판정            → RETURN  (판정 사유, D-06)
    RETURN + 요청 끊김            → MANUAL  (operator, D-09)
    RETURN + 요청 유지·새 판정    → 변화 없음 (D-04, D-06)
    MANUAL + 안전 판정            → 변화 없음 (조종자 몫 — AI 재개 틱에 다시 판정, D-10)
"""

import math
from dataclasses import dataclass

from modules.mission.safety import SAFETY_REASONS, SafetyVerdict

MODE_MANUAL = "MANUAL"
MODE_AI = "AI"
MODE_RETURN = "RETURN"
MODES = (MODE_MANUAL, MODE_AI, MODE_RETURN)

REASON_OPERATOR = "operator"

_TRANSITION_REASONS = (REASON_OPERATOR, *SAFETY_REASONS)


def _require_mode(name: str, value: object) -> None:
    if value not in MODES:
        raise ValueError(f"field '{name}' must be one of {MODES}, got {value!r}")


@dataclass(frozen=True, slots=True)
class ModeTransition:
    time_sec: float
    from_mode: str
    to_mode: str
    reason: str  # "operator" 또는 SAFETY_REASONS
    detail: str = ""

    def __post_init__(self) -> None:
        is_finite_number = (
            isinstance(self.time_sec, (int, float))
            and not isinstance(self.time_sec, bool)
            and math.isfinite(self.time_sec)
        )
        if not is_finite_number:
            raise ValueError(f"field 'time_sec' must be a finite number, got {self.time_sec!r}")

        _require_mode("from_mode", self.from_mode)
        _require_mode("to_mode", self.to_mode)

        if self.reason not in _TRANSITION_REASONS:
            raise ValueError(f"field 'reason' must be one of {_TRANSITION_REASONS}, got {self.reason!r}")


class ModeStateMachine:
    def __init__(self, initial_mode: str = MODE_MANUAL) -> None:
        _require_mode("initial_mode", initial_mode)
        self._mode = initial_mode

    @property
    def mode(self) -> str:
        return self._mode

    def on_operator(self, now_sec: float, ai_requested: bool) -> ModeTransition | None:
        if self._mode == MODE_MANUAL and ai_requested:
            return self._transition(now_sec, MODE_AI, REASON_OPERATOR)

        if self._mode in (MODE_AI, MODE_RETURN) and not ai_requested:
            return self._transition(now_sec, MODE_MANUAL, REASON_OPERATOR)

        return None

    def on_safety(self, now_sec: float, verdict: SafetyVerdict | None) -> ModeTransition | None:
        if self._mode == MODE_AI and verdict is not None:
            return self._transition(now_sec, MODE_RETURN, verdict.reason, verdict.detail)

        return None

    def _transition(self, now_sec: float, to_mode: str, reason: str, detail: str = "") -> ModeTransition:
        transition = ModeTransition(now_sec, self._mode, to_mode, reason, detail)
        self._mode = to_mode
        return transition
