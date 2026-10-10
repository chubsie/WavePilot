"""d1 선택기·대체 선택기 단위 테스트 (SEL-01~03, D-04, D-11).

HTTP 없는 stub 만 쓴다(D-11) — 가짜 HTTP 서버를 두지 않는다. 실서버 시나리오는 tests/test_d1_selector_live.py.
"""

import pytest

from modules.decision import d1_selector
from modules.decision.d1_selector import D1SelectionError, D1Selector, estimate_latency
from modules.decision.fallback_selector import EMERGENCY_RULE_MODE, FallbackSelector
from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    FALLBACK_BAD_RESPONSE,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_INTERNAL_ERROR,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_NONE,
    FALLBACK_OUT_OF_MENU,
    FALLBACK_SINGLE_OPTION,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    SOURCE_D1,
    SOURCE_RULE,
    DecisionModel,
    DroneState,
    Latency,
    MissionSummary,
    SelectionEvent,
    SelectionResult,
    SelectionState,
    TrackState,
)
from modules.decision.options import filter_options
from modules.decision.params import PARAM_KEYS_BY_MODE, ParamDefaults, fill_params
from modules.decision.prompt import QUESTION_ID_RULE_MODE, serialize_request_state
from modules.decision.rule_selector import RuleSelector
from modules.decision.systemone_client import ChoiceReply, OutOfMenuError
from modules.llm_report.interface import LLMResponseError, LLMTimeoutError, LLMUnavailableError

CONFIDENCE_THRESHOLD = 0.3
CIRCUIT_OPEN_SEC = 5.0
STUB_RTT_MS = 20.0
STUB_TOTAL_MS = 120.0
STUB_INPUT_TOKENS = 300
D1_REASONS = (
    FALLBACK_TIMEOUT,
    FALLBACK_OUT_OF_MENU,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_UNAVAILABLE,
    FALLBACK_BAD_RESPONSE,
    FALLBACK_SINGLE_OPTION,
)


# --- 상태·사건 헬퍼 ---


def _build_state() -> SelectionState:
    # 활성 person track 1개 + target_class person — 보기가 2개를 넘는다
    drone = DroneState(
        north_m=0.0, east_m=0.0, down_m=-40.0, altitude_m=40.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-55.0, gimbal_yaw_deg=0.0,
        battery_pct=92.0, flight_mode="hover",
    )
    track = TrackState(
        track_id="T1", class_name="person", north_m=31.0, east_m=-6.0,
        first_seen_sec=7.5, last_seen_sec=12.0, observation_count=9, mean_confidence=0.71,
        is_active=True, upper_color=None,
    )
    mission = MissionSummary(target_class="person", target_upper_color=None, hearted_tracks=())

    return SelectionState(time_sec=12.0, drone=drone, tracks=(track,), mission=mission)


def _build_event() -> SelectionEvent:
    return SelectionEvent(kind=EVENT_PERIODIC_CHECK, mode_result=None)


def _build_reply(choice: str, options: tuple[str, ...], confidence: float = 0.8) -> ChoiceReply:
    rest = (1.0 - 0.6) / (len(options) - 1)
    probabilities = {option: (0.6 if option == choice else rest) for option in options}

    return ChoiceReply(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
        input_tokens=STUB_INPUT_TOKENS,
        total_ms=STUB_TOTAL_MS,
    )


# --- stub ---


class StubClient:
    """SystemOneClient 와 같은 두 메서드만 가진 HTTP 없는 객체 (D-11)."""

    def __init__(self, rtt_ms: float | None = STUB_RTT_MS, reply: ChoiceReply | None = None, error: Exception | None = None):
        self.rtt_ms = rtt_ms
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []
        self.rtt_calls = 0

    def measure_rtt_ms(self) -> float | None:
        self.rtt_calls += 1
        return self.rtt_ms

    def ask_choice(self, state_text: str, question_id: str, instructions: str, criteria: dict[str, str]) -> ChoiceReply:
        self.calls.append((state_text, question_id, instructions, dict(criteria)))
        if self.error is not None:
            raise self.error
        return self.reply


class StubPrimary:
    """select 가 지정 예외를 내거나 결과를 돌려주는 primary."""

    def __init__(self, error: BaseException | None = None, result: SelectionResult | None = None):
        self.error = error
        self.result = result
        self.call_count = 0

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.result


