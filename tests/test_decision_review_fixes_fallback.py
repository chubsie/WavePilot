"""01-REVIEW 대체 선택기·팩토리 수정 테스트 (WR-02, WR-03, WR-06, IN-03, IN-06).

HTTP 없는 가짜 primary·가짜 clock 만 쓴다(D-11). 네트워크에 접속하지 않는다.
"""

import logging

import pytest

from modules.decision import fallback_selector as fallback_module
from modules.decision import FALLBACK_INTERNAL_ERROR as PACKAGE_FALLBACK_INTERNAL_ERROR
from modules.decision.d1_selector import D1SelectionError
from modules.decision.factory import (
    D1_URL_ENV,
    SELECTOR_D1,
    SELECTOR_ENV,
    SELECTOR_RULE,
    SelectorConfig,
    create_decision_model,
)
from modules.decision.fallback_selector import CIRCUIT_TIMEOUT_STREAK, FallbackSelector
from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    FALLBACK_BAD_RESPONSE,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_INTERNAL_ERROR,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_NONE,
    FALLBACK_REASONS,
    FALLBACK_TIMEOUT,
    SOURCE_D1,
    SOURCE_RULE,
    DroneState,
    MissionSummary,
    SelectionEvent,
    SelectionResult,
    SelectionState,
    TrackState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector

CIRCUIT_OPEN_SEC = 5.0


# --- 상태·사건 헬퍼 ---


def _build_state() -> SelectionState:
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


# --- 가짜 ---


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedPrimary:
    """호출마다 steps 를 하나씩 꺼내 예외면 던지고 결과면 돌려준다."""

    def __init__(self, steps: list[BaseException | SelectionResult]):
        self.steps = list(steps)
        self.call_count = 0

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        step = self.steps[self.call_count]
        self.call_count += 1
        if isinstance(step, BaseException):
            raise step
        return step


class RaisingFallback:
    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        raise RuntimeError("rule selector broke")


def _build_d1_result() -> SelectionResult:
    rule_result = RuleSelector(ParamDefaults()).select(_build_state(), _build_event())

    return SelectionResult(
        rule_mode=rule_result.rule_mode,
        params=rule_result.params,
        confidence=rule_result.confidence,
        probabilities=rule_result.probabilities,
        source=SOURCE_D1,
        fallback_reason=FALLBACK_NONE,
        options=rule_result.options,
        latency=rule_result.latency,
        input_tokens=300,
    )


def _timeout() -> D1SelectionError:
    return D1SelectionError(FALLBACK_TIMEOUT, None)


def _build_selector(primary: ScriptedPrimary, clock: FakeClock, circuit_open_sec: float = CIRCUIT_OPEN_SEC) -> FallbackSelector:
    return FallbackSelector(primary, RuleSelector(ParamDefaults()), circuit_open_sec, clock=clock)


def _select_reasons(selector: FallbackSelector, count: int) -> list[str]:
    state, event = _build_state(), _build_event()
    return [selector.select(state, event).fallback_reason for _ in range(count)]


# --- WR-02: 연속 timeout 회로 차단 ---


def test_circuit_timeout_streak_default_is_three():
    assert CIRCUIT_TIMEOUT_STREAK == 3


def test_circuit_opens_after_consecutive_timeouts_and_retries_after_window():
    clock = FakeClock(0.0)
    primary = ScriptedPrimary([_timeout(), _timeout(), _timeout(), _build_d1_result()])
    selector = _build_selector(primary, clock)

    assert _select_reasons(selector, 3) == [FALLBACK_TIMEOUT] * 3
    assert primary.call_count == 3

    clock.now = 4.9
    assert _select_reasons(selector, 1) == [FALLBACK_CIRCUIT_OPEN]
    assert primary.call_count == 3

    clock.now = 5.0
    assert selector.select(_build_state(), _build_event()).source == SOURCE_D1
    assert primary.call_count == 4


def test_fewer_timeouts_than_streak_keep_circuit_closed():
    primary = ScriptedPrimary([_timeout(), _timeout(), _build_d1_result()])
    selector = _build_selector(primary, FakeClock())

    _select_reasons(selector, 2)
    result = selector.select(_build_state(), _build_event())

    assert result.source == SOURCE_D1
    assert primary.call_count == 3


