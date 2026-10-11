"""rule_mode 선택기 계약 테스트 (D-05, D-06, SEL-01, SEL-04).

선택 결과 SelectionResult·Latency 검증, DecisionModel Protocol, 패키지 재수출, interface/ 의 표준 라이브러리 전용 import 를 본다.
"""

import ast
import dataclasses
import math
import sys
from pathlib import Path

import pytest

import modules.decision.interface as decision_interface
from modules.decision import (
    RULE_MODE_DEFINITIONS,
    RULE_MODES,
    DecisionModel,
    SelectionEvent,
    SelectionResult,
    SelectionState,
    build_request_state,
)
from modules.decision.interface.selector import (
    FALLBACK_BAD_RESPONSE,
    FALLBACK_CIRCUIT_OPEN,
    FALLBACK_INTERNAL_ERROR,
    FALLBACK_LOW_CONFIDENCE,
    FALLBACK_NONE,
    FALLBACK_OUT_OF_MENU,
    FALLBACK_REASONS,
    FALLBACK_SINGLE_OPTION,
    FALLBACK_TIMEOUT,
    FALLBACK_UNAVAILABLE,
    SOURCE_D1,
    SOURCE_RULE,
    SOURCES,
    Latency,
)

INTERFACE_DIR = Path(__file__).resolve().parents[1] / "modules" / "decision" / "interface"
RESULT_KEYS = [
    "rule_mode", "params", "confidence", "probabilities", "source", "fallback_reason",
    "options", "latency", "input_tokens",
]


def _make_result(**overrides) -> SelectionResult:
    fields = {
        "rule_mode": "search",
        "params": {"target_track": None, "hold_sec": 5},
        "confidence": 1.0,
        "probabilities": {"search": 1.0, "hold": 0.0, "return_home": 0.0},
        "source": SOURCE_RULE,
        "fallback_reason": FALLBACK_NONE,
        "options": ("search", "hold", "return_home"),
        "latency": Latency(total_ms=0.4, orin_processing_ms=None, network_rtt_ms=None),
        "input_tokens": None,
    }
    fields.update(overrides)
    return SelectionResult(**fields)


def _make_d1_result() -> SelectionResult:
    return _make_result(
        probabilities={"search": 0.81, "hold": 0.12, "return_home": 0.07},
        confidence=0.715,
        source=SOURCE_D1,
        latency=Latency(total_ms=230.0, orin_processing_ms=210.0, network_rtt_ms=20.0),
        input_tokens=312,
    )


class _StubDecisionModel:
    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        return _make_result()


class _NoSelect:
    def decide(self, state, event):
        return None


# --- 메뉴 ---


def test_rule_modes_order_and_definitions() -> None:
    assert RULE_MODES == ("search", "approach", "aim_and_fire", "observe", "hold", "return_home")
    assert set(RULE_MODE_DEFINITIONS) == set(RULE_MODES)


# --- 상수 ---


def test_source_and_fallback_constants() -> None:
    assert SOURCES == (SOURCE_D1, SOURCE_RULE) == ("d1", "rule")
    assert FALLBACK_REASONS == (
        FALLBACK_NONE, FALLBACK_TIMEOUT, FALLBACK_OUT_OF_MENU, FALLBACK_LOW_CONFIDENCE,
        FALLBACK_UNAVAILABLE, FALLBACK_BAD_RESPONSE, FALLBACK_CIRCUIT_OPEN, FALLBACK_SINGLE_OPTION,
        FALLBACK_INTERNAL_ERROR,
    )
    assert FALLBACK_REASONS == (
        "none", "timeout", "out_of_menu", "low_confidence",
        "unavailable", "bad_response", "circuit_open", "single_option", "internal_error",
    )


# --- SelectionResult ---


def test_rule_result_to_dict_has_fixed_shape() -> None:
    data = _make_result().to_dict()

    assert list(data) == RESULT_KEYS
    assert data["options"] == ["search", "hold", "return_home"]
    assert data["latency"] == {"total_ms": 0.4, "orin_processing_ms": None, "network_rtt_ms": None}
    assert data["probabilities"] == {"search": 1.0, "hold": 0.0, "return_home": 0.0}


def test_d1_result_is_valid() -> None:
    result = _make_d1_result()

    assert result.source == "d1"
    assert result.fallback_reason == "none"
    assert result.input_tokens == 312
    assert result.to_dict()["latency"]["network_rtt_ms"] == 20.0


