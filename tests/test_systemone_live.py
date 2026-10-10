"""실서버 d1-3B /v1/systemone 테스트 (D-11 — 가짜 서버 없이 Orin 실서버로만, 기본 게이트에서는 마커 d1 로 빠진다).

실행:
    D1_SERVER_URL=http://<orin-ip>:8081 uv run pytest -m d1 tests/test_systemone_live.py

서버 기동은 사용자가 01-02 RUNBOOK 으로 한다(테스트는 서버를 띄우지 않는다).
D1_SERVER_URL 이 없거나 /health 가 200 이 아니면 전부 skip 된다.

닫힌 포트 테스트는 기본으로 D1_SERVER_URL 과 같은 호스트의 CLOSED_PORT_DEFAULT(8099) 포트를 쓰고,
D1_CLOSED_URL(선택)로 바꿀 수 있다. Orin 방화벽이 패킷을 버리도록(drop) 설정돼 있으면 연결 거부 대신
timeout 이 나므로, 그때는 D1_CLOSED_URL 을 연결이 거부되는 포트로 지정한다.

메뉴 밖 답은 정상 서버로 재현되지 않으므로 이 파일에 두지 않고 tests/test_systemone_parse.py 가 맡는다.
"""

import os
import urllib.parse

import pytest

from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    RULE_MODES,
    DroneState,
    MissionSummary,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.prompt import QUESTION_ID_RULE_MODE, build_rule_mode_question, serialize_request_state
from modules.decision.systemone_client import SystemOneClient
from modules.llm_report.interface import LLMTimeoutError, LLMUnavailableError

pytestmark = pytest.mark.d1

LIVE_REQUEST_TIMEOUT_SEC = 30.0  # 첫 호출 CUDA warm-up 여유. 무한 대기는 금지
TIMEOUT_REPRO_SEC = 0.005  # 추론(≥ 50 ms)보다 짧게 (RESEARCH §4)
CLOSED_PORT_DEFAULT = 8099
CLOSED_PORT_TIMEOUT_SEC = 2.0
LIVE_RTT_MAX_MS = 500.0  # LAN 기준 넉넉한 상한
PROBABILITY_SUM_TOLERANCE = 1e-6
FILTERED_OPTIONS = ("hold", "return_home")


@pytest.fixture
def live_d1_url() -> str:
    url = os.environ.get("D1_SERVER_URL", "")
    if not url:
        pytest.skip("D1_SERVER_URL not set")

    if not SystemOneClient(url, LIVE_REQUEST_TIMEOUT_SEC).is_ready():
        pytest.skip(f"d1-3B llama-server not ready at {url}/health")

    return url


@pytest.fixture
def live_state_text() -> str:
    # test_decision_prompt.py 와 같은 작은 상태: drone 1개, person track 1개, 주기 확인 사건
    drone = DroneState(
        north_m=0.0,
        east_m=0.0,
        down_m=-40.0,
        altitude_m=40.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=0.0,
        gimbal_pitch_deg=-55.0,
        gimbal_yaw_deg=0.0,
        battery_pct=92.0,
        flight_mode="hover",
    )
    track = TrackState(
        track_id="T1",
        class_name="person",
        north_m=31.0,
        east_m=-6.0,
        first_seen_sec=7.5,
        last_seen_sec=12.0,
        observation_count=9,
        mean_confidence=0.71,
        is_active=True,
        upper_color=None,
    )
    mission = MissionSummary(target_class="person", target_upper_color=None, hearted_tracks=())
    state = SelectionState(time_sec=12.0, drone=drone, tracks=(track,), mission=mission)

    return serialize_request_state(state, SelectionEvent(EVENT_PERIODIC_CHECK, None))


def get_closed_url(live_url: str) -> str:
    closed_url = os.environ.get("D1_CLOSED_URL", "")
    if closed_url:
        return closed_url

    parts = urllib.parse.urlsplit(live_url)
    return f"{parts.scheme}://{parts.hostname}:{CLOSED_PORT_DEFAULT}"


def test_live_choice_from_full_menu(live_d1_url: str, live_state_text: str) -> None:
    client = SystemOneClient(live_d1_url, LIVE_REQUEST_TIMEOUT_SEC)
    instructions, criteria = build_rule_mode_question(RULE_MODES)

    reply = client.ask_choice(live_state_text, QUESTION_ID_RULE_MODE, instructions, criteria)

    assert reply.choice in RULE_MODES, reply
    assert set(reply.probabilities) <= set(RULE_MODES), reply
    assert abs(sum(reply.probabilities.values()) - 1.0) <= PROBABILITY_SUM_TOLERANCE, reply
    assert 0.0 <= reply.confidence <= 1.0, reply
    assert reply.total_ms > 0, reply
    assert reply.input_tokens is None or reply.input_tokens > 0, reply


def test_live_choice_stays_inside_filtered_options(live_d1_url: str, live_state_text: str) -> None:
    # 규칙이 걸러 준 보기 제한(D-02)이 실서버에서도 지켜진다
    client = SystemOneClient(live_d1_url, LIVE_REQUEST_TIMEOUT_SEC)
    instructions, criteria = build_rule_mode_question(FILTERED_OPTIONS)

    reply = client.ask_choice(live_state_text, QUESTION_ID_RULE_MODE, instructions, criteria)

    assert reply.choice in FILTERED_OPTIONS, reply
    assert set(reply.probabilities) <= set(FILTERED_OPTIONS), reply


def test_live_short_timeout_raises_timeout_error(live_d1_url: str, live_state_text: str) -> None:
    client = SystemOneClient(live_d1_url, TIMEOUT_REPRO_SEC)
    instructions, criteria = build_rule_mode_question(RULE_MODES)

    with pytest.raises(LLMTimeoutError):
        client.ask_choice(live_state_text, QUESTION_ID_RULE_MODE, instructions, criteria)


def test_live_closed_port_raises_unavailable(live_d1_url: str, live_state_text: str) -> None:
    closed = SystemOneClient(get_closed_url(live_d1_url), CLOSED_PORT_TIMEOUT_SEC)
    instructions, criteria = build_rule_mode_question(RULE_MODES)

    with pytest.raises(LLMUnavailableError):
        closed.ask_choice(live_state_text, QUESTION_ID_RULE_MODE, instructions, criteria)

    assert closed.is_ready() is False
    assert closed.measure_rtt_ms() is None


def test_live_rtt_probe_and_latency_split(live_d1_url: str, live_state_text: str) -> None:
    # SEL-02 분해 재료(total_ms, /health RTT)가 실서버에서 나오는지 확인한다
    client = SystemOneClient(live_d1_url, LIVE_REQUEST_TIMEOUT_SEC)
    instructions, criteria = build_rule_mode_question(RULE_MODES)

    rtt_ms = client.measure_rtt_ms()
    assert isinstance(rtt_ms, float), rtt_ms
    assert 0.0 < rtt_ms < LIVE_RTT_MAX_MS, rtt_ms

    reply = client.ask_choice(live_state_text, QUESTION_ID_RULE_MODE, instructions, criteria)
    orin_estimate_ms = max(0.0, reply.total_ms - rtt_ms)

    assert 0.0 <= orin_estimate_ms <= reply.total_ms, (reply, rtt_ms)