class RaisingFallback:
    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        raise RuntimeError("rule selector broke")


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _build_d1_selector(client: StubClient, threshold: float = CONFIDENCE_THRESHOLD) -> D1Selector:
    return D1Selector(client, ParamDefaults(), threshold)


def _build_fallback_selector(primary: StubPrimary, clock: FakeClock | None = None, circuit_open_sec: float = CIRCUIT_OPEN_SEC) -> FallbackSelector:
    return FallbackSelector(primary, RuleSelector(ParamDefaults()), circuit_open_sec, clock=clock or FakeClock())


def _build_d1_result() -> SelectionResult:
    state, event = _build_state(), _build_event()
    options = filter_options(state, event, ParamDefaults().approach_stop_distance_m)
    reply = _build_reply("observe", options)

    return _build_d1_selector(StubClient(reply=reply)).select(state, event)


# --- D1Selector ---


def test_d1_selector_returns_d1_result_with_shared_params():
    state, event = _build_state(), _build_event()
    options = filter_options(state, event, ParamDefaults().approach_stop_distance_m)
    client = StubClient(reply=_build_reply("observe", options))

    result = _build_d1_selector(client).select(state, event)

    assert result.source == SOURCE_D1
    assert result.fallback_reason == FALLBACK_NONE
    assert result.rule_mode == "observe"
    assert result.options == options
    # total_ms 는 select 벽시계라 stub 값과 무관하다(CR-02). orin 분해만 POST 왕복으로 한다
    assert result.latency.total_ms > 0
    assert result.latency.orin_processing_ms == STUB_TOTAL_MS - STUB_RTT_MS
    assert result.latency.network_rtt_ms == STUB_RTT_MS
    assert result.input_tokens == STUB_INPUT_TOKENS
    assert result.confidence == 0.8
    assert result.params == fill_params("observe", state, event, ParamDefaults())


def test_d1_selector_sends_filtered_options_and_serialized_state():
    state, event = _build_state(), _build_event()
    options = filter_options(state, event, ParamDefaults().approach_stop_distance_m)
    client = StubClient(reply=_build_reply("search", options))

    _build_d1_selector(client).select(state, event)

    assert len(options) > 2
    assert len(client.calls) == 1
    state_text, question_id, _instructions, criteria = client.calls[0]
    assert tuple(criteria) == options
    assert question_id == QUESTION_ID_RULE_MODE
    assert state_text == serialize_request_state(state, event)
    assert client.rtt_calls == 1


def test_d1_selector_rtt_none_raises_unavailable_without_post():
    state, event = _build_state(), _build_event()
    client = StubClient(rtt_ms=None, reply=_build_reply("search", filter_options(state, event, ParamDefaults().approach_stop_distance_m)))

    with pytest.raises(D1SelectionError) as raised:
        _build_d1_selector(client).select(state, event)

    # probe 실패는 서버 도달 불가로 보고 POST 를 보내지 않는다(WR-02)
    assert raised.value.reason == FALLBACK_UNAVAILABLE
    assert client.calls == []
    assert raised.value.latency.orin_processing_ms is None
    assert raised.value.latency.network_rtt_ms is None


def test_estimate_latency_clamps_orin_to_zero():
    assert estimate_latency(200.0, 120.0, 150.0) == Latency(200.0, 0.0, 150.0)
    assert estimate_latency(200.0, 120.0, None) == Latency(200.0, None, None)
    assert estimate_latency(200.0, 120.0, 20.0) == Latency(200.0, 100.0, 20.0)