def test_success_resets_timeout_streak():
    steps = [_timeout(), _timeout(), _build_d1_result(), _timeout(), _timeout(), _build_d1_result()]
    primary = ScriptedPrimary(steps)
    selector = _build_selector(primary, FakeClock())
    state, event = _build_state(), _build_event()

    results = [selector.select(state, event) for _ in range(6)]

    assert primary.call_count == 6
    assert results[-1].source == SOURCE_D1


@pytest.mark.parametrize(
    "error",
    [D1SelectionError(FALLBACK_LOW_CONFIDENCE, None), D1SelectionError(FALLBACK_BAD_RESPONSE, None), RuntimeError("bug")],
)
def test_non_timeout_reason_resets_timeout_streak(error: Exception):
    steps = [_timeout(), _timeout(), error, _timeout(), _timeout(), _build_d1_result()]
    primary = ScriptedPrimary(steps)
    selector = _build_selector(primary, FakeClock())

    reasons = _select_reasons(selector, 5)
    last = selector.select(_build_state(), _build_event())

    assert FALLBACK_CIRCUIT_OPEN not in reasons
    assert last.source == SOURCE_D1
    assert primary.call_count == 6


def test_streak_restarts_after_circuit_closes():
    clock = FakeClock(0.0)
    primary = ScriptedPrimary([_timeout()] * 5 + [_build_d1_result()])
    selector = _build_selector(primary, clock)

    _select_reasons(selector, 3)
    clock.now = 10.0

    # 회로가 닫힌 뒤에는 처음부터 다시 3회를 센다 — 2회로는 열리지 않는다
    assert _select_reasons(selector, 2) == [FALLBACK_TIMEOUT] * 2
    assert selector.select(_build_state(), _build_event()).source == SOURCE_D1
    assert primary.call_count == 6


def test_zero_circuit_window_never_opens_on_timeouts():
    primary = ScriptedPrimary([_timeout()] * 5)
    selector = _build_selector(primary, FakeClock(), circuit_open_sec=0.0)

    assert _select_reasons(selector, 5) == [FALLBACK_TIMEOUT] * 5
    assert primary.call_count == 5


def test_custom_timeout_streak_opens_after_one_timeout():
    primary = ScriptedPrimary([_timeout()])
    selector = FallbackSelector(primary, RuleSelector(ParamDefaults()), CIRCUIT_OPEN_SEC, clock=FakeClock(), circuit_timeout_streak=1)

    assert _select_reasons(selector, 2) == [FALLBACK_TIMEOUT, FALLBACK_CIRCUIT_OPEN]
    assert primary.call_count == 1


@pytest.mark.parametrize("streak", [0, -1, 1.5, True])
def test_rejects_invalid_timeout_streak(streak: object):
    with pytest.raises(ValueError, match="circuit_timeout_streak"):
        FallbackSelector(ScriptedPrimary([]), RuleSelector(ParamDefaults()), CIRCUIT_OPEN_SEC, circuit_timeout_streak=streak)


# --- WR-03: internal_error 와 traceback ---


def test_internal_error_reason_is_last_and_exported():
    assert FALLBACK_INTERNAL_ERROR == "internal_error"
    assert FALLBACK_REASONS[-1] == FALLBACK_INTERNAL_ERROR
    assert PACKAGE_FALLBACK_INTERNAL_ERROR == FALLBACK_INTERNAL_ERROR


def test_unexpected_primary_error_is_internal_error_with_traceback(caplog: pytest.LogCaptureFixture):
    primary = ScriptedPrimary([TypeError("bad field")])
    selector = _build_selector(primary, FakeClock())

    with caplog.at_level(logging.ERROR, logger=fallback_module.__name__):
        result = selector.select(_build_state(), _build_event())

    assert result.source == SOURCE_RULE
    assert result.fallback_reason == FALLBACK_INTERNAL_ERROR
    records = [record for record in caplog.records if record.exc_info is not None]
    assert len(records) == 1
    assert records[0].exc_info[0] is TypeError


