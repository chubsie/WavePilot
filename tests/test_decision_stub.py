"""규칙 기반 DecisionModel stub 테스트 (IF-05, D-05).

세 타입 질문(점수·선택·예/아니오)에 확률(합 1)이 붙은 답과 act/escalate 판정이 결정적으로 나오는지 본다.
"""

import pytest

from modules.llm_report.decision import (
    DEFAULT_QUESTIONS,
    QUESTION_ID_NEXT_ACTION,
    QUESTION_ID_REPORT_NEEDED,
    QUESTION_ID_URGENCY,
    Answer,
    ChoiceQuestion,
    Decision,
    DecisionModel,
    RuleBasedDecisionModel,
    ScoreQuestion,
    YesNoQuestion,
)
from modules.llm_report.state import StateJson, build_state_json
from tests.fakes import load_state_fixture, make_track

PROBABILITY_TOLERANCE = 1e-6


def _decide(tracks, telemetry) -> Decision:
    state = build_state_json(tracks, telemetry)
    return RuleBasedDecisionModel().decide(state, DEFAULT_QUESTIONS)


def _get_answer(decision: Decision, question_id: str) -> Answer:
    return next(answer for answer in decision.answers if answer.question_id == question_id)


def _get_probability_key(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# --- 규칙 ---


def test_no_tracks_gives_urgency_zero_and_act(hover_telemetry) -> None:
    decision = _decide([], hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 0
    assert _get_answer(decision, QUESTION_ID_NEXT_ACTION).value == "maintain"
    assert _get_answer(decision, QUESTION_ID_REPORT_NEEDED).value is False
    assert decision.verdict == "act"


def test_single_confident_car_gives_urgency_one_and_verify(hover_telemetry) -> None:
    decision = _decide([make_track(1, "car", mean_confidence=0.9)], hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 1
    assert _get_answer(decision, QUESTION_ID_NEXT_ACTION).value == "verify"
    assert _get_answer(decision, QUESTION_ID_REPORT_NEEDED).value is True
    assert decision.verdict == "act"


def test_single_person_gives_urgency_two_and_alert_operator(hover_telemetry) -> None:
    decision = _decide([make_track(2, "person")], hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 2
    assert _get_answer(decision, QUESTION_ID_NEXT_ACTION).value == "alert_operator"


def test_person_near_car_gives_urgency_three(hover_telemetry) -> None:
    tracks = [make_track(1, "car", 30.0, 0.0), make_track(2, "person", 30.0, 10.0)]

    decision = _decide(tracks, hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 3
    assert decision.verdict == "act"


def test_three_people_give_urgency_three(hover_telemetry) -> None:
    tracks = [make_track(n, "person", 30.0 + 5.0 * n) for n in (1, 2, 3)]

    decision = _decide(tracks, hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 3


def test_stale_person_only_gives_urgency_zero() -> None:
    state = StateJson.from_dict(load_state_fixture("stale_track"))

    decision = RuleBasedDecisionModel().decide(state, DEFAULT_QUESTIONS)

    assert _get_answer(decision, QUESTION_ID_URGENCY).value == 0
    assert decision.verdict == "act"


def test_new_track_marks_report_needed(hover_telemetry) -> None:
    state = StateJson.from_dict(load_state_fixture("single_track"))

    decision = RuleBasedDecisionModel().decide(state, DEFAULT_QUESTIONS)

    assert _get_answer(decision, QUESTION_ID_REPORT_NEEDED).value is True


# --- 확률 모양 ---


@pytest.mark.parametrize(
    "fixture_name",
    ["no_tracks", "single_track", "three_tracks", "mixed_five_tracks", "low_confidence_track"],
)
def test_answers_have_normalized_probabilities_with_argmax_value(fixture_name: str) -> None:
    state = StateJson.from_dict(load_state_fixture(fixture_name))

    decision = RuleBasedDecisionModel().decide(state, DEFAULT_QUESTIONS)

    for answer in decision.answers:
        value_key = _get_probability_key(answer.value)
        assert abs(sum(answer.probabilities.values()) - 1.0) <= PROBABILITY_TOLERANCE
        assert answer.probabilities[value_key] == max(answer.probabilities.values())


def test_answer_value_types_follow_question_types(hover_telemetry) -> None:
    decision = _decide([make_track(1)], hover_telemetry)

    urgency = _get_answer(decision, QUESTION_ID_URGENCY).value
    next_action = _get_answer(decision, QUESTION_ID_NEXT_ACTION).value
    report_needed = _get_answer(decision, QUESTION_ID_REPORT_NEEDED).value

    assert isinstance(urgency, int) and not isinstance(urgency, bool)
    assert isinstance(next_action, str)
    assert isinstance(report_needed, bool)


# --- escalate ---


def test_low_confidence_person_escalates_with_label_in_reason(hover_telemetry) -> None:
    decision = _decide([make_track(7, "person", mean_confidence=0.4)], hover_telemetry)

    assert _get_answer(decision, QUESTION_ID_URGENCY).confidence == 0.5
    assert decision.verdict == "escalate"
    assert "T7" in decision.reason


def test_ambiguous_person_vehicle_distance_escalates(hover_telemetry) -> None:
    tracks = [make_track(1, "car", 30.0, 0.0), make_track(2, "person", 30.0, 14.0)]

    decision = _decide(tracks, hover_telemetry)

    assert decision.verdict == "escalate"


def test_reason_avoids_restricted_words(hover_telemetry) -> None:
    decision = _decide([make_track(7, "person", mean_confidence=0.4)], hover_telemetry)

    lowered = decision.reason.lower()
    assert "threat" not in lowered
    assert "target" not in lowered


# --- 질문 검증 ---


def test_unknown_question_id_raises(hover_telemetry) -> None:
    state = build_state_json([], hover_telemetry)
    questions = [YesNoQuestion(id="weather_ok", instructions="Is the weather fine?")]

    with pytest.raises(ValueError, match="weather_ok"):
        RuleBasedDecisionModel().decide(state, questions)


def test_score_question_with_other_range_raises(hover_telemetry) -> None:
    state = build_state_json([], hover_telemetry)
    questions = [ScoreQuestion(id="urgency", instructions="Rate urgency.", min_score=0, max_score=5)]

    with pytest.raises(ValueError):
        RuleBasedDecisionModel().decide(state, questions)


def test_choice_question_without_chosen_option_raises(hover_telemetry) -> None:
    state = build_state_json([], hover_telemetry)
    questions = [
        ChoiceQuestion(id="next_action", instructions="Pick one.", options=("verify", "alert_operator")),
    ]

    with pytest.raises(ValueError):
        RuleBasedDecisionModel().decide(state, questions)


def test_question_type_mismatch_raises(hover_telemetry) -> None:
    state = build_state_json([], hover_telemetry)
    questions = [YesNoQuestion(id="urgency", instructions="Is it urgent?")]

    with pytest.raises(ValueError):
        RuleBasedDecisionModel().decide(state, questions)


def test_answer_rejects_probabilities_not_summing_to_one() -> None:
    with pytest.raises(ValueError):
        Answer(question_id="q", value="a", probabilities={"a": 0.6, "b": 0.6}, confidence=0.6)


def test_answer_rejects_value_that_is_not_argmax() -> None:
    with pytest.raises(ValueError):
        Answer(question_id="q", value="b", probabilities={"a": 0.9, "b": 0.1}, confidence=0.9)


# --- 직렬화·Protocol ---


def test_decision_to_dict_shape(hover_telemetry) -> None:
    decision = _decide([make_track(1)], hover_telemetry).to_dict()

    assert list(decision) == ["verdict", "reason", "answers"]
    assert list(decision["answers"][0]) == ["question_id", "value", "probabilities", "confidence"]


def test_rule_based_model_satisfies_protocol() -> None:
    assert isinstance(RuleBasedDecisionModel(), DecisionModel)
