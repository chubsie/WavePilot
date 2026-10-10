"""판단 모델 계약과 규칙 기반 stub (IF-05, D-05).

DecisionModel 은 상태 JSON 과 타입 질문(점수·선택·예/아니오)을 받아 확률이 붙은 답과 act/escalate 판정을 낸다.
Phase 02.1 에서 d1-3B 어댑터가 같은 Protocol 로 들어오므로, 질문 문구(instructions)는 그대로 쓸 수 있는 영어 한 문장이다.

RuleBasedDecisionModel 은 Phase 1 의 결정적 stub 이다:
- 활성 track 만 본다 — 지금 안 보이는(stale) track 으로 경보하지 않는다.
- 상태를 저장하지 않는다. "새 track" 은 first_seen_sec 이 보고 주기 창 안에 있는지로 판단한다.
- 보고를 생략하는 게이팅은 하지 않는다. report_needed 는 답으로만 싣는다(DEC-03 은 Phase 02.1).
- reason 문자열에 판정 어휘(threat, target 등)를 쓰지 않는다 — 운용자 콘솔에 그대로 찍힌다.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from modules.llm_report.state import StateJson, TrackStateJson

PROBABILITY_TOLERANCE = 1e-6
VERDICTS = ("act", "escalate")

QUESTION_ID_URGENCY = "urgency"
QUESTION_ID_NEXT_ACTION = "next_action"
QUESTION_ID_REPORT_NEEDED = "report_needed"
URGENCY_MIN = 0
URGENCY_MAX = 3
NEXT_ACTIONS = ("maintain", "verify", "alert_operator")

VEHICLE_CLASSES = ("car", "bus", "truck")
PERSON_CLASS = "person"
PERSON_NEAR_VEHICLE_M = 15.0  # 사람이 차량에 이만큼 가까우면 승하차·접촉 가능성으로 본다
PERSON_GROUP_MIN = 3  # 이 이상 모이면 무리로 본다
AMBIGUOUS_MARGIN_M = 3.0  # 거리 경계 ± 이 안이면 위치 오차로 판정이 뒤집힐 수 있다
LOW_CONFIDENCE = 0.5  # 평균 탐지 신뢰도가 이보다 낮으면 클래스 자체가 불확실
MIN_OBSERVATIONS = 3  # 이보다 적게 본 track 은 일시적 오탐일 수 있다
CLEAR_CONFIDENCE = 0.9  # 규칙이 분명히 맞을 때의 답 확신도
AMBIGUOUS_CONFIDENCE = 0.5  # 모호한 근거가 섞였을 때의 답 확신도
ESCALATE_BELOW = 0.6  # 어떤 답이라도 이보다 확신이 낮으면 운용자에게 넘긴다
NEW_TRACK_WINDOW_SEC = 8.0  # SKELETON 보고 주기와 같게 — 무상태 stub 이 "새 track" 을 보는 창


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    id: str
    instructions: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreQuestion:
    id: str
    instructions: str
    min_score: int
    max_score: int


@dataclass(frozen=True, slots=True)
class YesNoQuestion:
    id: str
    instructions: str


Question = ChoiceQuestion | ScoreQuestion | YesNoQuestion


@dataclass(frozen=True, slots=True)
class Answer:
    question_id: str
    value: str | int | bool
    probabilities: dict[str, float]  # 선택지 키("yes"/"no", "0".."3", 옵션 이름) → 확률
    confidence: float

    def __post_init__(self) -> None:
        total = sum(self.probabilities.values())
        if not math.isclose(total, 1.0, abs_tol=PROBABILITY_TOLERANCE):
            raise ValueError(f"answer '{self.question_id}' probabilities must sum to 1, got {total}")

        value_key = _get_probability_key(self.value)
        if value_key not in self.probabilities:
            raise ValueError(f"answer '{self.question_id}' value {value_key!r} is not a probability key")

        # 동률(예/아니오 0.5:0.5)은 허용한다 — value 확률이 최댓값이기만 하면 된다
        highest = max(self.probabilities.values())
        if self.probabilities[value_key] < highest - PROBABILITY_TOLERANCE:
            raise ValueError(f"answer '{self.question_id}' value {value_key!r} is not the argmax")

        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"answer '{self.question_id}' confidence must be in [0, 1], got {self.confidence}")

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "value": self.value,
            "probabilities": dict(self.probabilities),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    answers: tuple[Answer, ...]
    verdict: str  # "act"(규칙대로 진행) | "escalate"(운용자 판단 필요)
    reason: str

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"field 'verdict' must be one of {VERDICTS}, got {self.verdict!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "answers": [answer.to_dict() for answer in self.answers],
        }


@runtime_checkable
class DecisionModel(Protocol):
    def decide(self, state: StateJson, questions: Sequence[Question]) -> Decision: ...


DEFAULT_QUESTIONS: tuple[Question, ...] = (
    ScoreQuestion(
        id=QUESTION_ID_URGENCY,
        instructions="Rate how urgently the operator should look at this scene, from 0 (nothing) to 3 (now).",
        min_score=URGENCY_MIN,
        max_score=URGENCY_MAX,
    ),
    ChoiceQuestion(
        id=QUESTION_ID_NEXT_ACTION,
        instructions="Choose the next step for the overwatch drone.",
        options=NEXT_ACTIONS,
    ),
    YesNoQuestion(
        id=QUESTION_ID_REPORT_NEEDED,
        instructions="Should a new situation report be sent to the operator now?",
    ),
)


@dataclass(frozen=True, slots=True)
class _Assessment:
    """질문과 무관하게 상태에서 한 번만 계산하는 규칙 판정 결과."""

    urgency: int
    evidence_labels: tuple[str, ...]
    has_new_track: bool
    confidence: float
    ambiguity_causes: tuple[str, ...]


class RuleBasedDecisionModel:
    def decide(self, state: StateJson, questions: Sequence[Question] = DEFAULT_QUESTIONS) -> Decision:
        assessment = _assess_state(state)

        answers = tuple(self._answer_question(question, assessment) for question in questions)

        is_escalated = any(answer.confidence < ESCALATE_BELOW for answer in answers)
        verdict = "escalate" if is_escalated else "act"

        return Decision(answers=answers, verdict=verdict, reason=_describe_reason(assessment, verdict))

    def _answer_question(self, question: Question, assessment: _Assessment) -> Answer:
        handlers = {
            QUESTION_ID_URGENCY: self._answer_urgency,
            QUESTION_ID_NEXT_ACTION: self._answer_next_action,
            QUESTION_ID_REPORT_NEEDED: self._answer_report_needed,
        }
        handler = handlers.get(question.id)
        if handler is None:
            raise ValueError(f"question id {question.id!r} is not supported by RuleBasedDecisionModel")
        return handler(question, assessment)

    def _answer_urgency(self, question: Question, assessment: _Assessment) -> Answer:
        if not isinstance(question, ScoreQuestion):
            raise ValueError(f"question {question.id!r} must be a ScoreQuestion")
        if (question.min_score, question.max_score) != (URGENCY_MIN, URGENCY_MAX):
            raise ValueError(
                f"question {question.id!r} must range {URGENCY_MIN}..{URGENCY_MAX}, "
                f"got {question.min_score}..{question.max_score}"
            )

        keys = [str(score) for score in range(question.min_score, question.max_score + 1)]
        return _build_answer(question.id, assessment.urgency, keys, assessment.confidence)

    def _answer_next_action(self, question: Question, assessment: _Assessment) -> Answer:
        if not isinstance(question, ChoiceQuestion):
            raise ValueError(f"question {question.id!r} must be a ChoiceQuestion")

        next_action = NEXT_ACTIONS[min(assessment.urgency, len(NEXT_ACTIONS) - 1)]
        if next_action not in question.options:
            raise ValueError(f"question {question.id!r} options {question.options} lack {next_action!r}")

        return _build_answer(question.id, next_action, list(question.options), assessment.confidence)

    def _answer_report_needed(self, question: Question, assessment: _Assessment) -> Answer:
        if not isinstance(question, YesNoQuestion):
            raise ValueError(f"question {question.id!r} must be a YesNoQuestion")

        is_report_needed = assessment.urgency >= 1 or assessment.has_new_track
        return _build_answer(question.id, is_report_needed, ["yes", "no"], assessment.confidence)


# --- 모듈 내부 도우미 ---


def _assess_state(state: StateJson) -> _Assessment:
    active_tracks = [track for track in state.tracks if track.is_active]
    persons = [track for track in active_tracks if track.class_name == PERSON_CLASS]
    vehicles = [track for track in active_tracks if track.class_name in VEHICLE_CLASSES]

    nearest_distance_m = _get_nearest_person_vehicle_distance(persons, vehicles)
    is_person_near_vehicle = nearest_distance_m is not None and nearest_distance_m <= PERSON_NEAR_VEHICLE_M

    if not active_tracks:
        urgency, evidence_tracks = 0, []
    elif persons and (is_person_near_vehicle or len(persons) >= PERSON_GROUP_MIN):
        urgency, evidence_tracks = 3, persons + vehicles
    elif persons:
        urgency, evidence_tracks = 2, persons
    else:
        urgency, evidence_tracks = 1, active_tracks

    ambiguity_causes = _find_ambiguity_causes(evidence_tracks, nearest_distance_m)
    confidence = AMBIGUOUS_CONFIDENCE if ambiguity_causes else CLEAR_CONFIDENCE

    new_track_since_sec = state.time_sec - NEW_TRACK_WINDOW_SEC
    has_new_track = any(track.first_seen_sec >= new_track_since_sec for track in active_tracks)

    return _Assessment(
        urgency=urgency,
        evidence_labels=tuple(sorted({track.track_id for track in evidence_tracks}, key=_get_label_number)),
        has_new_track=has_new_track,
        confidence=confidence,
        ambiguity_causes=tuple(ambiguity_causes),
    )


def _get_nearest_person_vehicle_distance(
    persons: Sequence[TrackStateJson], vehicles: Sequence[TrackStateJson]
) -> float | None:
    distances = [
        math.hypot(person.north_m - vehicle.north_m, person.east_m - vehicle.east_m)
        for person in persons
        for vehicle in vehicles
    ]
    return min(distances) if distances else None


def _find_ambiguity_causes(
    evidence_tracks: Sequence[TrackStateJson], nearest_distance_m: float | None
) -> list[str]:
    causes = []
    for track in evidence_tracks:
        if track.mean_confidence < LOW_CONFIDENCE:
            causes.append(f"{track.track_id} mean_confidence {track.mean_confidence:.2f} < {LOW_CONFIDENCE:.2f}")
        if track.observation_count < MIN_OBSERVATIONS:
            causes.append(f"{track.track_id} observation_count {track.observation_count} < {MIN_OBSERVATIONS}")

    # 거리 경계 근처면 위치 오차 몇 m 로 urgency 2 와 3 이 뒤집힌다
    if nearest_distance_m is not None and abs(nearest_distance_m - PERSON_NEAR_VEHICLE_M) <= AMBIGUOUS_MARGIN_M:
        causes.append(
            f"person-vehicle distance {nearest_distance_m:.1f} m is within "
            f"{PERSON_NEAR_VEHICLE_M:.0f}+-{AMBIGUOUS_MARGIN_M:.0f} m"
        )

    return causes


def _build_answer(question_id: str, value: str | int | bool, keys: Sequence[str], confidence: float) -> Answer:
    probabilities = _build_probabilities(_get_probability_key(value), keys, confidence)
    return Answer(question_id=question_id, value=value, probabilities=probabilities, confidence=confidence)


def _build_probabilities(chosen_key: str, keys: Sequence[str], confidence: float) -> dict[str, float]:
    # argmax 에 confidence, 나머지 선택지에 남은 확률을 균등 분배한다 (세 질문 타입 공통)
    if len(keys) == 1:
        return {chosen_key: 1.0}

    remainder = (1.0 - confidence) / (len(keys) - 1)
    return {key: (confidence if key == chosen_key else remainder) for key in keys}


def _get_probability_key(value: str | int | bool) -> str:
    # bool 은 int 의 하위 타입이라 먼저 본다
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _get_label_number(label: str) -> int:
    return int(label[1:])


def _describe_reason(assessment: _Assessment, verdict: str) -> str:
    if assessment.evidence_labels:
        evidence = ", ".join(assessment.evidence_labels)
        reason = f"urgency {assessment.urgency} from {evidence}"
    else:
        reason = f"urgency {assessment.urgency}: no active tracks"

    if verdict == "escalate":
        reason += "; ambiguous: " + "; ".join(assessment.ambiguity_causes)

    return reason
