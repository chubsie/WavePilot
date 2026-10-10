"""보고 스키마·파싱·생성기 테스트 (RPT-02, AI-SPEC D1~D3, D5, D6).

서버 없이 FakeLLMBackend 로 고정 응답(적대적 입력 포함)을 주입해 결정적으로 확인한다.
"""

import json

import pytest

from llm_module.backend import LLMTimeoutError, LLMUnavailableError
from llm_module.report_generator import NO_TRACKS_FLAG, ReportGenerator
from llm_module.report_prompt import SYSTEM_PROMPT, build_user_prompt
from llm_module.report_schema import (
    MAX_SENTENCE_CHARS,
    ReportFormatError,
    ReportSentence,
    SituationReport,
    build_report_schema,
    parse_report,
)
from llm_module.state import StateJson, truncate_state
from tests.fakes import FakeLLMBackend, FakeResponse, load_state_fixture

UNSUPPORTED_SCHEMA_KEYS = {
    "$ref", "uniqueItems", "anyOf", "oneOf", "allOf", "not", "if", "prefixItems",
    "patternProperties", "contains", "minimum", "maximum",
}
TRACE_KEYS = {
    "attempt", "max_tokens", "schema_track_ids", "finish_reason", "prompt_tokens",
    "completion_tokens", "latency_sec", "outcome",
}


class RecordingTraceSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)


def _load_state(name: str) -> StateJson:
    return StateJson.from_dict(load_state_fixture(name))


def _make_report_text(*sentences: tuple[str, str, list[str]]) -> str:
    return json.dumps(
        {"sentences": [{"kind": kind, "text": text, "track_ids": ids} for kind, text, ids in sentences]}
    )


def _get_track_ids_schema(schema: dict) -> dict:
    return schema["properties"]["sentences"]["items"]["properties"]["track_ids"]


def _collect_keys(node: object) -> set[str]:
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= _collect_keys(value)
        return keys
    if isinstance(node, list):
        return set().union(*(_collect_keys(item) for item in node)) if node else set()
    return set()


# --- 스키마 ---


def test_build_report_schema_uses_sorted_input_ids_and_limits() -> None:
    schema = build_report_schema({"T3", "T1"})
    sentences = schema["properties"]["sentences"]
    track_ids = _get_track_ids_schema(schema)

    assert track_ids["items"]["enum"] == ["T1", "T3"]
    assert (track_ids["minItems"], track_ids["maxItems"]) == (1, 8)
    assert (sentences["minItems"], sentences["maxItems"]) == (1, 6)
    assert sentences["items"]["properties"]["text"]["maxLength"] == MAX_SENTENCE_CHARS == 240
    assert schema["additionalProperties"] is False
    assert sentences["items"]["additionalProperties"] is False


def test_build_report_schema_avoids_unsupported_keywords() -> None:
    schema = build_report_schema({"T1", "T2", "T10"})

    assert not (_collect_keys(schema) & UNSUPPORTED_SCHEMA_KEYS)


# --- parse_report ---


def test_parse_report_accepts_grounded_sentence() -> None:
    text = _make_report_text(("observation", "One car is parked.", ["T1"]))

    report = parse_report(text, {"T1"})

    assert report.is_grounded
    assert report.sentences == (ReportSentence("observation", "One car is parked.", ("T1",)),)


@pytest.mark.parametrize(
    ("track_ids", "expected_flag"),
    [
        ([], "sentences[0] cites no track"),
        (["T99"], "cites unknown tracks"),
        (["EX-1"], "cites unknown tracks"),
    ],
)
def test_parse_report_flags_grounding_violations(track_ids: list[str], expected_flag: str) -> None:
    text = _make_report_text(("observation", "One car is parked.", track_ids))

    report = parse_report(text, {"T1"})

    assert not report.is_grounded
    assert any(expected_flag in flag for flag in report.flags)


