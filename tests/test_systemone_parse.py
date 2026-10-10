"""/v1/systemone 요청 모양·응답 파서·생성자 검증 테스트 (SEL-01, D-07, D-11).

응답 파서는 dict 를 받는 순수 함수라 소켓·HTTP 없이 테스트한다. SystemOneClient 는 생성자 검증과
repr 만 보고 메서드는 부르지 않는다. 실서버 경로는 tests/test_systemone_live.py(마커 d1)가 맡는다.
"""

import copy
import math

import pytest

from modules.decision.systemone_client import (
    HEALTH_PATH,
    SYSTEMONE_PATH,
    ChoiceReply,
    OutOfMenuError,
    SystemOneClient,
    build_systemone_payload,
    parse_choice_response,
)
from modules.llm_report.interface import LLMResponseError

QUESTION_ID = "rule_mode"
ALLOWED = ("search", "hold", "return_home")
TOTAL_MS = 12.5
DELETE = object()  # make_body 에서 "이 키를 지운다" 표시

# RESEARCH §2.2 응답 예시 그대로
EXAMPLE_BODY = {
    "model": "d1-3b",
    "answers": {
        "rule_mode": {
            "type": "choice",
            "choice": "search",
            "probabilities": {"search": 0.81, "hold": 0.12, "return_home": 0.07},
            "confidence": 0.715,
        }
    },
    "usage": {"input_tokens": 312, "output_tokens": 0},
}


def make_body(**answer_overrides: object) -> dict:
    # 정상 body 를 복사하고 answer 필드만 바꾼다. 값이 DELETE 면 그 키를 지운다
    body = copy.deepcopy(EXAMPLE_BODY)
    answer = body["answers"][QUESTION_ID]
    for key, value in answer_overrides.items():
        if value is DELETE:
            del answer[key]
        else:
            answer[key] = value

    return body


# --- parse_choice_response: 정상 ---


def test_parse_valid_body_returns_choice_reply() -> None:
    reply = parse_choice_response(make_body(), QUESTION_ID, ALLOWED, TOTAL_MS)

    assert isinstance(reply, ChoiceReply)
    assert reply.choice == "search"
    assert reply.probabilities == {"search": 0.81, "hold": 0.12, "return_home": 0.07}
    assert reply.confidence == 0.715
    assert reply.input_tokens == 312
    assert reply.total_ms == TOTAL_MS


def test_parse_reorders_probabilities_by_allowed_as_floats() -> None:
    # 서버가 키 순서를 바꾸고 정수 확률을 줘도 allowed 순서의 float dict 가 된다
    body = make_body(choice="hold", probabilities={"return_home": 0, "hold": 1, "search": 0}, confidence=1)

    reply = parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert list(reply.probabilities) == list(ALLOWED)
    assert all(type(value) is float for value in reply.probabilities.values())
    assert reply.probabilities is not body["answers"][QUESTION_ID]["probabilities"]


def test_parse_accepts_sum_within_tolerance() -> None:
    # 서버 확률은 반올림되지 않으므로 1 ± 1e-7 는 통과해야 한다
    body = make_body(probabilities={"search": 0.81 + 1e-7, "hold": 0.12, "return_home": 0.07})

    reply = parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert reply.choice == "search"


def test_parse_accepts_probabilities_subset_of_allowed() -> None:
    body = make_body(probabilities={"search": 0.9, "hold": 0.1})

    reply = parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert set(reply.probabilities) == {"search", "hold"}


# --- parse_choice_response: 보기 밖 ---


def test_choice_outside_allowed_raises_out_of_menu() -> None:
    body = make_body(choice="approach")

    with pytest.raises(OutOfMenuError) as error_info:
        parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert isinstance(error_info.value, LLMResponseError)
    assert "approach" in str(error_info.value)


def test_probability_key_outside_allowed_raises_out_of_menu() -> None:
    body = make_body(probabilities={"search": 0.8, "hold": 0.1, "aim_and_fire": 0.1})

    with pytest.raises(OutOfMenuError) as error_info:
        parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert "aim_and_fire" in str(error_info.value)


# --- parse_choice_response: 모양·값 이상 → LLMResponseError (OutOfMenuError 아님) ---


def _body_without_answers() -> dict:
    body = make_body()
    del body["answers"]
    return body


def _body_with_answers(answers: object) -> dict:
    body = make_body()
    body["answers"] = answers
    return body


