"""playwright 격리 검사 (CMP-01, D-02).

playwright 는 uv 그룹 compare 에만 있다. 그래서 import 는 modules/drone/run_compare.py 의 함수 안에서만 하고,
모듈 import·--help·base 환경 테스트는 playwright 없이 돌아야 한다. 보행 방식은 test_yolo_isolation.py 와 같다.
"""

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

PLAYWRIGHT_MODULE = "playwright"
COMPARE_ENTRY_FILE = "modules/drone/run_compare.py"
EXCLUDED_DIRS = frozenset({".venv", "tests", "graft", "__pycache__"})

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]


def _get_production_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not EXCLUDED_DIRS.intersection(path.relative_to(root).parts)
    )


def _get_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """(줄 번호, 전체 dotted 모듈 이름) 목록. 상대 import 는 제외한다."""
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module))
    return imports


def _get_module_level_imports(node: ast.AST) -> list[tuple[int, str]]:
    """함수 본문 안을 제외한 import. 모듈 최상단의 try/if 안 import 도 포함한다."""
    imports: list[tuple[int, str]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, ast.Import):
            imports.extend((child.lineno, alias.name) for alias in child.names)
        elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
            imports.append((child.lineno, child.module))
        else:
            imports.extend(_get_module_level_imports(child))
    return imports


def _is_playwright(module_name: str) -> bool:
    return module_name.split(".")[0] == PLAYWRIGHT_MODULE


def _parse_compare_entry() -> ast.AST:
    path = WAVEPILOT_ROOT / COMPARE_ENTRY_FILE
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_compare_entry_has_no_module_level_playwright_import() -> None:
    module_imports = _get_module_level_imports(_parse_compare_entry())

    violations = [f"line {line_no}: {name}" for line_no, name in module_imports if _is_playwright(name)]

    assert not violations, f"{COMPARE_ENTRY_FILE} imports playwright at module level: {violations}"


def test_compare_entry_imports_playwright_lazily() -> None:
    all_imports = _get_imports(_parse_compare_entry())

    assert any(name == "playwright.sync_api" for _, name in all_imports), "no lazy playwright.sync_api import found"


def test_playwright_used_only_by_compare_entry() -> None:
    violations: list[str] = []
    for path in _get_production_files(WAVEPILOT_ROOT):
        relative_path = path.relative_to(WAVEPILOT_ROOT).as_posix()
        if relative_path == COMPARE_ENTRY_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{relative_path}:{line_no} imports {name}" for line_no, name in _get_imports(tree) if _is_playwright(name)
        )

    assert not violations, "\n".join(violations)


def test_compare_entry_imports_without_playwright() -> None:
    code = "import sys, modules.drone.run_compare; assert 'playwright' not in sys.modules"

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=WAVEPILOT_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_pyproject_isolates_playwright() -> None:
    pyproject = tomllib.loads((WAVEPILOT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base_dependencies = pyproject["project"]["dependencies"]
    dev_group = pyproject["dependency-groups"]["dev"]
    compare_group = pyproject["dependency-groups"]["compare"]

    assert "playwright>=1.47" in compare_group
    assert not [dep for dep in base_dependencies + dev_group if dep.startswith(PLAYWRIGHT_MODULE)]