def test_d1_selection_error_keeps_its_reason():
    primary = ScriptedPrimary([D1SelectionError(FALLBACK_BAD_RESPONSE, None)])

    result = _build_selector(primary, FakeClock()).select(_build_state(), _build_event())

    assert result.fallback_reason == FALLBACK_BAD_RESPONSE


def test_rule_failure_logs_traceback_before_emergency_hold(caplog: pytest.LogCaptureFixture):
    primary = ScriptedPrimary([_timeout()])
    selector = FallbackSelector(primary, RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock())

    with caplog.at_level(logging.ERROR, logger=fallback_module.__name__):
        result = selector.select(_build_state(), _build_event())

    assert result.rule_mode == "hold"
    assert result.fallback_reason == FALLBACK_TIMEOUT
    assert any(record.exc_info is not None and record.exc_info[0] is RuntimeError for record in caplog.records)


# --- IN-03: 비상 결과의 options ---


def test_emergency_options_are_filtered_options():
    defaults = ParamDefaults()
    primary = ScriptedPrimary([_timeout()])
    selector = FallbackSelector(primary, RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock(), param_defaults=defaults)
    state, event = _build_state(), _build_event()

    result = selector.select(state, event)

    expected_options = filter_options(state, event, defaults.approach_stop_distance_m)
    assert len(expected_options) > 1
    assert result.options == expected_options
    assert result.probabilities["hold"] == 1.0
    assert sum(result.probabilities.values()) == 1.0
    assert set(result.probabilities) == set(expected_options)


def test_emergency_options_fall_back_to_hold_when_filter_fails(monkeypatch: pytest.MonkeyPatch):
    def raise_filter_error(*args: object) -> tuple[str, ...]:
        raise ValueError("filter broke")

    monkeypatch.setattr(fallback_module, "filter_options", raise_filter_error)
    selector = FallbackSelector(ScriptedPrimary([_timeout()]), RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock())

    result = selector.select(_build_state(), _build_event())

    assert result.options == ("hold",)
    assert result.probabilities == {"hold": 1.0}


def test_emergency_options_fall_back_to_hold_when_hold_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fallback_module, "filter_options", lambda *args: ("search", "return_home"))
    selector = FallbackSelector(ScriptedPrimary([_timeout()]), RaisingFallback(), CIRCUIT_OPEN_SEC, clock=FakeClock())

    result = selector.select(_build_state(), _build_event())

    assert result.options == ("hold",)


# --- WR-06: D1_SERVER_URL 공백 ---


@pytest.mark.parametrize("raw_url", ["  http://orin:8081  ", "\thttp://orin:8081\n"])
def test_from_env_strips_d1_url(raw_url: str):
    config = SelectorConfig.from_env({SELECTOR_ENV: SELECTOR_D1, D1_URL_ENV: raw_url})

    assert config.d1_url == "http://orin:8081"


@pytest.mark.parametrize("raw_url", ["", "   "])
def test_from_env_blank_d1_url_is_none(raw_url: str):
    config = SelectorConfig.from_env({D1_URL_ENV: raw_url})

    assert config.d1_url is None


def test_from_env_blank_d1_url_with_d1_selector_fails():
    with pytest.raises(ValueError, match=D1_URL_ENV):
        SelectorConfig.from_env({SELECTOR_ENV: SELECTOR_D1, D1_URL_ENV: "   "})


def test_create_decision_model_propagates_client_url_error():
    config = SelectorConfig.from_env({SELECTOR_ENV: SELECTOR_D1, D1_URL_ENV: "ftp://orin:8081"})

    with pytest.raises(ValueError, match="base_url"):
        create_decision_model(config)


# --- IN-06: 빈 WAVEPILOT_SELECTOR ---


@pytest.mark.parametrize("raw_kind", ["", "   ", "\t\n"])
def test_from_env_blank_selector_is_rule(raw_kind: str):
    config = SelectorConfig.from_env({SELECTOR_ENV: raw_kind})

    assert config.kind == SELECTOR_RULE
    assert isinstance(create_decision_model(config), RuleSelector)


def test_from_env_selector_still_rejects_unknown_kind():
    with pytest.raises(ValueError, match=SELECTOR_ENV):
        SelectorConfig.from_env({SELECTOR_ENV: " llm "})
