"""모듈 간 import 경계 검사 (SC4, SC7, IF-06, D-03, D-04).

각 모듈은 `modules/<name>/interface/` 에 계약을 두고, 다른 모듈은 그 interface 만 import 한다.
구현을 골라 조립하는 컴포지션 루트(drone/, operator_console/__main__.py)만 예외다.
AST 로 모든 import 를 모아 규칙과 대조한다.
"""

import ast
from pathlib import Path

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
MODULES_DIR = WAVEPILOT_ROOT / "modules"
MODULE_PREFIX = "modules."

LLM_FACADE = "modules.llm_report"

# 구현을 골라 끼우는 진입점 — 다른 모듈의 구현을 import 해도 된다
COMPOSITION_ROOTS = frozenset({
    "modules/drone/run_scenario.py",
    "modules/operator_console/__main__.py",
})

# 파이프라인은 interface 와 LLM 최상위 파사드만 알아야 mock·실기 구현을 바꿔 끼울 수 있다
INTERFACE_ONLY_FILES = frozenset({"modules/drone/pipeline.py"})


def _get_python_files() -> list[Path]:
    return sorted(MODULES_DIR.rglob("*.py"))


def _get_imports(path: Path) -> list[tuple[int, str]]:
    """(줄 번호, 전체 dotted 모듈 이름) 목록. 상대 import 는 같은 패키지 안이라 제외한다."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module))
    return imports


def _get_module_name(dotted: str) -> str | None:
    """`modules.<name>...` 이면 <name>, 아니면 None."""
    if not dotted.startswith(MODULE_PREFIX):
        return None
    return dotted.removeprefix(MODULE_PREFIX).split(".")[0]


def _is_interface_import(dotted: str) -> bool:
    parts = dotted.split(".")
    return len(parts) >= 3 and parts[2] == "interface"


def _find_violation(relative_path: str, imported: str) -> str | None:
    owner = relative_path.split("/")[1]
    target = _get_module_name(imported)
    is_interface_file = "/interface/" in relative_path

    if target is None or imported == "modules":
        return None
    if is_interface_file and not _is_interface_import(imported):
        return "interface/ may import only other interface/ packages"
    if target == owner:
        return None
    if _is_interface_import(imported):
        return None
    if relative_path in INTERFACE_ONLY_FILES:
        return None if imported == LLM_FACADE else "pipeline may import only interface/ and the llm_report facade"
    if relative_path in COMPOSITION_ROOTS or owner == "drone":
        return None if not imported.startswith(f"{LLM_FACADE}.") else f"use the top-level '{LLM_FACADE}' facade"
    return f"other modules may import only modules.{target}.interface"


def test_modules_respect_import_boundaries() -> None:
    violations: list[str] = []

    for path in _get_python_files():
        relative_path = path.relative_to(WAVEPILOT_ROOT).as_posix()
        for line_no, imported in _get_imports(path):
            reason = _find_violation(relative_path, imported)
            if reason is not None:
                violations.append(f"{relative_path}:{line_no} imports '{imported}' — {reason}")

    assert not violations, "\n".join(violations)


def test_every_module_with_contracts_has_interface_package() -> None:
    contract_modules = ("llm_report", "yolo_detection", "perception", "flight_control", "comm")

    missing = [name for name in contract_modules if not (MODULES_DIR / name / "interface" / "__init__.py").is_file()]

    assert not missing, f"modules without interface/ package: {missing}"


def test_operator_package_does_not_shadow_stdlib() -> None:
    # operator/ 는 표준 라이브러리 operator 를 가려 dataclasses 등이 깨진다 (D-04 편차)
    assert not (MODULES_DIR / "operator").exists(), "use modules/operator_console/, not operator/"