def test_d1_selector_low_confidence_raises_with_latency():
    state, event = _build_state(), _build_event()
    client = StubClient(reply=_build_reply("search", filter_options(state, event, ParamDefaults().approach_stop_distance_m), confidence=0.2))

    with pytest.raises(D1SelectionError) as raised:
        _build_d1_selector(client).select(state, event)

    assert raised.value.reason == FALLBACK_LOW_CONFIDENCE
    assert raised.value.latency.orin_processing_ms == STUB_TOTAL_MS - STUB_RTT_MS
    assert raised.value.latency.network_rtt_ms == STUB_RTT_MS


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LLMTimeoutError("slow"), FALLBACK_TIMEOUT),
        (LLMUnavailableError("down"), FALLBACK_UNAVAILABLE),
        (OutOfMenuError("weird"), FALLBACK_OUT_OF_MENU),
        (LLMResponseError("bad"), FALLBACK_BAD_RESPONSE),
    ],
)
def test_d1_selector_maps_client_errors(error: Exception, reason: str):
    client = StubClient(error=error)

    with pytest.raises(D1SelectionError) as raised:
        _build_d1_selector(client).select(_build_state(), _build_event())

    assert raised.value.reason == reason
    assert raised.value.__cause__ is error
    assert raised.value.latency.total_ms > 0
    assert raised.value.latency.network_rtt_ms == STUB_RTT_MS
    assert raised.value.latency.orin_processing_ms is None


def test_d1_selector_rejects_choice_outside_options():
    state, event = _build_state(), _build_event()
    options = filter_options(state, event, ParamDefaults().approach_stop_distance_m)
    # 파서를 우회한 응답 — choice 가 보기 밖
    reply = ChoiceReply(choice="teleport", probabilities={options[0]: 1.0}, confidence=0.9, input_tokens=1, total_ms=STUB_TOTAL_MS)

    with pytest.raises(D1SelectionError) as raised:
        _build_d1_selector(StubClient(reply=reply)).select(state, event)

    assert raised.value.reason == FALLBACK_OUT_OF_MENU


def test_d1_selector_single_option_skips_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(d1_selector, "filter_options", lambda state, event, approach_stop_distance_m: ("hold",))
    client = StubClient(reply=None)

    with pytest.raises(D1SelectionError) as raised:
        _build_d1_selector(client).select(_build_state(), _build_event())

    assert raised.value.reason == FALLBACK_SINGLE_OPTION
    assert client.calls == []
    assert client.rtt_calls == 0


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_d1_selector_rejects_threshold_outside_unit_interval(threshold: float):
    with pytest.raises(ValueError):
        D1Selector(StubClient(), ParamDefaults(), threshold)


@pytest.mark.parametrize("reason", ["nonsense", FALLBACK_NONE])
def test_d1_selection_error_rejects_unknown_reason(reason: str):
    with pytest.raises(ValueError):
        D1SelectionError(reason)


def test_d1_selection_error_str_includes_reason_and_message():
    error = D1SelectionError(FALLBACK_TIMEOUT, None, "after 1000 ms")

    assert FALLBACK_TIMEOUT in str(error)
    assert "after 1000 ms" in str(error)


# --- FallbackSelector: 대체 ---


def test_fallback_selector_passes_d1_result_through():
    d1_result = _build_d1_result()
    primary = StubPrimary(result=d1_result)

    result = _build_fallback_selector(primary).select(_build_state(), _build_event())

    assert result is d1_result
    assert result.source == SOURCE_D1


@pytest.mark.parametrize("reason", D1_REASONS)
def test_fallback_selector_maps_each_reason_to_rule_result(reason: str):
    d1_latency = Latency(80.0, 60.0, 20.0)
    primary = StubPrimary(error=D1SelectionError(reason, d1_latency))
    state, event = _build_state(), _build_event()
    rule_alone = RuleSelector(ParamDefaults()).select(state, event)

    result = _build_fallback_selector(primary).select(state, event)

    assert result.source == SOURCE_RULE
    assert result.fallback_reason == reason
    assert result.rule_mode == rule_alone.rule_mode
    assert result.latency.network_rtt_ms == d1_latency.network_rtt_ms
    assert result.latency.orin_processing_ms == d1_latency.orin_processing_ms
    assert result.latency.total_ms >= d1_latency.total_ms


def test_fallback_selector_without_d1_latency_keeps_rule_latency():
    primary = StubPrimary(error=D1SelectionError(FALLBACK_SINGLE_OPTION, None))

    result = _build_fallback_selector(primary).select(_build_state(), _build_event())

    assert result.latency.network_rtt_ms is None
    assert result.latency.orin_processing_ms is None
    assert result.latency.total_ms >= 0.0


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyError("missing"), ValueError("bad")])
def test_fallback_selector_absorbs_unexpected_exceptions(error: Exception):
    primary = StubPrimary(error=error)

    result = _build_fallback_selector(primary).select(_build_state(), _build_event())

    assert isinstance(result, SelectionResult)
    assert result.source == SOURCE_RULE
    assert result.fallback_reason == FALLBACK_INTERNAL_ERROR


