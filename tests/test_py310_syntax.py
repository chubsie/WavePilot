"""Jetson 용 파일의 Python 3.10 문법 호환 검사 (DET-01, DET-02).

Jetson JetPack 6 의 Ultralytics 컨테이너는 Python 3.10 이고 이 리포는 3.12 를 목표로 한다.
컨테이너에는 이 파일들을 PYTHONPATH 로 마운트해 쓰므로 문법이 3.10 에서 읽혀야 한다.
"""

import ast
from pathlib import Path

import pytest

JETSON_PYTHON_VERSION = (3, 10)
PY311_ONLY_MODULES = frozenset({"tomllib"})  # 3.11+ 표준 라이브러리 — Jetson 컨테이너에 없다

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]


def _get_jetson_files() -> list[Path]:
    candidates = [
        WAVEPILOT_ROOT / "modules" / "__init__.py",
        *sorted((WAVEPILOT_ROOT / "modules" / "yolo_detection").glob("**/*.py")),
    ]
    return [path for path in candidates if path.exists()]


def _get_relative_id(path: Path) -> str:
    return path.relative_to(WAVEPILOT_ROOT).as_posix()


def _get_imported_top_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


JETSON_FILES = _get_jetson_files()


@pytest.mark.parametrize("path", JETSON_FILES, ids=_get_relative_id)
def test_jetson_files_parse_as_python310(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=JETSON_PYTHON_VERSION)


@pytest.mark.parametrize("path", JETSON_FILES, ids=_get_relative_id)
def test_jetson_files_avoid_py311_stdlib(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert not _get_imported_top_modules(tree) & PY311_ONLY_MODULES


def test_jetson_file_list_covers_adapter() -> None:
    relative_paths = {_get_relative_id(path) for path in JETSON_FILES}

    assert {"modules/yolo_detection/interface/detection.py", "modules/yolo_detection/source.py"} <= relative_paths