BAD_BODIES = {
    "no_answers": _body_without_answers(),
    "answers_not_dict": _body_with_answers(["rule_mode"]),
    "missing_question_id": _body_with_answers({"other": EXAMPLE_BODY["answers"][QUESTION_ID]}),
    "answer_not_dict": _body_with_answers({QUESTION_ID: "search"}),
    "type_not_choice": make_body(type="score"),
    "type_missing": make_body(type=DELETE),
    "choice_not_str": make_body(choice=1),
    "choice_missing": make_body(choice=DELETE),
    "probabilities_not_dict": make_body(probabilities=[0.81, 0.12, 0.07]),
    "probability_bool": make_body(probabilities={"search": True, "hold": 0.0, "return_home": 0.0}),
    "probability_str": make_body(probabilities={"search": "0.81", "hold": 0.12, "return_home": 0.07}),
    "probability_nan": make_body(probabilities={"search": math.nan, "hold": 0.12, "return_home": 0.07}),
    "probability_negative": make_body(probabilities={"search": 0.9, "hold": -0.1, "return_home": 0.2}),
    "probability_above_one": make_body(probabilities={"search": 1.5, "hold": 0.0, "return_home": 0.0}),
    "probability_sum_0_9": make_body(probabilities={"search": 0.71, "hold": 0.12, "return_home": 0.07}),
    "choice_not_in_probabilities": make_body(probabilities={"hold": 0.6, "return_home": 0.4}),
    "choice_not_argmax": make_body(choice="hold"),
    "confidence_missing": make_body(confidence=DELETE),
    "confidence_bool": make_body(confidence=True),
    "confidence_above_one": make_body(confidence=1.5),
    "confidence_nan": make_body(confidence=math.nan),
}


@pytest.mark.parametrize("body", list(BAD_BODIES.values()), ids=list(BAD_BODIES))
def test_malformed_answer_raises_response_error_not_out_of_menu(body: dict) -> None:
    with pytest.raises(LLMResponseError) as error_info:
        parse_choice_response(body, QUESTION_ID, ALLOWED, TOTAL_MS)

    assert not isinstance(error_info.value, OutOfMenuError)


# --- parse_choice_response: usage 는 선택 정보 ---


def _body_with_usage(usage: object) -> dict:
    body = make_body()
    if usage is DELETE:
        del body["usage"]
    else:
        body["usage"] = usage
    return body


@pytest.mark.parametrize(
    "usage",
    [DELETE, {"input_tokens": True}, {"input_tokens": -1}, {"input_tokens": "312"}, "312"],
    ids=["missing", "bool", "negative", "str", "usage_not_dict"],
)
def test_bad_usage_gives_none_input_tokens(usage: object) -> None:
    reply = parse_choice_response(_body_with_usage(usage), QUESTION_ID, ALLOWED, TOTAL_MS)

    assert reply.input_tokens is None


# --- parse_choice_response: 인자 검증 ---


def test_empty_allowed_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_choice_response(make_body(), QUESTION_ID, (), TOTAL_MS)


@pytest.mark.parametrize("total_ms", [-1.0, math.nan, math.inf], ids=["negative", "nan", "inf"])
def test_bad_total_ms_raises_value_error(total_ms: float) -> None:
    with pytest.raises(ValueError):
        parse_choice_response(make_body(), QUESTION_ID, ALLOWED, total_ms)


# --- build_systemone_payload ---


def test_payload_matches_systemone_shape() -> None:
    payload = build_systemone_payload('{"a":1}', "rule_mode", "Choose.", {"hold": "d1", "return_home": "d2"})

    assert payload == {
        "state": '{"a":1}',
        "questions": {
            "rule_mode": {
                "type": "choice",
                "instructions": "Choose.",
                "criteria": {"hold": "d1", "return_home": "d2"},
            }
        },
    }


def test_payload_copies_criteria() -> None:
    criteria = {"hold": "d1"}

    payload = build_systemone_payload('{"a":1}', "rule_mode", "Choose.", criteria)
    criteria["return_home"] = "d2"

    assert payload["questions"]["rule_mode"]["criteria"] == {"hold": "d1"}


@pytest.mark.parametrize(
    ("state_text", "criteria"),
    [("", {"hold": "d1"}), ('{"a":1}', {})],
    ids=["empty_state", "empty_criteria"],
)
def test_payload_rejects_empty_inputs(state_text: str, criteria: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_systemone_payload(state_text, "rule_mode", "Choose.", criteria)


# --- SystemOneClient 생성자 (네트워크 없음) ---


@pytest.mark.parametrize("base_url", ["file:///etc/passwd", "localhost:8081"], ids=["file_scheme", "no_scheme"])
def test_client_rejects_bad_base_url(base_url: str) -> None:
    with pytest.raises(ValueError):
        SystemOneClient(base_url, 1.0)


@pytest.mark.parametrize("timeout_sec", [0, -1, math.inf, math.nan], ids=["zero", "negative", "inf", "nan"])
def test_client_rejects_bad_timeout(timeout_sec: float) -> None:
    with pytest.raises(ValueError):
        SystemOneClient("http://127.0.0.1:8081", timeout_sec)


def test_client_construction_strips_slash_and_shows_repr() -> None:
    client = SystemOneClient("http://127.0.0.1:8081/", 1.5)

    text = repr(client)

    assert "'http://127.0.0.1:8081'" in text
    assert "8081/'" not in text
    assert "1.5" in text


def test_paths_and_error_hierarchy() -> None:
    assert SYSTEMONE_PATH == "/v1/systemone"
    assert HEALTH_PATH == "/health"
    assert issubclass(OutOfMenuError, LLMResponseError)
