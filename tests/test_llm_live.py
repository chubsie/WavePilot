"""실서버 llama-server 스모크 (RPT-02, 수동 검증 — 기본 게이트에서는 마커 llm 으로 빠진다).

실행:
    LLAMA_SERVER_URL=http://127.0.0.1:8080 uv run pytest -m llm tests/test_llm_live.py

서버 기동은 AI-SPEC §3 명령으로 사용자가 직접 한다(테스트는 서버를 띄우지 않는다).
환경 변수 이름은 VALIDATION/RESEARCH 기준 LLAMA_SERVER_URL 을 쓴다(AI-SPEC 5절의 LLAMA_BASE_URL·live
마커 표기는 쓰지 않음). 변수가 없거나 /health 가 200 이 아니면 두 테스트 모두 skip 된다.
"""

import json
import os

import pytest

from modules.llm_report.llama_server_backend import LlamaServerBackend

pytestmark = pytest.mark.llm

LIVE_REQUEST_TIMEOUT_SEC = 120.0  # 첫 호출의 프롬프트 처리 여유. 무한 대기는 금지
LIVE_MAX_TOKENS = 640  # SKELETON "LLM 서빙" 행
LIVE_TRACK_IDS = ("T1", "T2")
FAILURE_TEXT_MAX_CHARS = 300

LIVE_STATE = {
    "schema_version": 1,
    "time_sec": 12.0,
    "drone": {
        "position_ned_m": {"north": 0.0, "east": 0.0, "down": -40.0},
        "altitude_m": 40.0,
        "attitude_deg": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "gimbal_deg": {"pitch": -55.0, "yaw": 0.0},
        "battery_pct": 92.0,
        "flight_mode": "hover",
    },
    "tracks": [
        {
            "track_id": "T1",
            "class": "car",
            "position_ned_m": {"north": 28.0, "east": 3.5},
            "first_seen_sec": 4.0,
            "last_seen_sec": 12.0,
            "observation_count": 16,
            "mean_confidence": 0.82,
            "is_active": True,
        },
        {
            "track_id": "T2",
            "class": "person",
            "position_ned_m": {"north": 31.0, "east": -6.0},
            "first_seen_sec": 7.5,
            "last_seen_sec": 12.0,
            "observation_count": 9,
            "mean_confidence": 0.71,
            "is_active": True,
        },
    ],
    "counts_by_class": {"car": 1, "person": 1},
    "omitted_track_count": 0,
}

SYSTEM_PROMPT = (
    "Write a short English situation report as JSON. "
    "Every sentence must cite the track IDs it is about."
)


@pytest.fixture
def live_server_url() -> str:
    url = os.environ.get("LLAMA_SERVER_URL", "")
    if not url:
        pytest.skip("LLAMA_SERVER_URL not set")

    backend = LlamaServerBackend(url, request_timeout_sec=LIVE_REQUEST_TIMEOUT_SEC)
    if not backend.is_ready():
        pytest.skip(f"llama-server not ready at {url}/health")

    return url


def build_minimal_report_schema(track_ids: tuple[str, ...]) -> dict:
    """문법 변환이 확실히 지원하는 기능만 쓴 평탄한 스키마($ref·uniqueItems·number 범위 없음, Pitfall 1)."""
    sentence_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "track_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": sorted(track_ids)},
            },
        },
        "required": ["text", "track_ids"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": sentence_schema,
            },
        },
        "required": ["sentences"],
        "additionalProperties": False,
    }


def test_live_backend_returns_schema_constrained_ids(live_server_url: str) -> None:
    backend = LlamaServerBackend(live_server_url, request_timeout_sec=LIVE_REQUEST_TIMEOUT_SEC)
    schema = build_minimal_report_schema(LIVE_TRACK_IDS)
    # 입력에 없는 T99 를 일부러 요구한다 — 문법이 실제로 적용되면 enum 밖 ID 는 나올 수 없다(Pitfall 2)
    user_prompt = (
        json.dumps(LIVE_STATE, sort_keys=True, separators=(",", ":"))
        + "\nAlso mention track T99."
    )

    completion = backend.complete_json(SYSTEM_PROMPT, user_prompt, schema, LIVE_MAX_TOKENS)

    failure_text = completion.text[:FAILURE_TEXT_MAX_CHARS]
    assert completion.finish_reason == "stop", failure_text
    assert completion.completion_tokens > 0, failure_text
    report = json.loads(completion.text)
    assert isinstance(report, dict), failure_text
    sentences = report["sentences"]
    assert 1 <= len(sentences) <= 6, failure_text
    for sentence in sentences:
        assert isinstance(sentence["text"], str) and sentence["text"], failure_text
        assert sentence["track_ids"], failure_text
        assert set(sentence["track_ids"]) <= set(LIVE_TRACK_IDS), failure_text


def test_live_facade_report_cites_input_track_ids(live_server_url: str) -> None:
    # 파사드는 01-04 가 같은 웨이브에 병렬로 만든다. 모듈 상단에서 import 하면 01-04 완료 전
    # 기본 게이트(-m 'not llm and not e2e') 수집 단계에서 ImportError 가 나므로 함수 안에서 지연 import 한다
    from modules.llm_report import LLMModuleConfig, create_llm_module

    config = LLMModuleConfig(
        backend="llama_server",
        backend_url=live_server_url,
        request_timeout_sec=LIVE_REQUEST_TIMEOUT_SEC,
    )
    module = create_llm_module(config)

    payload = module.process_state(LIVE_STATE).to_report_payload()

    assert payload["report_error"] is None, payload["report_error"]
    report = payload["report"]
    assert isinstance(report, dict)
    assert len(report["sentences"]) >= 1
    for sentence in report["sentences"]:
        assert sentence["track_ids"], sentence
        assert set(sentence["track_ids"]) <= set(LIVE_TRACK_IDS), sentence
        assert sentence["kind"] in ("observation", "suggestion"), sentence