def test_fallback_selector_does_not_swallow_keyboard_interrupt():
    primary = StubPrimary(error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _build_fallback_selector(primary).select(_build_state(), _build_event())


# --- FallbackSelector: 회로 차단 ---


def test_circuit_opens_after_unavailable_and_retries_after_window():
    clock = FakeClock(0.0)
    primary = StubPrimary(error=D1SelectionError(FALLBACK_UNAVAILABLE, None))
    selector = _build_fallback_selector(primary, clock)
    state, event = _build_state(), _build_event()

    assert selector.select(state, event).fallback_reason == FALLBACK_UNAVAILABLE
    assert primary.call_count == 1

    clock.now = 1.0
    assert selector.select(state, event).fallback_reason == FALLBACK_CIRCUIT_OPEN
    assert primary.call_count == 1

    clock.now = 5.0
    primary.error = None
    primary.result = _build_d1_result()
    assert selector.select(state, event).source == SOURCE_D1
    assert primary.call_count == 2

    clock.now = 5.1
    assert selector.select(state, event).source == SOURCE_D1
    assert primary.call_count == 3


@pytest.mark.parametrize("reason", [FALLBACK_TIMEOUT, FALLBACK_LOW_CONFIDENCE, FALLBACK_OUT_OF_MENU, FALLBACK_BAD_RESPONSE])
def test_non_unavailable_reasons_keep_circuit_closed(reason: str):
    primary = StubPrimary(error=D1SelectionError(reason, None))
    selector = _build_fallback_selector(primary)
    state, event = _build_state(), _build_event()

    selector.select(state, event)
    second = selector.select(state, event)

    assert primary.call_count == 2
    assert second.fallback_reason == reason


def test_zero_circuit_window_never_skips_primary():
    primary = StubPrimary(error=D1SelectionError(FALLBACK_UNAVAILABLE, None))
    selector = _build_fallback_selector(primary, circuit_open_sec=0.0)
    state, event = _build_state(), _build_event()

    selector.select(state, event)
    second = selector.select(state, event)

    assert primary.call_count == 2
    assert second.fallback_reason == FALLBACK_UNAVAILABLE


# --- FallbackSelector: 비상 hold ---


def test_emergency_hold_when_rule_selector_also_fails():
    defaults = ParamDefaults(hold_duration_sec=7.5)
    primary = StubPrimary(error=D1SelectionError(FALLBACK_TIMEOUT, Latency(50.0, None, 10.0)))
    selector = FallbackSelector(primary, RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock(), param_defaults=defaults)

    state, event = _build_state(), _build_event()
    result = selector.select(state, event)

    assert result.rule_mode == EMERGENCY_RULE_MODE == "hold"
    assert result.options == filter_options(state, event, defaults.approach_stop_distance_m)
    assert result.probabilities == {option: (1.0 if option == "hold" else 0.0) for option in result.options}
    assert result.confidence == 1.0
    assert result.params == {"duration_sec": 7.5}
    assert set(result.params) == set(PARAM_KEYS_BY_MODE["hold"])
    assert result.source == SOURCE_RULE
    assert result.fallback_reason == FALLBACK_TIMEOUT
    assert result.latency.orin_processing_ms is None
    assert result.latency.network_rtt_ms is None
    assert result.input_tokens is None


def test_emergency_hold_uses_default_param_defaults():
    primary = StubPrimary(error=RuntimeError("boom"))
    selector = FallbackSelector(primary, RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock())

    result = selector.select(_build_state(), _build_event())

    assert result.params == {"duration_sec": ParamDefaults().hold_duration_sec}
    assert result.fallback_reason == FALLBACK_INTERNAL_ERROR


# --- Protocol·검증 ---


def test_fallback_selector_is_decision_model():
    assert isinstance(_build_fallback_selector(StubPrimary()), DecisionModel)


def test_fallback_selector_rejects_negative_circuit_window():
    with pytest.raises(ValueError):
        FallbackSelector(StubPrimary(), RuleSelector(ParamDefaults()), -1.0)
