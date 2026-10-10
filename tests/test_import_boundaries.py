"""패키지 간 import 경계 검사 (SC4, SC7, IF-06, D-03, D-04).

인터페이스 우선 구조는 "누가 누구를 import 하지 않는가"로만 지켜진다.
AST 로 모든 import 를 모아 금지 목록과 대조한다. 아직 없는 패키지는 건너뛴다 —
지금 통과하고, 패키지가 생기면 그때부터 의미 있는 검사가 된다.
"""

import ast
from pathlib import Path

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]

CHECKED_PACKAGES = (
    "interfaces", "mocks", "comm", "perception", "drone", "operator_console", "llm_module",
    "yolo_detection",
)

# 패키지(최상위 이름) -> import 하면 안 되는 최상위 모듈
FORBIDDEN_BY_PACKAGE = {
    "interfaces": {"mocks", "comm", "perception", "drone", "operator_console", "llm_module", "yolo_detection"},
    "mocks": {"comm", "perception", "llm_module", "drone", "operator_console"},
    "perception": {"mocks", "comm", "llm_module", "drone", "operator_console", "yolo_detection"},
    "comm": {"mocks", "perception", "llm_module", "drone", "operator_console"},
    "llm_module": {"mocks", "comm", "perception", "drone", "operator_console", "yolo_detection"},
    "operator_console": {"mocks", "perception", "llm_module", "drone"},
    "yolo_detection": {"mocks", "comm", "perception", "llm_module", "drone", "operator_console"},
}

# 파일 단위 규칙: 파이프라인은 구현체(mock·전송·인식 구현)를 몰라야 교체 지점이 유지된다
FORBIDDEN_BY_FILE = {
    "drone/pipeline.py": {"mocks", "comm", "perception"},
}

LLM_FACADE = "llm_module"


def _get_python_files() -> list[Path]:
    python_files: list[Path] = []
    for package in CHECKED_PACKAGES:
        package_dir = WAVEPILOT_ROOT / package
        if package_dir.is_dir():
            python_files.extend(sorted(package_dir.rglob("*.py")))
    return python_files


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


def _find_violations(path: Path) -> list[str]:
    relative_path = path.relative_to(WAVEPILOT_ROOT).as_posix()
    package = relative_path.split("/")[0]
    forbidden = FORBIDDEN_BY_PACKAGE.get(package, set()) | FORBIDDEN_BY_FILE.get(relative_path, set())

    violations: list[str] = []
    for line_no, module in _get_imports(path):
        top_level = module.split(".")[0]
        if top_level in forbidden:
            violations.append(f"{relative_path}:{line_no} imports forbidden module '{module}'")
        is_llm_internal = module.startswith(f"{LLM_FACADE}.")
        if is_llm_internal and package != LLM_FACADE:
            violations.append(
                f"{relative_path}:{line_no} imports '{module}'; only the top-level "
                f"'{LLM_FACADE}' facade may be imported outside llm_module/"
            )
    return violations


def test_packages_respect_import_boundaries() -> None:
    violations = [message for path in _get_python_files() for message in _find_violations(path)]

    assert not violations, "\n".join(violations)


def test_operator_package_does_not_shadow_stdlib() -> None:
    # operator/ 는 표준 라이브러리 operator 를 가려 dataclasses 등이 깨진다 (D-04 편차)
    assert not (WAVEPILOT_ROOT / "operator").exists(), "use operator_console/, not operator/"
