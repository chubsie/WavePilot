"""d1-3B 선택기 — 걸러진 보기 안에서 rule_mode 하나를 d1 서버에 묻는다 (SEL-01, SEL-02, D-02, D-03, D-04).

선택 순서:
1. filter_options 로 보기를 거른다(D-02). 보기가 2개 미만이면 물을 것이 없어 single_option 으로 실패한다.
2. 질문(instructions, criteria)과 상태 JSON 을 만든다.
3. GET /health 로 RTT 를 잰다 — 질문 직전에 둬서 같은 네트워크 상태를 잰다.
4. POST /v1/systemone 으로 choice 질문 1개를 보낸다.
5. 응답 choice 를 보기와 다시 대조하고(방어), 확신도가 기준 미만이면 low_confidence 로 실패한다.
6. 파라미터는 규칙 선택기와 같은 fill_params 로 채운다(D-03, D-04) — d1 은 모드 이름만 고른다.

지연 추정(SEL-02, RESEARCH §2.3): 응답에 timings 가 없어 orin_processing_ms = max(0, total_ms − /health RTT) 로 추정한다.
RTT probe 가 실패하면 분해할 수 없으므로 orin·network 둘 다 None 이다.

실패는 모두 D1SelectionError(reason, latency) 로 올린다. 클라이언트 예외 네 계열 밖의 예외는 그대로 올려
FallbackSelector 가 bad_response 로 흡수한다.
"""

import time

from modules.decision.interface import (
    FALLBACK_BAD_RESPONSE,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_NONE,
    FALLBACK_OUT_OF_MENU,
    FALLBACK_REASONS,
    FALLBACK_SINGLE_OPTION,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    SOURCE_D1,
    Latency,
    SelectionEvent,
    SelectionResult,
    SelectionState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.prompt import QUESTION_ID_RULE_MODE, build_rule_mode_question, serialize_request_state
from modules.decision.systemone_client import ChoiceReply, OutOfMenuError, SystemOneClient
from modules.llm_report.interface import LLMResponseError, LLMTimeoutError, LLMUnavailableError

MIN_OPTION_COUNT = 2  # 보기가 1개면 서버 답이 정해져 있어 물을 필요가 없다
MS_PER_SEC = 1000.0


class D1SelectionError(Exception):
    """d1 선택 실패. reason 은 FallbackSelector 가 결과의 fallback_reason 으로 그대로 쓴다."""

    def __init__(self, reason: str, latency: Latency | None = None, message: str = "") -> None:
        if reason not in FALLBACK_REASONS or reason == FALLBACK_NONE:
            raise ValueError(f"reason must be a fallback reason other than 'none', got {reason!r}")

        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason
        self.latency = latency


def estimate_latency(total_ms: float, network_rtt_ms: float | None) -> Latency:
    """POST 왕복 시간과 /health RTT 로 지연을 분해한다. RTT 를 모르면 분해하지 않는다."""
    if network_rtt_ms is None:
        return Latency(total_ms=total_ms, orin_processing_ms=None, network_rtt_ms=None)

    orin_processing_ms = max(0.0, total_ms - network_rtt_ms)
    return Latency(total_ms=total_ms, orin_processing_ms=orin_processing_ms, network_rtt_ms=network_rtt_ms)


class D1Selector:
    def __init__(self, client: SystemOneClient, defaults: ParamDefaults, confidence_threshold: float) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold!r}")

        self._client = client
        self._defaults = defaults
        self._confidence_threshold = confidence_threshold

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        options = filter_options(state, event)
        if len(options) < MIN_OPTION_COUNT:
            raise D1SelectionError(FALLBACK_SINGLE_OPTION, None, f"only {options} left after filtering")

        instructions, criteria = build_rule_mode_question(options)
        state_text = serialize_request_state(state, event)

        network_rtt_ms = self._client.measure_rtt_ms()
        reply = self._ask_rule_mode(state_text, instructions, criteria, network_rtt_ms)
        latency = estimate_latency(reply.total_ms, network_rtt_ms)

        if reply.choice not in options:
            # 파서가 이미 막지만 응답 변조·서버 버전 차이에 대비해 한 번 더 본다(T-01-05-01)
            raise D1SelectionError(FALLBACK_OUT_OF_MENU, latency, f"choice {reply.choice!r} is outside {options}")
        if reply.confidence < self._confidence_threshold:
            message = f"confidence {reply.confidence:.3f} < {self._confidence_threshold}"
            raise D1SelectionError(FALLBACK_LOW_CONFIDENCE, latency, message)

        params = fill_params(reply.choice, state, event, self._defaults)

        return SelectionResult(
            rule_mode=reply.choice,
            params=params,
            confidence=reply.confidence,
            probabilities=dict(reply.probabilities),
            source=SOURCE_D1,
            fallback_reason=FALLBACK_NONE,
            options=options,
            latency=latency,
            input_tokens=reply.input_tokens,
        )

    def _ask_rule_mode(
        self, state_text: str, instructions: str, criteria: dict[str, str], network_rtt_ms: float | None
    ) -> ChoiceReply:
        """클라이언트 예외를 fallback 사유로 바꾼다.

        except 순서가 의미를 가진다 — OutOfMenuError 는 LLMResponseError 의 하위 클래스라 먼저 잡는다.
        """
        started_sec = time.perf_counter()

        try:
            return self._client.ask_choice(state_text, QUESTION_ID_RULE_MODE, instructions, criteria)
        except OutOfMenuError as error:
            reason = FALLBACK_OUT_OF_MENU
            cause = error
        except LLMTimeoutError as error:
            reason = FALLBACK_TIMEOUT
            cause = error
        except LLMUnavailableError as error:
            reason = FALLBACK_UNAVAILABLE
            cause = error
        except LLMResponseError as error:
            reason = FALLBACK_BAD_RESPONSE
            cause = error

        elapsed_ms = (time.perf_counter() - started_sec) * MS_PER_SEC
        latency = Latency(total_ms=elapsed_ms, orin_processing_ms=None, network_rtt_ms=network_rtt_ms)
        raise D1SelectionError(reason, latency, str(cause)) from cause