INVALID_REPORT_TEXTS = {
    "not_json": "The car is parked.",
    "no_sentences_key": json.dumps({"report": []}),
    "empty_sentences": json.dumps({"sentences": []}),
    "kind_typo": _make_report_text(("observe", "One car.", ["T1"])),
    "missing_text": json.dumps({"sentences": [{"kind": "observation", "track_ids": ["T1"]}]}),
    "track_ids_string": json.dumps({"sentences": [{"kind": "observation", "text": "One car.", "track_ids": "T1"}]}),
    "seven_sentences": _make_report_text(*[("observation", "One car.", ["T1"])] * 7),
    "text_too_long": _make_report_text(("observation", "x" * 241, ["T1"])),
}


@pytest.mark.parametrize("case_name", sorted(INVALID_REPORT_TEXTS))
def test_parse_report_rejects_structural_errors(case_name: str) -> None:
    with pytest.raises(ReportFormatError):
        parse_report(INVALID_REPORT_TEXTS[case_name], {"T1"})


def test_situation_report_to_dict_shape() -> None:
    report = SituationReport(
        sentences=(ReportSentence("observation", "One car.", ("T1", "T2")),),
        flags=("sentences[0] cites no track",),
    )

    assert report.to_dict() == {
        "sentences": [{"kind": "observation", "text": "One car.", "track_ids": ["T1", "T2"]}],
        "flags": ["sentences[0] cites no track"],
        "is_grounded": False,
    }


# --- 생성기: 호출 모양 ---


def test_generator_skips_backend_when_no_tracks() -> None:
    backend = FakeLLMBackend([])

    report = ReportGenerator(backend).generate(_load_state("no_tracks"))

    assert backend.call_count == 0
    assert report.sentences == ()
    assert report.flags == (NO_TRACKS_FLAG,) == ("no tracks in state; LLM skipped",)


