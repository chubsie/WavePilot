"""d1 먼저, 실패하면 규칙 — 조합 선택기 (SEL-03).

select 는 Exception 을 밖으로 내지 않는다. d1 실패 사유는 결과의 fallback_reason 으로 남고(D-05),
d1 시도의 network_rtt·orin 추정은 대체 결과에도 유지된다(total_ms 에는 규칙 시간을 더한다).
D1SelectionError 가 아닌 예상 밖 예외는 선택기 코드 버그로 보고 internal_error 로 남기며 traceback 을 로그에 쓴다.
규칙 선택기마저 실패하면 비상 hold 결과를 만든다 — 제자리 정지가 가장 보수적이고, 복귀 판단은 Phase 5 안전 감시 몫이다.

회로 차단: 아래 두 경우 circuit_open_sec 동안 d1 을 부르지 않고 circuit_open 으로 대체한다(CONTRACTS CIRCUIT_OPEN_SEC).
- unavailable(연결 거부·서버 내려감) 1회 — 서버가 없는 동안 사건마다 시도해 로그가 넘치는 것을 막는다.
- timeout 이 연속 CIRCUIT_TIMEOUT_STREAK 회 — 꺼진 호스트·끊긴 Wi-Fi 는 거부 대신 timeout 으로 보여,
  열지 않으면 사건마다 timeout 만큼 기다린다. 성공이나 timeout 이 아닌 사유가 나오면 연속 횟수를 0 으로 되돌린다.
low_confidence·out_of_menu·bad_response 는 서버가 살아 있다는 뜻이라 회로를 열지 않는다.
KeyboardInterrupt 같은 BaseException 은 잡지 않는다.
"""

import dataclasses
import logging
import time
from collections.abc import Callable