def test_rule_result_may_carry_fallback_reason() -> None:
    result = _make_result(fallback_reason=FALLBACK_TIMEOUT)

    assert result.fallback_reason == "timeout"


def test_result_allows_probability_tie() -> None:
    result = _make_result(probabilities={"search": 0.5, "hold": 0.5}, confidence=0.0)

    assert result.rule_mode == "search"


@pytest.mark.parametrize(
    "overrides",
    [
        {"rule_mode": "approach"},
        {"options": ()},
        {"options": ("search", "fly")},
        {"options": ("search", "search")},
        {"probabilities": {"search": 0.9, "approach": 0.1}},
        {"probabilities": {"search": 0.8, "hold": 0.1}},
        {"probabilities": {"search": 1.1, "hold": -0.1}},
        {"probabilities": {"search": 0.2, "hold": 0.8}},
        {"probabilities": {}},
        {"probabilities": {"hold": 1.0}},
        {"confidence": 1.2},
        {"confidence": -0.1},
        {"confidence": math.nan},
        {"source": "llm"},
        {"fallback_reason": "oops"},
        {"source": SOURCE_D1, "fallback_reason": FALLBACK_TIMEOUT},
        {"params": {"targets": ["T1"]}},
        {"params": {"flag": True}},
        {"params": {"value": math.inf}},
        {"input_tokens": -1},
        {"input_tokens": True},
    ],
)
def test_result_rejects_invalid(overrides) -> None:
    valid = _make_result()

    with pytest.raises(ValueError):
        dataclasses.replace(valid, **overrides)


def test_result_copies_params_and_probabilities() -> None:
    params = {"hold_sec": 5}
    probabilities = {"search": 1.0, "hold": 0.0}
    result = _make_result(params=params, probabilities=probabilities, options=("search", "hold"))

    params["hold_sec"] = 99
    probabilities["search"] = 0.0

    assert result.params == {"hold_sec": 5}
    assert result.probabilities == {"search": 1.0, "hold": 0.0}


def test_result_options_list_becomes_tuple() -> None:
    result = _make_result(options=["search", "hold", "return_home"])

    assert result.options == ("search", "hold", "return_home")


# --- Latency ---


def test_latency_allows_none_parts() -> None:
    latency = Latency(total_ms=1.0, orin_processing_ms=None, network_rtt_ms=None)

    assert latency.to_dict() == {"total_ms": 1.0, "orin_processing_ms": None, "network_rtt_ms": None}


@pytest.mark.parametrize(
    "fields",
    [
        {"total_ms": -1.0, "orin_processing_ms": None, "network_rtt_ms": None},
        {"total_ms": math.nan, "orin_processing_ms": None, "network_rtt_ms": None},
        {"total_ms": 10.0, "orin_processing_ms": -1.0, "network_rtt_ms": None},
        {"total_ms": 10.0, "orin_processing_ms": None, "network_rtt_ms": math.inf},
    ],
)
def test_latency_rejects_invalid(fields) -> None:
    with pytest.raises(ValueError):
        Latency(**fields)


# --- DecisionModel Protocol (SEL-04) ---


def test_stub_with_select_is_decision_model() -> None:
    stub = _StubDecisionModel()

    assert isinstance(stub, DecisionModel)
    assert stub.select(None, None).rule_mode == "search"


def test_object_without_select_is_not_decision_model() -> None:
    assert not isinstance(_NoSelect(), DecisionModel)


# --- 패키지 재수출 ---


def test_package_reexports_interface() -> None:
    assert callable(build_request_state)
    assert decision_interface.__all__ == sorted(decision_interface.__all__)

    missing = [name for name in decision_interface.__all__ if not hasattr(decision_interface, name)]

    assert not missing

    import modules.decision as decision_package

    assert decision_package.__all__ == decision_interface.__all__


# --- import 경계: interface/ 는 표준 라이브러리와 자기 자신만 ---


def _get_absolute_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def test_interface_imports_only_stdlib_and_itself() -> None:
    paths = sorted(INTERFACE_DIR.glob("*.py"))
    violations: list[str] = []

    for path in paths:
        for imported in _get_absolute_imports(path):
            is_stdlib = imported.split(".")[0] in sys.stdlib_module_names
            if not is_stdlib and not imported.startswith("modules.decision.interface"):
                violations.append(f"{path.name} imports {imported}")

    assert len(paths) >= 4
    assert not violations, violations
