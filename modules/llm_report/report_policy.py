"""보고 문장의 권한 경계 검사 (AI-SPEC D4, 가드레일 G5).

LLM 은 판정·교전·비행 지시 권한이 없다. 보고 문장이 적대성 판정, 피아 식별, 표적·교전,
권한 밖 행동(복귀·비상 등)을 말하거나, suggestion 이 "더 가까이 보기 / 관측으로 확인" 밖의
행동을 요구하면 flag 를 남긴다. Phase 1 은 표시만 하고 문장을 지우지 않는다(E-03).
문장 보류·제거 정책은 RPT-03 과 함께 Phase 2 에서 정한다.

어휘 목록은 문헌 기반 초안이다. SALUTE 보고 서식을 아는 정찰 경험자 보정(AI-SPEC 1b) 전이므로
오탐·누락이 나오면 회귀 테스트와 함께 이 목록을 갱신한다.
"""

import re

from modules.llm_report.report_schema import SituationReport

# 범주 이름 → 패턴. 모두 단어 경계 + 대소문자 무시로 컴파일한다 (AI-SPEC 금지 어휘 초기 목록 표)
FORBIDDEN_PATTERNS: dict[str, tuple[str, ...]] = {
    "hostility_judgement": (
        r"hostile", r"hostility", r"enemy", r"adversar\w*", r"combatant\w*", r"threat\w*",
        r"dangerous", r"suspicious",
    ),
    "friend_or_foe": (r"friend(?:ly)?", r"foe", r"IFF", r"ally", r"allied"),
    "targeting_engagement": (
        r"target(?:s|ed|ing)?", r"engag\w*", r"neutraliz\w*", r"strik(?:e|es|ing)", r"attack\w*",
        r"open fire", r"fire on", r"kill\w*", r"destroy\w*", r"eliminat\w*", r"weapon\w*", r"armed",
    ),
    "out_of_scope_action": (r"return to (?:home|base)", r"abort", r"emergency", r"evacuat\w*"),
}

# 텔레메트리 용어에서 온 표현은 금지 어휘가 아니다. 매칭 전에 문장에서 지운다
ALLOWED_PHRASES = ("target altitude",)

# suggestion 은 "look closer / observe / confirm by observation" 만 허용한다. 그 밖의 행동 요구를 잡는다
SUGGESTION_ACTION_PATTERNS: tuple[str, ...] = (
    r"(?:fly|move|go|reposition|relocate)\s+(?:to|toward)",
    r"alert|alarm",
    r"land",
)

_FORBIDDEN_REGEXES = tuple(
    re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)
    for patterns in FORBIDDEN_PATTERNS.values()
    for pattern in patterns
)
_SUGGESTION_ACTION_REGEXES = tuple(
    re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE) for pattern in SUGGESTION_ACTION_PATTERNS
)
_ALLOWED_PHRASE_REGEXES = tuple(
    re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE) for phrase in ALLOWED_PHRASES
)


def find_policy_violations(report: SituationReport) -> list[str]:
    """문장 순서대로 flag 문자열을 돌려준다. 같은 문장에서 같은 term 은 한 번만 적는다."""
    flags = []
    for index, sentence in enumerate(report.sentences):
        text = _remove_allowed_phrases(sentence.text)

        for term in _find_terms(text, _FORBIDDEN_REGEXES):
            flags.append(f"sentences[{index}] uses restricted term: {term}")

        if sentence.kind == "suggestion":
            for term in _find_terms(text, _SUGGESTION_ACTION_REGEXES):
                flags.append(f"sentences[{index}] suggests out-of-scope action: {term}")

    return flags


def _remove_allowed_phrases(text: str) -> str:
    for regex in _ALLOWED_PHRASE_REGEXES:
        text = regex.sub(" ", text)
    return text


def _find_terms(text: str, regexes: tuple[re.Pattern, ...]) -> list[str]:
    # 문장 안 등장 순서로 정렬하고, 공백을 하나로 접은 소문자 term 으로 중복을 없앤다
    matches = sorted(
        (match.start(), " ".join(match.group(0).lower().split()))
        for regex in regexes
        for match in regex.finditer(text)
    )

    terms: list[str] = []
    for _, term in matches:
        if term not in terms:
            terms.append(term)
    return terms