from modules.decision.d1_selector import D1SelectionError, D1Selector
from modules.decision.interface import (
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_INTERNAL_ERROR,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    SOURCE_RULE,
    Latency,
    SelectionEvent,
    SelectionResult,
    SelectionState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector

EMERGENCY_RULE_MODE = "hold"
EMERGENCY_CONFIDENCE = 1.0  # 보기가 1개면 서버도 1.0 을 준다 (RESEARCH §2.2)
CIRCUIT_TIMEOUT_STREAK = 3  # ASSUMED — 연속 timeout 이 이만큼이면 회로를 연다. 실측 timeout 빈도로 재조정
MS_PER_SEC = 1000.0

logger = logging.getLogger(__name__)


class FallbackSelector:
    def __init__(
        self,
        primary: D1Selector,
        fallback: RuleSelector,
        circuit_open_sec: float,
        clock: Callable[[], float] = time.monotonic,
        param_defaults: ParamDefaults | None = None,
        circuit_timeout_streak: int = CIRCUIT_TIMEOUT_STREAK,
    ) -> None:
        if circuit_open_sec < 0:
            raise ValueError(f"circuit_open_sec must be >= 0, got {circuit_open_sec!r}")
        if isinstance(circuit_timeout_streak, bool) or not isinstance(circuit_timeout_streak, int) or circuit_timeout_streak < 1:
            raise ValueError(f"circuit_timeout_streak must be an int >= 1, got {circuit_timeout_streak!r}")

        self._primary = primary
        self._fallback = fallback
        self._circuit_open_sec = circuit_open_sec
        self._circuit_timeout_streak = circuit_timeout_streak
        self._clock = clock
        self._param_defaults = param_defaults if param_defaults is not None else ParamDefaults()
        self._circuit_open_until_sec: float | None = None
        self._consecutive_timeout_count = 0

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        started_sec = time.perf_counter()

        if self._is_circuit_open():
            reason = FALLBACK_CIRCUIT_OPEN
            d1_latency = None
        else:
            try:
                d1_result = self._primary.select(state, event)
            except D1SelectionError as error:
                reason = error.reason
                d1_latency = error.latency
            except Exception:
                reason = FALLBACK_INTERNAL_ERROR
                d1_latency = None
                logger.exception("d1 selector raised unexpected error")
            else:
                self._consecutive_timeout_count = 0
                return d1_result

            self._update_circuit(reason)

        try:
            rule_result = self._fallback.select(state, event)
        except Exception:
            logger.exception("rule selector failed, using emergency hold")
            elapsed_ms = (time.perf_counter() - started_sec) * MS_PER_SEC
            return self._build_emergency_result(state, event, reason, elapsed_ms)

        logger.info("selector fallback reason=%s rule_mode=%s", reason, rule_result.rule_mode)
        return dataclasses.replace(
            rule_result,
            source=SOURCE_RULE,
            fallback_reason=reason,
            latency=_merge_latency(d1_latency, rule_result.latency),
        )

    def _is_circuit_open(self) -> bool:
        """열림 기한 안이면 True. 기한이 지났으면 닫고 d1 을 다시 시도하게 한다."""
        if self._circuit_open_until_sec is None:
            return False
        if self._clock() < self._circuit_open_until_sec:
            return True

        self._circuit_open_until_sec = None
        return False

    def _update_circuit(self, reason: str) -> None:
        """d1 실패 사유로 연속 timeout 횟수를 세고, 회로를 열 조건이면 연다."""
        self._consecutive_timeout_count = self._consecutive_timeout_count + 1 if reason == FALLBACK_TIMEOUT else 0

        should_open = reason == FALLBACK_UNAVAILABLE or self._consecutive_timeout_count >= self._circuit_timeout_streak
        if should_open and self._circuit_open_sec > 0:
            self._circuit_open_until_sec = self._clock() + self._circuit_open_sec
            self._consecutive_timeout_count = 0

    def _build_emergency_result(
        self, state: SelectionState, event: SelectionEvent, reason: str, elapsed_ms: float
    ) -> SelectionResult:
        # fill_params 가 실패 원인일 수 있어 다시 부르지 않고 기본값을 직접 쓴다.
        # 키 집합은 PARAM_KEYS_BY_MODE["hold"] 와 같아야 한다(Phase 3 실행기 계약)
        params = {"duration_sec": self._param_defaults.hold_duration_sec}
        options = self._get_emergency_options(state, event)
        probabilities = {option: (1.0 if option == EMERGENCY_RULE_MODE else 0.0) for option in options}

        return SelectionResult(
            rule_mode=EMERGENCY_RULE_MODE,
            params=params,
            confidence=EMERGENCY_CONFIDENCE,
            probabilities=probabilities,
            source=SOURCE_RULE,
            fallback_reason=reason,
            options=options,
            latency=Latency(total_ms=elapsed_ms, orin_processing_ms=None, network_rtt_ms=None),
            input_tokens=None,
        )

    def _get_emergency_options(self, state: SelectionState, event: SelectionEvent) -> tuple[str, ...]:
        # 비상 결과의 options 는 보기 필터 결과(실제 제시 보기)를 쓰고, 계산이 실패하거나 hold 가 없으면 ("hold",) 로 둔다
        try:
            options = filter_options(state, event, self._param_defaults.approach_stop_distance_m)
        except Exception:
            logger.exception("filter_options failed in emergency hold, recording options as hold only")
            return (EMERGENCY_RULE_MODE,)

        if EMERGENCY_RULE_MODE not in options:
            return (EMERGENCY_RULE_MODE,)
        return options


# --- 모듈 내부 도우미 ---


def _merge_latency(d1_latency: Latency | None, rule_latency: Latency) -> Latency:
    """d1 시도의 분해값을 유지하고 total 에 규칙 시간을 더한다."""
    if d1_latency is None:
        return rule_latency

    return Latency(
        total_ms=d1_latency.total_ms + rule_latency.total_ms,
        orin_processing_ms=d1_latency.orin_processing_ms,
        network_rtt_ms=d1_latency.network_rtt_ms,
    )
