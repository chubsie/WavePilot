"""실서버 d1 선택기 테스트 (SEL-01, SEL-02, SEL-03 — D-11: 가짜 서버 없이 Orin 실서버로만).

실행:
    Orin 에서 사용자가 01-02 런북대로 llama-decision(포트 8081)을 띄운 뒤
    D1_SERVER_URL=http://<orin-ip>:8081 uv run pytest -m d1 tests/test_d1_selector_live.py

기본 게이트에서는 마커 d1 로 빠지고, D1_SERVER_URL 이 없거나 /health 가 200 이 아니면 전부 skip 된다.
메뉴 밖 답은 실서버로 재현되지 않아 이 파일에 두지 않는다(tests/test_systemone_parse.py, tests/test_fallback_selector.py 몫).
"""

import math
import os
import socket

import pytest

from modules.decision.d1_selector import D1Selector
from modules.decision.factory import CIRCUIT_OPEN_SEC, SELECTOR_D1, SelectorConfig, create_decision_model
from modules.decision.fallback_selector import FallbackSelector
from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_NONE,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    RULE_MODES,
    SOURCE_D1,
    SOURCE_RULE,
    DroneState,
    MissionSummary,
    SelectionEvent,
    SelectionResult,
    SelectionState,
    TrackState,
)
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.decision.systemone_client import SystemOneClient

pytestmark = pytest.mark.d1

LIVE_REQUEST_TIMEOUT_MS = 10000.0  # 첫 호출 CUDA warm-up 여유
LIVE_TIMEOUT_MS = 5.0  # 추론(≥ 50 ms)보다 짧게 (RESEARCH §4)
# 판정이 confidence < threshold 라 1.0 이면 서버가 정확히 1.0 을 내지 않는 한 항상 미달한다.
# 0.99 는 확실한 상태에서 통과할 수 있어 쓰지 않는다
LIVE_HIGH_THRESHOLD = 1.0
READY_TIMEOUT_SEC = 2.0
CLOSED_PORT_TIMEOUT_SEC = 1.0
PROBABILITY_SUM_TOLERANCE = 1e-6


@pytest.fixture
def d1_server_url() -> str:
    url = os.environ.get("D1_SERVER_URL", "")
    if not url:
        pytest.skip("D1_SERVER_URL not set")

    if not SystemOneClient(url, READY_TIMEOUT_SEC).is_ready():
        pytest.skip(f"d1-3B llama-server not ready at {url}/health")

    return url


# --- 헬퍼 (테스트 파일끼리 import 하지 않는다) ---


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


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


def _build_config(url: str, timeout_ms: float, threshold: float, circuit_open_sec: float) -> SelectorConfig:
    return SelectorConfig(
        kind=SELECTOR_D1,
        d1_url=url,
        request_timeout_ms=timeout_ms,
        confidence_threshold=threshold,
        circuit_open_sec=circuit_open_sec,
        param_defaults=ParamDefaults(),
    )


def _get_closed_port() -> int:
    # 포트 번호만 얻고 바로 닫는다 — 리스너를 두지 않으므로 가짜 서버가 아니다.
    # Orin ufw 가 DROP 하면 닫힌 포트가 timeout 으로 보이므로 로컬 닫힌 포트를 쓴다(RESEARCH Pitfall 5)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _assert_valid_result(result: SelectionResult) -> None:
    assert isinstance(result, SelectionResult), result
    assert result.rule_mode in result.options, result.to_dict()
    assert set(result.options) <= set(RULE_MODES), result.to_dict()
    assert result.latency.total_ms > 0, result.to_dict()


# --- 시나리오 ---


def test_live_d1_normal_selection(d1_server_url: str) -> None:
    config = _build_config(d1_server_url, LIVE_REQUEST_TIMEOUT_MS, 0.0, CIRCUIT_OPEN_SEC)

    result = create_decision_model(config).select(_build_state(), _build_event())

    _assert_valid_result(result)
    detail = result.to_dict()
    assert result.source == SOURCE_D1, detail
    assert result.fallback_reason == FALLBACK_NONE, detail
    assert set(result.probabilities) <= set(result.options), detail
    assert math.isclose(sum(result.probabilities.values()), 1.0, abs_tol=PROBABILITY_SUM_TOLERANCE), detail
    assert 0.0 <= result.confidence <= 1.0, detail
    assert result.latency.network_rtt_ms is not None and result.latency.network_rtt_ms >= 0, detail
    assert result.latency.orin_processing_ms is not None and result.latency.orin_processing_ms >= 0, detail
    assert result.latency.orin_processing_ms <= result.latency.total_ms, detail
    assert result.input_tokens is not None and result.input_tokens > 0, detail
    assert isinstance(result.params, dict), detail


def test_live_timeout_falls_back_to_rule(d1_server_url: str) -> None:
    config = _build_config(d1_server_url, LIVE_TIMEOUT_MS, 0.0, CIRCUIT_OPEN_SEC)

    result = create_decision_model(config).select(_build_state(), _build_event())

    _assert_valid_result(result)
    assert result.source == SOURCE_RULE, result.to_dict()
    assert result.fallback_reason == FALLBACK_TIMEOUT, result.to_dict()


def test_live_closed_port_falls_back_and_opens_circuit(d1_server_url: str) -> None:
    clock = FakeClock()
    client = SystemOneClient(f"http://127.0.0.1:{_get_closed_port()}", CLOSED_PORT_TIMEOUT_SEC)
    primary = D1Selector(client, ParamDefaults(), 0.0)
    selector = FallbackSelector(primary, RuleSelector(ParamDefaults()), circuit_open_sec=5.0, clock=clock)
    state, event = _build_state(), _build_event()

    first = selector.select(state, event)
    _assert_valid_result(first)
    assert first.fallback_reason == FALLBACK_UNAVAILABLE, first.to_dict()

    second = selector.select(state, event)
    _assert_valid_result(second)
    assert second.fallback_reason == FALLBACK_CIRCUIT_OPEN, second.to_dict()

    clock.advance(5.1)
    third = selector.select(state, event)
    _assert_valid_result(third)
    assert third.fallback_reason == FALLBACK_UNAVAILABLE, third.to_dict()


def test_live_low_confidence_falls_back_to_rule(d1_server_url: str) -> None:
    config = _build_config(d1_server_url, LIVE_REQUEST_TIMEOUT_MS, LIVE_HIGH_THRESHOLD, CIRCUIT_OPEN_SEC)

    result = create_decision_model(config).select(_build_state(), _build_event())

    _assert_valid_result(result)
    detail = result.to_dict()
    assert result.source == SOURCE_RULE, detail
    assert result.fallback_reason == FALLBACK_LOW_CONFIDENCE, detail
    # d1 이 실제로 답했다는 증거
    assert result.latency.network_rtt_ms is not None, detail
    assert result.latency.orin_processing_ms is not None, detail
