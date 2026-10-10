"""YOLO 선택 의존성 격리 검사 (SC3, DET-01).

무거운 선택 의존성(yolo 그룹)은 base 환경 밖에 있어야 한다.
그래서 ultralytics/torch/cv2/numpy import 는 어댑터(modules/yolo_detection/)와 CLI(modules/yolo_detection/run_yolo.py)에만 두고,
그 안에서도 모듈 최상단이 아니라 함수 안에서만 한다.
"""

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

HEAVY_MODULES = frozenset({"ultralytics", "torch", "cv2", "numpy"})
ALLOWED_PREFIXES = ("modules/yolo_detection/",)
ALLOWED_FILES = frozenset({"modules/yolo_detection/run_yolo.py"})
EXCLUDED_DIRS = frozenset({".venv", "tests", "graft", "__pycache__"})

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]


def _get_production_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not EXCLUDED_DIRS.intersection(path.relative_to(root).parts)
    )


def _is_allowed(relative_path: str) -> bool:
    return relative_path in ALLOWED_FILES or relative_path.startswith(ALLOWED_PREFIXES)


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


def _is_heavy(module_name: str) -> bool:
    return module_name.split(".")[0] in HEAVY_MODULES


def test_heavy_imports_stay_in_yolo_adapter() -> None:
    violations: list[str] = []
    for path in _get_production_files(WAVEPILOT_ROOT):
        relative_path = path.relative_to(WAVEPILOT_ROOT).as_posix()
        if _is_allowed(relative_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line_no, module_name in _get_imports(tree):
            if _is_heavy(module_name):
                violations.append(f"{relative_path}:{line_no} imports {module_name} outside the YOLO adapter")

    assert not violations, "\n".join(violations)


def test_heavy_imports_are_not_module_level() -> None:
    violations: list[str] = []
    for path in _get_production_files(WAVEPILOT_ROOT):
        relative_path = path.relative_to(WAVEPILOT_ROOT).as_posix()
        if not _is_allowed(relative_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line_no, module_name in _get_module_level_imports(tree):
            if _is_heavy(module_name):
                violations.append(f"{relative_path}:{line_no} imports {module_name} at module level")

    assert not violations, "\n".join(violations)


def test_yolo_detection_imports_without_heavy_deps() -> None:
    blocker = "; ".join(f"sys.modules[{name!r}] = None" for name in sorted(HEAVY_MODULES))
    code = f"import sys; {blocker}; import modules.yolo_detection, modules.yolo_detection.convert, modules.yolo_detection.source"

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=WAVEPILOT_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_pyproject_isolates_yolo_dependencies() -> None:
    pyproject = tomllib.loads((WAVEPILOT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base_dependencies = pyproject["project"]["dependencies"]
    dev_group = pyproject["dependency-groups"]["dev"]
    yolo_group = pyproject["dependency-groups"]["yolo"]
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]

    assert base_dependencies == []
    assert not [dep for dep in base_dependencies + dev_group if dep.startswith(("ultralytics", "lap"))]
    assert any(dep.startswith("ultralytics") for dep in yolo_group)
    assert any(dep.startswith("lap") for dep in yolo_group)
    assert any(marker.startswith("yolo:") for marker in pytest_options["markers"])
    assert "not yolo" in pytest_options["addopts"]
