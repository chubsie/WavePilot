"""선택기 팩토리 테스트 (SEL-04, D-06) — 설정 값 하나로 규칙 ↔ d1+대체 선택기를 바꾼다.

d1 조립은 생성만 하고 select 를 부르지 않는다(네트워크 없음, D-11).
"""

import pytest

from modules.decision.factory import (
    CIRCUIT_OPEN_ENV,
    CIRCUIT_OPEN_SEC,
    CONFIDENCE_THRESHOLD,
    CONFIDENCE_THRESHOLD_ENV,
    D1_REQUEST_TIMEOUT_MS,
    D1_TIMEOUT_ENV,
    D1_URL_ENV,
    SELECTOR_D1,
    SELECTOR_ENV,
    SELECTOR_KINDS,
    SELECTOR_RULE,
    SelectorConfig,
    create_decision_model,
)
from modules.decision.fallback_selector import FallbackSelector
from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    SOURCE_RULE,
    DecisionModel,
    DroneState,
    MissionSummary,
    SelectionEvent,
    SelectionResult,
    SelectionState,
    TrackState,
)
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector

LOCAL_D1_URL = "http://127.0.0.1:8081"
DOC_D1_URL = "http://192.0.2.10:8081"  # RFC 5737 문서용 주소 — 접속하지 않는다


# --- 헬퍼 ---


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


def _build_config(kind: str, d1_url: str | None = None) -> SelectorConfig:
    return SelectorConfig(
        kind=kind,
        d1_url=d1_url,
        request_timeout_ms=D1_REQUEST_TIMEOUT_MS,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        circuit_open_sec=CIRCUIT_OPEN_SEC,
        param_defaults=ParamDefaults(),
    )


def run_once(model: DecisionModel, state: SelectionState, event: SelectionEvent) -> SelectionResult:
    # 호출 쪽은 DecisionModel 만 안다 — 뒤의 선택기 종류와 무관하게 같은 코드
    return model.select(state, event)


# --- from_env ---


def test_constants_match_contract():
    assert SELECTOR_ENV == "WAVEPILOT_SELECTOR"
    assert D1_URL_ENV == "D1_SERVER_URL"
    assert D1_TIMEOUT_ENV == "WAVEPILOT_D1_TIMEOUT_MS"
    assert CONFIDENCE_THRESHOLD_ENV == "WAVEPILOT_D1_CONFIDENCE_THRESHOLD"
    assert CIRCUIT_OPEN_ENV == "WAVEPILOT_D1_CIRCUIT_OPEN_SEC"
    assert SELECTOR_KINDS == (SELECTOR_RULE, SELECTOR_D1) == ("rule", "d1")


def test_from_env_defaults_to_rule():
    config = SelectorConfig.from_env({})

    assert config.kind == SELECTOR_RULE
    assert config.d1_url is None
    assert config.request_timeout_ms == 1000.0
    assert config.confidence_threshold == 0.3
    assert config.circuit_open_sec == 5.0
    assert config.param_defaults == ParamDefaults()


def test_from_env_normalizes_kind_and_keeps_url():
    config = SelectorConfig.from_env({SELECTOR_ENV: " D1 ", D1_URL_ENV: DOC_D1_URL})

    assert config.kind == SELECTOR_D1
    assert config.d1_url == DOC_D1_URL


def test_from_env_parses_numbers():
    environ = {
        D1_TIMEOUT_ENV: "250",
        CONFIDENCE_THRESHOLD_ENV: "0.5",
        CIRCUIT_OPEN_ENV: "0",
    }

    config = SelectorConfig.from_env(environ)

    assert config.request_timeout_ms == 250.0
    assert config.confidence_threshold == 0.5
    assert config.circuit_open_sec == 0.0


def test_from_env_rejects_unknown_kind():
    with pytest.raises(ValueError, match="rule|d1"):
        SelectorConfig.from_env({SELECTOR_ENV: "llm"})


@pytest.mark.parametrize("environ", [{SELECTOR_ENV: "d1"}, {SELECTOR_ENV: "d1", D1_URL_ENV: ""}])
def test_from_env_d1_requires_url(environ: dict[str, str]):
    with pytest.raises(ValueError):
        SelectorConfig.from_env(environ)


def test_from_env_rejects_non_numeric_value_with_env_name():
    with pytest.raises(ValueError, match=D1_TIMEOUT_ENV):
        SelectorConfig.from_env({D1_TIMEOUT_ENV: "abc"})


@pytest.mark.parametrize(
    "environ",
    [
        {D1_TIMEOUT_ENV: "0"},
        {D1_TIMEOUT_ENV: "-5"},
        {CONFIDENCE_THRESHOLD_ENV: "-0.1"},
        {CONFIDENCE_THRESHOLD_ENV: "1.5"},
        {CIRCUIT_OPEN_ENV: "-1"},
    ],
)
def test_from_env_rejects_out_of_range_values(environ: dict[str, str]):
    with pytest.raises(ValueError):
        SelectorConfig.from_env(environ)


# --- create_decision_model ---


def test_rule_config_builds_rule_selector():
    model = create_decision_model(_build_config(SELECTOR_RULE))

    assert isinstance(model, RuleSelector)
    assert isinstance(model, DecisionModel)
    assert run_once(model, _build_state(), _build_event()).source == SOURCE_RULE


def test_d1_config_builds_fallback_selector_without_network():
    model = create_decision_model(_build_config(SELECTOR_D1, LOCAL_D1_URL))

    assert isinstance(model, FallbackSelector)
    assert isinstance(model, DecisionModel)
