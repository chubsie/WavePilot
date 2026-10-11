"""d1-3B 선택기 — 걸러진 보기 안에서 rule_mode 하나를 d1 서버에 묻는다 (SEL-01, SEL-02, D-02, D-03, D-04).

선택 순서:
1. filter_options 로 보기를 거른다(D-02). 보기가 2개 미만이면 물을 것이 없어 single_option 으로 실패한다.
2. 질문(instructions, criteria)과 상태 JSON 을 만든다.
3. GET /health 로 RTT 를 잰다 — 질문 직전에 둬서 같은 네트워크 상태를 잰다.
   probe 가 실패하면 POST 를 보내지 않고, POST 실패와 같은 매핑으로 원인별 사유를 붙인다(WR-02) —
   시간 초과는 timeout, 연결 거부·503·연결 끊김은 unavailable, 응답 이상은 bad_response.
4. POST /v1/systemone 으로 choice 질문 1개를 보낸다.
5. 응답 choice 를 보기와 다시 대조하고(방어), 확신도가 기준 미만이면 low_confidence 로 실패한다.
6. 파라미터는 규칙 선택기와 같은 fill_params 로 채운다(D-03, D-04) — d1 은 모드 이름만 고른다.

지연(SEL-02, RESEARCH §2.3, CR-02):
- total_ms 는 select() 시작부터 응답을 받을 때까지의 벽시계다 — 필터·직렬화·/health probe·POST 를 모두 넣어
  RuleSelector 의 total_ms 와 같은 범위로 비교한다. 실패 경로도 같은 시작 시각부터 잰다.
- 응답에 timings 가 없어 orin_processing_ms = max(0, POST 왕복 − /health RTT) 로 추정한다.
  RTT 를 모르면 분해할 수 없으므로 orin·network 둘 다 None 이다.

실패는 모두 D1SelectionError(reason, latency) 로 올린다. 클라이언트 예외 세 계열(CLIENT_ERRORS) 밖의 예외는
그대로 올려 FallbackSelector 가 흡수한다.
"""

import functools
import time
from collections.abc import Callable

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
from modules.decision.systemone_client import OutOfMenuError, SystemOneClient
from modules.llm_report.interface import LLMBackendError, LLMResponseError, LLMTimeoutError, LLMUnavailableError

MIN_OPTION_COUNT = 2  # 보기가 1개면 서버 답이 정해져 있어 물을 필요가 없다
MS_PER_SEC = 1000.0
PROBE_FAILURE_MESSAGE = "health probe failed"
CLIENT_ERRORS = (LLMTimeoutError, LLMUnavailableError, LLMResponseError)
# 위에서부터 첫 일치를 쓴다 — OutOfMenuError 는 LLMResponseError 의 하위 클래스라 먼저 둔다
REASON_BY_CLIENT_ERROR = (
    (OutOfMenuError, FALLBACK_OUT_OF_MENU),
    (LLMTimeoutError, FALLBACK_TIMEOUT),
    (LLMUnavailableError, FALLBACK_UNAVAILABLE),
    (LLMResponseError, FALLBACK_BAD_RESPONSE),
)


class D1SelectionError(Exception):
    """d1 선택 실패. reason 은 FallbackSelector 가 결과의 fallback_reason 으로 그대로 쓴다."""

    def __init__(self, reason: str, latency: Latency | None = None, message: str = "") -> None:
        if reason not in FALLBACK_REASONS or reason == FALLBACK_NONE:
            raise ValueError(f"reason must be a fallback reason other than 'none', got {reason!r}")

        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason
        self.latency = latency


def estimate_latency(selection_ms: float, request_ms: float, network_rtt_ms: float | None) -> Latency:
    """select 벽시계를 total 로 두고, POST 왕복에서 /health RTT 를 빼 Orin 처리 시간을 추정한다.

    RTT 를 모르면 분해하지 않는다.
    """
    if network_rtt_ms is None:
        return Latency(total_ms=selection_ms, orin_processing_ms=None, network_rtt_ms=None)

    orin_processing_ms = max(0.0, request_ms - network_rtt_ms)
    return Latency(total_ms=selection_ms, orin_processing_ms=orin_processing_ms, network_rtt_ms=network_rtt_ms)


def get_fallback_reason(error: LLMBackendError) -> str:
    """클라이언트 예외를 fallback 사유로 바꾼다. probe 와 POST 가 같은 표를 쓴다."""
    for error_type, reason in REASON_BY_CLIENT_ERROR:
        if isinstance(error, error_type):
            return reason

    raise ValueError(f"no fallback reason for {type(error).__name__}")


def _get_elapsed_ms(started_sec: float) -> float:
    return (time.perf_counter() - started_sec) * MS_PER_SEC


class D1Selector:
    def __init__(
        self,
        client: SystemOneClient,
        defaults: ParamDefaults,
        confidence_threshold: float,
        allow_return_home: bool = True,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold!r}")

        self._client = client
        self._defaults = defaults
        self._confidence_threshold = confidence_threshold
        self._allow_return_home = allow_return_home

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        started_sec = time.perf_counter()

        options = filter_options(state, event, self._defaults.approach_stop_distance_m, self._allow_return_home)
        # filter_options 가 search·hold 를 늘 남겨 오늘은 도달하지 않는다 — 보기 규칙이 바뀔 때의 가드
        if len(options) < MIN_OPTION_COUNT:
            raise D1SelectionError(FALLBACK_SINGLE_OPTION, None, f"only {options} left after filtering")

        instructions, criteria = build_rule_mode_question(options)
        state_text = serialize_request_state(state, event)

        # probe 가 실패한 서버에 POST 를 보내면 request timeout 만큼 더 막힌다(WR-02)
        network_rtt_ms = self._call_client(self._client.probe_rtt_ms, started_sec, None, PROBE_FAILURE_MESSAGE)
        ask_rule_mode = functools.partial(self._client.ask_choice, state_text, QUESTION_ID_RULE_MODE, instructions, criteria)
        reply = self._call_client(ask_rule_mode, started_sec, network_rtt_ms)
        latency = estimate_latency(_get_elapsed_ms(started_sec), reply.total_ms, network_rtt_ms)

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

    @staticmethod
    def _call_client[T](
        call: Callable[[], T], started_sec: float, network_rtt_ms: float | None, failure_message: str = ""
    ) -> T:
        """클라이언트 호출 실패를 D1SelectionError 로 바꾼다. 실패 지연도 select 시작 시각(started_sec)부터 잰다."""
        try:
            return call()
        except CLIENT_ERRORS as error:
            latency = Latency(total_ms=_get_elapsed_ms(started_sec), orin_processing_ms=None, network_rtt_ms=network_rtt_ms)
            message = f"{failure_message}: {error}" if failure_message else str(error)
            raise D1SelectionError(get_fallback_reason(error), latency, message) from error