def test_generator_calls_backend_with_input_id_schema_and_fixed_prompts() -> None:
    state = _load_state("three_tracks")
    backend = FakeLLMBackend([FakeResponse(_make_report_text(("observation", "Two cars.", ["T1", "T2"])))])

    report = ReportGenerator(backend).generate(state)
    call = backend.calls[0]

    assert backend.call_count == 1
    assert _get_track_ids_schema(call.schema)["items"]["enum"] == sorted(state.track_labels)
    assert call.max_tokens == 640
    assert call.system_prompt == SYSTEM_PROMPT
    assert call.user_prompt == json.dumps(
        state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert call.user_prompt == build_user_prompt(state)
    assert report.is_grounded


def test_generator_schema_excludes_truncated_track_ids() -> None:
    full_state = _load_state("omitted_tracks")
    state = truncate_state(full_state, max_tracks=5)
    dropped_labels = set(full_state.track_labels) - set(state.track_labels)
    backend = FakeLLMBackend([FakeResponse(_make_report_text(("observation", "Cars.", [state.track_labels[0]])))])

    ReportGenerator(backend).generate(state)
    enum = set(_get_track_ids_schema(backend.calls[0].schema)["items"]["enum"])

    assert dropped_labels
    assert not (enum & dropped_labels)


# --- 생성기: 잘림·실패 ---


def test_generator_retries_once_with_doubled_max_tokens_after_length() -> None:
    ok_text = _make_report_text(("observation", "Two cars.", ["T1", "T2"]))
    backend = FakeLLMBackend([FakeResponse('{"sentences":[{"kind":"obs', "length"), FakeResponse(ok_text)])

    report = ReportGenerator(backend).generate(_load_state("three_tracks"))

    assert backend.call_count == 2
    assert backend.calls[1].max_tokens == 1280
    assert report.is_grounded


def test_generator_raises_after_second_length() -> None:
    truncated = FakeResponse('{"sentences":[{"kind":"obs', "length")
    backend = FakeLLMBackend([truncated, truncated])

    with pytest.raises(ReportFormatError, match="truncated"):
        ReportGenerator(backend).generate(_load_state("three_tracks"))

    assert backend.call_count == 2


@pytest.mark.parametrize("error", [LLMTimeoutError("timed out"), LLMUnavailableError("refused")])
def test_generator_propagates_backend_errors_without_retry(error: Exception) -> None:
    backend = FakeLLMBackend([error])

    with pytest.raises(type(error)):
        ReportGenerator(backend).generate(_load_state("three_tracks"))

    assert backend.call_count == 1


def test_generator_flags_restricted_term_but_keeps_sentence() -> None:
    text = _make_report_text(("observation", "A hostile car is parked.", ["T1"]))
    backend = FakeLLMBackend([FakeResponse(text)])

    report = ReportGenerator(backend).generate(_load_state("three_tracks"))

    assert report.sentences[0].text == "A hostile car is parked."
    assert "sentences[0] uses restricted term: hostile" in report.flags


# --- 고정 응답 12종 (AI-SPEC Reference Dataset 3) ---

FIXED_RESPONSE_CASES = {
    "normal": ([FakeResponse(_make_report_text(("observation", "Two cars are parked.", ["T1", "T2"])))], "ok", None),
    "empty_track_ids": ([FakeResponse(_make_report_text(("observation", "A car.", [])))], "flag", "cites no track"),
    "unknown_id": ([FakeResponse(_make_report_text(("observation", "A car.", ["T99"])))], "flag", "cites unknown tracks"),
    "example_id_copy": (
        [FakeResponse(_make_report_text(("observation", "Two vehicles.", ["EX-1", "EX-2"])))],
        "flag",
        "cites unknown tracks",
    ),
    "hostile_term": ([FakeResponse(_make_report_text(("observation", "A hostile car.", ["T1"])))], "flag", "hostile"),
    "engage_term": ([FakeResponse(_make_report_text(("suggestion", "Engage the car.", ["T1"])))], "flag", "engage"),
    "target_term": ([FakeResponse(_make_report_text(("observation", "The target is T5.", ["T5"])))], "flag", "target"),
    "suggestion_return_home": (
        [FakeResponse(_make_report_text(("suggestion", "Return to home now.", ["T5"])))],
        "flag",
        "return to home",
    ),
    "target_altitude_allowed": (
        [FakeResponse(_make_report_text(("observation", "Holding target altitude of 40 m near T1.", ["T1"])))],
        "ok",
        None,
    ),
    "not_json": ([FakeResponse("Two cars are parked.")], "error", None),
    "truncated_json": (
        [FakeResponse('{"sentences":[{"kind":"observation","te', "length")] * 2,
        "error",
        None,
    ),
    "missing_field": ([FakeResponse(json.dumps({"sentences": [{"kind": "observation", "text": "A car."}]}))], "error", None),
    "kind_typo": ([FakeResponse(_make_report_text(("observe", "A car.", ["T1"])))], "error", None),
    "seven_sentences": ([FakeResponse(_make_report_text(*[("observation", "A car.", ["T1"])] * 7))], "error", None),
}


@pytest.mark.parametrize("case_name", sorted(FIXED_RESPONSE_CASES))
def test_generator_fixed_responses(case_name: str) -> None:
    responses, expected_outcome, expected_flag = FIXED_RESPONSE_CASES[case_name]
    backend = FakeLLMBackend(list(responses))
    generator = ReportGenerator(backend)

    if expected_outcome == "error":
        with pytest.raises(ReportFormatError):
            generator.generate(_load_state("three_tracks"))
        return

    report = generator.generate(_load_state("three_tracks"))

    if expected_outcome == "ok":
        assert report.is_grounded, report.flags
    else:
        assert any(expected_flag in flag for flag in report.flags), report.flags


# --- trace ---


def test_trace_sink_records_one_event_per_attempt() -> None:
    sink = RecordingTraceSink()
    ok_text = _make_report_text(("observation", "Two cars.", ["T1", "T2"]))
    backend = FakeLLMBackend([FakeResponse("{", "length"), FakeResponse(ok_text)])

    ReportGenerator(backend, trace_sink=sink).generate(_load_state("three_tracks"))

    assert len(sink.events) == 2
    assert all(TRACE_KEYS <= set(event) for event in sink.events)
    assert [event["attempt"] for event in sink.events] == [1, 2]
    assert [event["outcome"] for event in sink.events] == ["truncated", "ok"]


def test_trace_sink_records_skip_for_no_tracks() -> None:
    sink = RecordingTraceSink()

    ReportGenerator(FakeLLMBackend([]), trace_sink=sink).generate(_load_state("no_tracks"))

    assert [event["outcome"] for event in sink.events] == ["skipped_no_tracks"]
