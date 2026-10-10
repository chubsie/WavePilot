"""01-REVIEW CR-02·WR-02·WR-06 회귀 테스트 — d1 지연 범위, probe 실패 시 POST 생략, probe timeout 상한, base_url 검증.

HTTP 서버는 띄우지 않는다. probe timeout 실측만 응답하지 않는 로컬 소켓을 쓴다.
"""

import socket
import time

import pytest

from modules.decision.d1_selector import D1SelectionError, D1Selector, estimate_latency
from modules.decision.interface import (
    EVENT_PERIODIC_CHECK,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    DroneState,
    Latency,
    MissionSummary,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.systemone_client import HEALTH_TIMEOUT_SEC, ChoiceReply, SystemOneClient
from modules.llm_report.interface import LLMTimeoutError

PROBE_DELAY_SEC = 0.05
PROBE_DELAY_MS = PROBE_DELAY_SEC * 1000.0
FAKE_RTT_MS = 5.0
FAKE_REQUEST_MS = 10.0
SHORT_REQUEST_TIMEOUT_SEC = 0.1
LONG_REQUEST_TIMEOUT_SEC = 3.0


# --- 상태·가짜 클라이언트 ---


def _make_state() -> SelectionState:
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


def _make_event() -> SelectionEvent:
    return SelectionEvent(kind=EVENT_PERIODIC_CHECK, mode_result=None)


def _make_reply(choice: str = "search") -> ChoiceReply:
    options = filter_options(_make_state(), _make_event(), ParamDefaults().approach_stop_distance_m)
    rest = 0.4 / (len(options) - 1)
    probabilities = {option: (0.6 if option == choice else rest) for option in options}

    return ChoiceReply(choice=choice, probabilities=probabilities, confidence=0.8, input_tokens=10, total_ms=FAKE_REQUEST_MS)


class SlowProbeClient:
    """probe 가 PROBE_DELAY_SEC 만큼 실제로 막힌 뒤 rtt_ms 를 돌려주는 가짜 클라이언트."""

    def __init__(self, rtt_ms: float | None = FAKE_RTT_MS, error: Exception | None = None) -> None:
        self.rtt_ms = rtt_ms
        self.error = error
        self.ask_count = 0

    def measure_rtt_ms(self) -> float | None:
        time.sleep(PROBE_DELAY_SEC)
        return self.rtt_ms

    def ask_choice(self, state_text: str, question_id: str, instructions: str, criteria: dict[str, str]) -> ChoiceReply:
        self.ask_count += 1
        if self.error is not None:
            raise self.error
        return _make_reply()


def _select(client: SlowProbeClient):
    return D1Selector(client, ParamDefaults(), 0.3).select(_make_state(), _make_event())


# --- CR-02: total_ms 는 select 전체 벽시계 ---


def test_success_total_includes_slow_probe_and_orin_uses_post_only():
    result = _select(SlowProbeClient())

    assert result.latency.total_ms >= PROBE_DELAY_MS
    assert result.latency.orin_processing_ms == FAKE_REQUEST_MS - FAKE_RTT_MS
    assert result.latency.network_rtt_ms == FAKE_RTT_MS


def test_failure_total_includes_slow_probe():
    client = SlowProbeClient(error=LLMTimeoutError("slow"))

    with pytest.raises(D1SelectionError) as raised:
        _select(client)

    assert raised.value.reason == FALLBACK_TIMEOUT
    assert raised.value.latency.total_ms >= PROBE_DELAY_MS
    assert raised.value.latency.orin_processing_ms is None
    assert raised.value.latency.network_rtt_ms == FAKE_RTT_MS


def test_estimate_latency_splits_request_not_selection():
    assert estimate_latency(200.0, 120.0, 20.0) == Latency(200.0, 100.0, 20.0)
    assert estimate_latency(200.0, 120.0, 150.0) == Latency(200.0, 0.0, 150.0)
    assert estimate_latency(200.0, 120.0, None) == Latency(200.0, None, None)


# --- WR-02: probe 실패면 POST 를 보내지 않는다 ---


def test_probe_failure_skips_post_and_raises_unavailable():
    client = SlowProbeClient(rtt_ms=None)

    with pytest.raises(D1SelectionError, match="health probe failed") as raised:
        _select(client)

    assert raised.value.reason == FALLBACK_UNAVAILABLE
    assert client.ask_count == 0
    assert raised.value.latency.total_ms >= PROBE_DELAY_MS
    assert raised.value.latency.orin_processing_ms is None
    assert raised.value.latency.network_rtt_ms is None


@pytest.mark.parametrize(
    ("request_timeout_sec", "expected_probe_timeout_sec"),
    [(SHORT_REQUEST_TIMEOUT_SEC, SHORT_REQUEST_TIMEOUT_SEC), (LONG_REQUEST_TIMEOUT_SEC, HEALTH_TIMEOUT_SEC)],
    ids=["capped_by_request_timeout", "capped_by_health_timeout"],
)
def test_probe_timeout_is_min_of_health_and_request(
    monkeypatch: pytest.MonkeyPatch, request_timeout_sec: float, expected_probe_timeout_sec: float
):
    used_timeouts: list[float] = []

    def record_fetch_json(self, request, timeout_sec: float) -> dict:
        used_timeouts.append(timeout_sec)
        return {"status": "ok"}

    monkeypatch.setattr(SystemOneClient, "_fetch_json", record_fetch_json)
    client = SystemOneClient("http://127.0.0.1:8081", request_timeout_sec)

    client.measure_rtt_ms()
    client.is_ready()

    assert used_timeouts == [expected_probe_timeout_sec, expected_probe_timeout_sec]


def test_probe_against_silent_socket_gives_up_within_request_timeout():
    # listen 만 하고 응답하지 않는 로컬 소켓 — connect 는 되고 본문 읽기에서 timeout 이 난다
    with socket.create_server(("127.0.0.1", 0)) as silent_server:
        port = silent_server.getsockname()[1]
        client = SystemOneClient(f"http://127.0.0.1:{port}", SHORT_REQUEST_TIMEOUT_SEC)

        started_sec = time.monotonic()
        rtt_ms = client.measure_rtt_ms()
        elapsed_sec = time.monotonic() - started_sec

    assert rtt_ms is None
    assert elapsed_sec < HEALTH_TIMEOUT_SEC


# --- WR-06: base_url 검증 ---


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("http://orin:abc", "invalid port"),
        ("http://orin:99999", "invalid port"),
        ("http://:8081", "with a host"),
        (" http://orin:8081", "whitespace"),
        ("http://orin:8081 ", "whitespace"),
    ],
    ids=["non_numeric_port", "port_out_of_range", "no_host", "leading_space", "trailing_space"],
)
def test_client_rejects_malformed_base_url(base_url: str, message: str):
    with pytest.raises(ValueError, match=message):
        SystemOneClient(base_url, 1.0)


@pytest.mark.parametrize("base_url", ["http://192.168.0.157:8081", "https://orin.local", "http://orin:8081/"])
def test_client_accepts_well_formed_base_url(base_url: str):
    assert "SystemOneClient(" in repr(SystemOneClient(base_url, 1.0))
