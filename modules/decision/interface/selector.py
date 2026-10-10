"""선택 결과와 선택기 Protocol (D-05, D-06).

선택기 1회 결과 하나로 로그(SEL-02)와 Phase 6 비교표를 만든다(D-05).
호출 쪽은 DecisionModel.select 만 알고 뒤에 규칙·d1·대체 조합 중 무엇이 있는지 모른다(SEL-04).
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from modules.decision.interface.menu import RULE_MODES
from modules.decision.interface.state import SelectionEvent, SelectionState

SOURCE_D1 = "d1"
SOURCE_RULE = "rule"
SOURCES = (SOURCE_D1, SOURCE_RULE)

FALLBACK_NONE = "none"
FALLBACK_TIMEOUT = "timeout"
FALLBACK_OUT_OF_MENU = "out_of_menu"
FALLBACK_LOW_CONFIDENCE = "low_confidence"
FALLBACK_UNAVAILABLE = "unavailable"
FALLBACK_BAD_RESPONSE = "bad_response"
FALLBACK_CIRCUIT_OPEN = "circuit_open"
FALLBACK_SINGLE_OPTION = "single_option"
FALLBACK_INTERNAL_ERROR = "internal_error"  # 선택기 코드의 예상 밖 예외 — 서버 오작동(bad_response)과 구분한다
FALLBACK_REASONS = (
    FALLBACK_NONE,
    FALLBACK_TIMEOUT,
    FALLBACK_OUT_OF_MENU,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_UNAVAILABLE,
    FALLBACK_BAD_RESPONSE,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_SINGLE_OPTION,
    FALLBACK_INTERNAL_ERROR,
)

# 서버 확률은 반올림하지 않으므로 이 허용오차면 통과한다 (RESEARCH §5 Answer 판정)
PROBABILITY_SUM_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class Latency:
    total_ms: float  # select() 1회 전체 벽시계 — d1 은 필터·직렬화·/health probe·POST 포함
    orin_processing_ms: float | None  # 서버가 timings 를 주지 않아 POST 왕복 − /health RTT 로 추정. 규칙 선택기는 None
    network_rtt_ms: float | None  # /health 왕복 시간. 규칙 선택기는 None

    def __post_init__(self) -> None:
        _validate_duration(self.total_ms, "latency.total_ms")
        if self.orin_processing_ms is not None:
            _validate_duration(self.orin_processing_ms, "latency.orin_processing_ms")
        if self.network_rtt_ms is not None:
            _validate_duration(self.network_rtt_ms, "latency.network_rtt_ms")

    def to_dict(self) -> dict:
        return {
            "total_ms": self.total_ms,
            "orin_processing_ms": self.orin_processing_ms,
            "network_rtt_ms": self.network_rtt_ms,
        }


@dataclass(frozen=True, slots=True)
class SelectionResult:
    rule_mode: str
    params: dict[str, str | int | float | None]  # 규칙 코드가 채운 파라미터(D-03, D-04)
    confidence: float
    probabilities: dict[str, float]  # 보기 → 확률
    source: str  # 실제로 고른 선택기: SOURCES 중 하나
    fallback_reason: str  # FALLBACK_REASONS 중 하나. d1 이 골랐으면 "none"
    options: tuple[str, ...]  # 제시한 보기 목록(보기 필터 결과)
    latency: Latency
    input_tokens: int | None  # d1 서버 usage.input_tokens. 규칙 선택기는 None

    def __post_init__(self) -> None:
        options = _validate_options(self.options)
        if self.rule_mode not in options:
            raise ValueError(f"field 'rule_mode' {self.rule_mode!r} must be one of options {options}")

        _validate_params(self.params)
        _validate_probabilities(self.probabilities, self.rule_mode, options)
        _validate_confidence(self.confidence)
        _validate_source(self.source, self.fallback_reason)
        _validate_input_tokens(self.input_tokens)

        # 넘긴 dict 를 바꿔도 결과가 바뀌지 않게 복사본으로 고정한다
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "probabilities", dict(self.probabilities))

    def to_dict(self) -> dict:
        return {
            "rule_mode": self.rule_mode,
            "params": dict(self.params),
            "confidence": self.confidence,
            "probabilities": dict(self.probabilities),
            "source": self.source,
            "fallback_reason": self.fallback_reason,
            "options": list(self.options),
            "latency": self.latency.to_dict(),
            "input_tokens": self.input_tokens,
        }


# D-06: modules.llm_report.decision.DecisionModel(v1.0 보고 경로, decide/verdict)과 이름이 같다.
# import 경로로만 구분된다. 보고 코드 정리(Deferred) 전까지 둘 다 유지한다.
# 이 Protocol 은 rule_mode 선택만 하며 보고용 개념(verdict, urgency, report_needed)을 담지 않는다(D-07).
@runtime_checkable
class DecisionModel(Protocol):
    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult: ...


# --- 모듈 내부 도우미 ---


def _validate_options(options: Sequence[str]) -> tuple[str, ...]:
    option_tuple = tuple(options)

    if not option_tuple:
        raise ValueError("field 'options' must not be empty")
    unknown_options = [option for option in option_tuple if option not in RULE_MODES]
    if unknown_options:
        raise ValueError(f"field 'options' has names outside RULE_MODES {unknown_options}")
    if len(set(option_tuple)) != len(option_tuple):
        raise ValueError(f"field 'options' has duplicates {list(option_tuple)}")

    return option_tuple


def _validate_params(params: Mapping[str, object]) -> None:
    if not isinstance(params, Mapping):
        raise ValueError(f"field 'params' must be a dict, got {type(params).__name__}")

    for key, value in params.items():
        if not isinstance(key, str):
            raise ValueError(f"field 'params' keys must be str, got {key!r}")
        # bool 은 int 의 하위 타입이라 따로 막는다
        if isinstance(value, bool) or not (value is None or isinstance(value, (str, int, float))):
            raise ValueError(f"field 'params.{key}' must be str, int, float or None, got {value!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"field 'params.{key}' must be finite, got {value!r}")


def _validate_probabilities(probabilities: Mapping[str, float], rule_mode: str, options: tuple[str, ...]) -> None:
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise ValueError("field 'probabilities' must be a non-empty dict")

    unknown_keys = [key for key in probabilities if key not in options]
    if unknown_keys:
        raise ValueError(f"field 'probabilities' has keys outside options {unknown_keys}")
    if rule_mode not in probabilities:
        raise ValueError(f"field 'probabilities' must include rule_mode {rule_mode!r}")

    for key, value in probabilities.items():
        if not _is_finite_number(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"field 'probabilities.{key}' must be in [0, 1], got {value!r}")

    total = sum(probabilities.values())
    if not math.isclose(total, 1.0, abs_tol=PROBABILITY_SUM_TOLERANCE):
        raise ValueError(f"field 'probabilities' must sum to 1, got {total}")

    # 동률은 허용한다 — rule_mode 확률이 최댓값이기만 하면 된다
    highest = max(probabilities.values())
    if probabilities[rule_mode] < highest - PROBABILITY_SUM_TOLERANCE:
        raise ValueError(f"field 'rule_mode' {rule_mode!r} is not the argmax of probabilities")


def _validate_confidence(confidence: float) -> None:
    if not _is_finite_number(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"field 'confidence' must be in [0, 1], got {confidence!r}")


def _validate_source(source: str, fallback_reason: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"field 'source' must be one of {SOURCES}, got {source!r}")
    if fallback_reason not in FALLBACK_REASONS:
        raise ValueError(f"field 'fallback_reason' must be one of {FALLBACK_REASONS}, got {fallback_reason!r}")

    # d1 이 실제로 골랐다면 대체가 없었다는 뜻이다
    if source == SOURCE_D1 and fallback_reason != FALLBACK_NONE:
        raise ValueError(f"source 'd1' requires fallback_reason 'none', got {fallback_reason!r}")


def _validate_input_tokens(input_tokens: int | None) -> None:
    if input_tokens is None:
        return

    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        raise ValueError(f"field 'input_tokens' must be None or int >= 0, got {input_tokens!r}")


def _validate_duration(value: float, name: str) -> None:
    if not _is_finite_number(value) or value < 0:
        raise ValueError(f"field '{name}' must be a finite number >= 0, got {value!r}")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
