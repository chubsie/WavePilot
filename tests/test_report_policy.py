"""보고 권한 경계 정책 테스트 (AI-SPEC D4, E-03 = 차단이 아니라 flag).

판정·피아·교전·권한 밖 행동 어휘와 suggestion 의 행동 요구를 잡고, 일상 서술과 허용 예외는 통과시킨다.
"""

import pytest

from modules.llm_report.report_policy import find_policy_violations
from modules.llm_report.report_schema import ReportSentence, SituationReport


def _make_report(kind: str, text: str) -> SituationReport:
    return SituationReport(sentences=(ReportSentence(kind, text, ("T1",)),))


POSITIVE_CASES = [
    ("observation", "A hostile vehicle is parked.", "restricted term: hostile"),
    ("observation", "T2 may be a threat.", "restricted term: threat"),
    ("suggestion", "Engage the truck.", "restricted term: engage"),
    ("observation", "The person appears armed.", "restricted term: armed"),
    ("observation", "Track the target near T4.", "restricted term: target"),
    ("observation", "An enemy group is near the road.", "restricted term: enemy"),
    ("suggestion", "Return to home now.", "return to home"),
    ("suggestion", "Fly to the car for a closer look.", "suggests out-of-scope action: fly to"),
]

NEGATIVE_CASES = [
    ("observation", "Two cars are parked near the road."),
    ("observation", "One person is walking north at about 1 m/s."),
    ("observation", "Track T3 was last seen 30 s ago."),
    ("observation", "Hold target altitude of 40 m."),
    ("observation", "The truck turned east."),
    ("suggestion", "Operator may look closer at T3 to confirm by observation."),
    ("observation", "The bus has been stationary since 12.0 s."),
]


@pytest.mark.parametrize(("kind", "text", "expected_fragment"), POSITIVE_CASES)
def test_policy_flags_restricted_language(kind: str, text: str, expected_fragment: str) -> None:
    flags = find_policy_violations(_make_report(kind, text))

    assert flags
    assert any(expected_fragment in flag for flag in flags), flags
    assert all(flag.startswith("sentences[0] ") for flag in flags)


@pytest.mark.parametrize(("kind", "text"), NEGATIVE_CASES)
def test_policy_passes_neutral_language(kind: str, text: str) -> None:
    assert find_policy_violations(_make_report(kind, text)) == []


def test_policy_flag_format_and_sentence_index() -> None:
    report = SituationReport(
        sentences=(
            ReportSentence("observation", "Two cars are parked.", ("T1",)),
            ReportSentence("observation", "A hostile car, hostile driver.", ("T2",)),
            ReportSentence("suggestion", "Move toward T3 and alert the base.", ("T3",)),
        )
    )

    flags = find_policy_violations(report)

    assert flags == [
        "sentences[1] uses restricted term: hostile",
        "sentences[2] suggests out-of-scope action: move toward",
        "sentences[2] suggests out-of-scope action: alert",
    ]


def test_suggestion_action_patterns_ignore_observations() -> None:
    assert find_policy_violations(_make_report("observation", "The car moved to the gate.")) == []
