"""llm_module 최상위 파사드 테스트 (IF-06, D-03, SC7).

이 파일은 "별도 스크립트가 공개 인터페이스만으로 쓴다" 를 흉내 낸다 — llm_module 서브모듈을 직접 import 하지 않고
`from llm_module import ...` 최상위 이름만 쓴다(첫 테스트가 이 파일 자신을 AST 로 검사한다).
"""

import ast
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import llm_module
from llm_module import (
    LLMModule,
    LLMModuleConfig,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModuleOutput,
    RuleBasedDecisionModel,
    StateJson,
    create_llm_module,
)
from tests.fakes import FIXTURES_DIR, FakeLLMBackend, FakeResponse, load_state_fixture, make_track

REQUIRED_EXPORTS = {
    "LLMModule", "LLMModuleConfig", "ModuleOutput", "create_llm_module", "StateJson", "Decision",
    "SituationReport", "LLMBackendError", "ReportFormatError",
}
FIXTURE_NAMES = sorted(path.stem for path in FIXTURES_DIR.glob("*.json"))
NO_TRACKS_FLAG = "no tracks in state; LLM skipped"
REPORT_ERROR_MAX_CHARS = 200
MAX_SENTENCES = 6
MAX_SENTENCE_CHARS = 240
MAX_CITED_TRACKS = 8


def _make_injected_module(responses: list) -> LLMModule:
    return LLMModule(decision_model=RuleBasedDecisionModel(), backend=FakeLLMBackend(responses))


# --- 공개 경계 ---


def test_this_file_imports_only_top_level_llm_module() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    internal_imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("llm_module.")
    ]

    assert internal_imports == []


def test_all_exports_required_names_and_every_name_exists() -> None:
    assert REQUIRED_EXPORTS <= set(llm_module.__all__)
    assert all(hasattr(llm_module, name) for name in llm_module.__all__)


