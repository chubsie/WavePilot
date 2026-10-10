"""상황 보고 출력 타입, 서버로 보낼 JSON Schema, 응답 검증 (RPT-02).

스키마는 문법 강제(1차 방어), parse_report 는 사후 검증(2차 방어)이다.
llama.cpp 는 지원하지 않는 스키마 기능을 조용히 건너뛰므로 둘 다 필요하다(AI-SPEC §3 Pitfall 1).
그래서 스키마는 평탄하게 쓰고 $ref·uniqueItems·anyOf·number minimum 같은 키워드를 쓰지 않는다.

구조 위반(JSON 아님, 필드 누락·타입 오류, 문장 수·길이 초과)은 ReportFormatError 로 올리고,
근거 규칙 위반(근거 없는 문장, 입력에 없는 ID)은 flag 로 남긴다 — 보고는 살려서 "근거 미확인" 으로 보인다.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass

REPORT_KINDS = ("observation", "suggestion")
MAX_SENTENCES = 6  # 운용자가 한눈에 읽는 분량, max_tokens 640 산정의 기준
MAX_SENTENCE_CHARS = 240  # 콘솔 한 줄 + 문장 하나에 사실 하나
MAX_CITED_TRACKS = 8  # 한 문장이 너무 많은 track 을 뭉뚱그리지 않게


class ReportFormatError(ValueError):
    """응답이 구조 자체를 어겼다 (JSON 아님, 필드 누락, 타입 오류, 잘림). 호출자에게 올린다."""


@dataclass(frozen=True, slots=True)
class ReportSentence:
    kind: str  # "observation"(본 것) | "suggestion"(운용자에게 하는 제안)
    text: str
    track_ids: tuple[str, ...]  # 이 문장의 근거 track 라벨

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "track_ids": list(self.track_ids)}


@dataclass(frozen=True, slots=True)
class SituationReport:
    sentences: tuple[ReportSentence, ...]
    flags: tuple[str, ...] = ()  # 근거·정책 위반 기록. 비어 있으면 grounded

    @property
    def is_grounded(self) -> bool:
        return not self.flags

    def to_dict(self) -> dict:
        return {
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "flags": list(self.flags),
            "is_grounded": self.is_grounded,
        }


def build_report_schema(track_ids: Iterable[str]) -> dict:
    # $ref 는 중첩 시 변환기가 깨지므로 쓰지 않고 평탄하게 인라인한다
    return {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SENTENCES,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(REPORT_KINDS)},
                        "text": {"type": "string", "minLength": 1, "maxLength": MAX_SENTENCE_CHARS},
                        "track_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_CITED_TRACKS,
                            # 입력에 있는 ID 만 생성 가능하게 문법 단계에서 막는다
                            "items": {"type": "string", "enum": sorted(track_ids)},
                        },
                    },
                    "required": ["kind", "text", "track_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["sentences"],
        "additionalProperties": False,
    }


def parse_report(raw_text: str, valid_track_ids: Iterable[str]) -> SituationReport:
    valid_ids = set(valid_track_ids)

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ReportFormatError(f"response is not valid JSON: {error}") from error

    if not isinstance(payload, dict) or not isinstance(payload.get("sentences"), list):
        raise ReportFormatError("top-level object must have a 'sentences' list")
    raw_sentences = payload["sentences"]
    if not raw_sentences:
        raise ReportFormatError("'sentences' is empty")
    if len(raw_sentences) > MAX_SENTENCES:
        raise ReportFormatError(f"'sentences' has {len(raw_sentences)} items, max {MAX_SENTENCES}")

    sentences = []
    flags = []
    for index, raw_sentence in enumerate(raw_sentences):
        sentence = _parse_sentence(raw_sentence, index)
        sentences.append(sentence)
        flags.extend(_check_grounding(sentence, index, valid_ids))

    return SituationReport(sentences=tuple(sentences), flags=tuple(flags))


def _parse_sentence(raw_sentence: object, index: int) -> ReportSentence:
    if not isinstance(raw_sentence, dict):
        raise ReportFormatError(f"sentences[{index}] must be an object")

    kind = raw_sentence.get("kind")
    text = raw_sentence.get("text")
    track_ids = raw_sentence.get("track_ids")

    if kind not in REPORT_KINDS:
        raise ReportFormatError(f"sentences[{index}].kind must be one of {REPORT_KINDS}")
    if not isinstance(text, str) or not text.strip():
        raise ReportFormatError(f"sentences[{index}].text must be a non-empty string")
    if len(text.strip()) > MAX_SENTENCE_CHARS:
        raise ReportFormatError(f"sentences[{index}].text exceeds {MAX_SENTENCE_CHARS} characters")
    if not isinstance(track_ids, list) or not all(isinstance(item, str) for item in track_ids):
        raise ReportFormatError(f"sentences[{index}].track_ids must be a list of strings")

    return ReportSentence(kind=kind, text=text.strip(), track_ids=tuple(track_ids))


def _check_grounding(sentence: ReportSentence, index: int, valid_track_ids: set[str]) -> list[str]:
    # 구조 오류는 예외, 근거 규칙 위반은 flag. 보고 자체는 살려서 운용자에게 "근거 미확인" 으로 보인다
    flags = []
    if not sentence.track_ids:
        flags.append(f"sentences[{index}] cites no track")

    unknown_ids = sorted(set(sentence.track_ids) - valid_track_ids)
    if unknown_ids:
        flags.append(f"sentences[{index}] cites unknown tracks: {unknown_ids}")

    return flags
