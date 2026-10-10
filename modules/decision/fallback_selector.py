"""d1 먼저, 실패하면 규칙 — 조합 선택기 (SEL-03).

select 는 Exception 을 밖으로 내지 않는다. d1 실패 사유는 결과의 fallback_reason 으로 남고(D-05),
d1 시도의 network_rtt·orin 추정은 대체 결과에도 유지된다(total_ms 에는 규칙 시간을 더한다).
규칙 선택기마저 실패하면 비상 hold 결과를 만든다 — 제자리 정지가 가장 보수적이고, 복귀 판단은 Phase 5 안전 감시 몫이다.

회로 차단: unavailable(연결 거부·서버 내려감) 뒤에는 circuit_open_sec 동안 d1 을 부르지 않고 circuit_open 으로 대체한다.
연결 거부는 빠르지만, 서버가 없는 동안 사건마다 시도해 로그가 넘치는 것을 막는다(CONTRACTS CIRCUIT_OPEN_SEC).
timeout·low_confidence·out_of_menu·bad_response 는 서버가 살아 있다는 뜻이라 회로를 열지 않는다.
KeyboardInterrupt 같은 BaseException 은 잡지 않는다.
"""

import dataclasses
import logging
import time
from collections.abc import Callable

from modules.decision.d1_selector import D1SelectionError, D1Selector
from modules.decision.interface import (
    FALLBACK_BAD_RESPONSE,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_UNAVAILABLE,
    SOURCE_RULE,
    Latency,
    SelectionEvent,
    SelectionResult,
    SelectionState,
)
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector

EMERGENCY_RULE_MODE = "hold"
EMERGENCY_CONFIDENCE = 1.0  # 보기가 1개면 서버도 1.0 을 준다 (RESEARCH §2.2)
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
    ) -> None:
        if circuit_open_sec < 0:
            raise ValueError(f"circuit_open_sec must be >= 0, got {circuit_open_sec!r}")

        self._primary = primary
        self._fallback = fallback
        self._circuit_open_sec = circuit_open_sec
        self._clock = clock
        self._param_defaults = param_defaults if param_defaults is not None else ParamDefaults()
        self._circuit_open_until_sec: float | None = None

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        started_sec = time.perf_counter()

        if self._is_circuit_open():
            reason = FALLBACK_CIRCUIT_OPEN
            d1_latency = None
        else:
            try:
                return self._primary.select(state, event)
            except D1SelectionError as error:
                reason = error.reason
                d1_latency = error.latency
                if reason == FALLBACK_UNAVAILABLE and self._circuit_open_sec > 0:
                    self._circuit_open_until_sec = self._clock() + self._circuit_open_sec
            except Exception as error:
                reason = FALLBACK_BAD_RESPONSE
                d1_latency = None
                logger.warning("d1 selector raised unexpected %s", type(error).__name__)

        try:
            rule_result = self._fallback.select(state, event)
        except Exception as error:
            logger.error("rule selector raised %s, using emergency hold", type(error).__name__)
            elapsed_ms = (time.perf_counter() - started_sec) * MS_PER_SEC
            return self._build_emergency_result(reason, elapsed_ms)

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

    def _build_emergency_result(self, reason: str, elapsed_ms: float) -> SelectionResult:
        # fill_params 가 실패 원인일 수 있어 다시 부르지 않고 기본값을 직접 쓴다.
        # 키 집합은 PARAM_KEYS_BY_MODE["hold"] 와 같아야 한다(Phase 3 실행기 계약)
        params = {"duration_sec": self._param_defaults.hold_duration_sec}

        return SelectionResult(
            rule_mode=EMERGENCY_RULE_MODE,
            params=params,
            confidence=EMERGENCY_CONFIDENCE,
            probabilities={EMERGENCY_RULE_MODE: 1.0},
            source=SOURCE_RULE,
            fallback_reason=reason,
            options=(EMERGENCY_RULE_MODE,),
            latency=Latency(total_ms=elapsed_ms, orin_processing_ms=None, network_rtt_ms=None),
            input_tokens=None,
        )


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