def test_import_does_not_load_llama_server_backend(wavepilot_root: Path) -> None:
    script = "import sys, llm_module; print('llm_module.llama_server_backend' in sys.modules)"

    result = subprocess.run(
        [sys.executable, "-c", script], cwd=wavepilot_root, capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False"


# --- 기본 경로 (fake 백엔드) ---


def test_process_with_default_config_returns_grounded_report(hover_telemetry) -> None:
    module = create_llm_module(LLMModuleConfig())
    tracks = [make_track(1, "car"), make_track(2, "person", 32.0, 40.0)]

    output = module.process(tracks, hover_telemetry)

    assert isinstance(output, ModuleOutput)
    assert output.report is not None
    assert output.report.is_grounded
    assert output.report_error is None
    assert all(set(sentence.track_ids) <= {"T1", "T2"} for sentence in output.report.sentences)


def test_report_payload_matches_skeleton_shape(hover_telemetry) -> None:
    output = create_llm_module().process([make_track(1)], hover_telemetry)

    payload = json.loads(json.dumps(output.to_report_payload()))

    assert set(payload) == {"time_sec", "decision", "report", "report_error"}
    assert set(payload["decision"]) == {"verdict", "reason", "answers"}
    assert set(payload["report"]) == {"sentences", "flags", "is_grounded"}
    assert set(payload["report"]["sentences"][0]) == {"kind", "text", "track_ids"}


def test_process_state_accepts_fixture_dict() -> None:
    output = create_llm_module().process_state(load_state_fixture("three_tracks"))

    assert output.state.track_labels == ("T1", "T2", "T5")
    assert output.report is not None


def test_process_state_rejects_invalid_dict() -> None:
    data = load_state_fixture("three_tracks")
    data["tracks"][0]["track_id"] = 1

    with pytest.raises(ValueError):
        create_llm_module().process_state(data)


def test_process_state_truncates_to_max_tracks(hover_telemetry) -> None:
    tracks = [make_track(n, "car", 10.0 + n) for n in range(1, 26)]
    big_state = create_llm_module(LLMModuleConfig(max_tracks=100)).build_state(tracks, hover_telemetry)

    output = create_llm_module(LLMModuleConfig(max_tracks=20)).process_state(big_state.to_dict())

    assert len(output.state.tracks) == 20
    assert output.state.omitted_track_count == 5


def test_process_state_with_no_tracks_skips_report() -> None:
    output = create_llm_module().process_state(load_state_fixture("no_tracks"))

    assert output.report is not None
    assert output.report.sentences == ()
    assert output.report.flags == (NO_TRACKS_FLAG,)
    assert output.decision.verdict == "act"


def test_process_state_accepts_state_json_instance() -> None:
    state = StateJson.from_dict(load_state_fixture("single_track"))

    output = create_llm_module().process_state(state)

    assert output.state.track_labels == ("T3",)


# --- 실패 격리 (G3, G6) ---


@pytest.mark.parametrize(
    ("response", "expected_prefix"),
    [
        (LLMUnavailableError("connection refused"), "unavailable: "),
        (LLMTimeoutError("timed out after 60 s"), "timeout: "),
        (LLMResponseError("HTTP 500 " + "x" * 400), "response: "),
        (FakeResponse("not json at all"), "format: "),
    ],
)
def test_report_failure_becomes_report_error(response: object, expected_prefix: str) -> None:
    module = _make_injected_module([response])

    output = module.process_state(load_state_fixture("three_tracks"))

    assert output.report is None
    assert output.decision is not None
    assert output.report_error.startswith(expected_prefix)
    assert len(output.report_error) <= REPORT_ERROR_MAX_CHARS
    assert output.to_report_payload()["report"] is None


def test_generate_report_raises_backend_errors_directly() -> None:
    module = _make_injected_module([LLMUnavailableError("down")])
    state = StateJson.from_dict(load_state_fixture("three_tracks"))

    with pytest.raises(LLMUnavailableError):
        module.generate_report(state)


# --- FakeReportBackend 결정성·품질 ---


@pytest.mark.parametrize("fixture_name", [name for name in FIXTURE_NAMES if name != "no_tracks"])
def test_fake_report_backend_produces_clean_report(fixture_name: str) -> None:
    module = create_llm_module()
    data = load_state_fixture(fixture_name)

    first = module.process_state(data)
    second = module.process_state(load_state_fixture(fixture_name))
    report = first.report

    assert first.report_error is None
    assert report.is_grounded, report.flags
    assert 1 <= len(report.sentences) <= MAX_SENTENCES
    assert all(len(sentence.text) <= MAX_SENTENCE_CHARS for sentence in report.sentences)
    assert all(1 <= len(sentence.track_ids) <= MAX_CITED_TRACKS for sentence in report.sentences)
    assert first.to_report_payload() == second.to_report_payload()


def test_fake_report_backend_mentions_omitted_tracks() -> None:
    output = create_llm_module().process_state(load_state_fixture("omitted_tracks"))

    assert "3 more tracks not listed." in output.report.sentences[0].text


def test_fake_report_backend_hedges_low_confidence_track() -> None:
    output = create_llm_module().process_state(load_state_fixture("low_confidence_track"))

    suggestions = [sentence for sentence in output.report.sentences if sentence.kind == "suggestion"]
    assert suggestions
    assert "unconfirmed" in suggestions[0].text
    assert suggestions[0].track_ids == ("T7",)


# --- 준비 대기 ---


def test_wait_until_ready_returns_for_ready_backend() -> None:
    _make_injected_module([]).wait_until_ready(timeout_sec=0)


def test_wait_until_ready_raises_when_backend_never_ready() -> None:
    module = LLMModule(decision_model=RuleBasedDecisionModel(), backend=FakeLLMBackend([], ready=False))

    with pytest.raises(LLMUnavailableError):
        module.wait_until_ready(timeout_sec=0)


# --- 설정·팩토리 ---


@pytest.mark.parametrize(
    "kwargs", [{"backend": "openai"}, {"max_tracks": 0}, {"request_timeout_sec": 0}]
)
def test_config_rejects_invalid_values(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        LLMModuleConfig(**kwargs)


def test_create_llm_module_builds_llama_server_backend_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict] = []

    class RecordingLlamaServerBackend:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

        def complete_json(self, system_prompt, user_prompt, schema, max_tokens):
            raise AssertionError("not called in this test")

        def is_ready(self) -> bool:
            return True

    fake_module = types.ModuleType("llm_module.llama_server_backend")
    fake_module.LlamaServerBackend = RecordingLlamaServerBackend
    monkeypatch.setitem(sys.modules, "llm_module.llama_server_backend", fake_module)
    config = LLMModuleConfig(
        backend="llama_server", backend_url="http://10.0.0.5:8080", request_timeout_sec=30.0
    )

    create_llm_module(config)

    assert created == [{"base_url": "http://10.0.0.5:8080", "request_timeout_sec": 30.0}]
