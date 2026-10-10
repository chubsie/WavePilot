"""--yolo local|orin 스위치 (D-05, SC2) — create_yolo_factory 와 두 컴포지션 루트의 플래그."""

import ast
import functools
import subprocess
import sys
from pathlib import Path

import pytest

from modules.drone import run_scenario, sim_demo
from modules.drone.yolo_factory import DEFAULT_YOLO_URL, create_yolo_factory
from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source
from modules.yolo_detection.remote.client import create_remote_yolo_source

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
YOLO_FACTORY_PATH = WAVEPILOT_ROOT / "modules" / "drone" / "yolo_factory.py"
LAZY_ONLY_MODULES = frozenset({"modules.yolo_detection.jpeg_source", "modules.yolo_detection.remote.client"})
HEAVY_MODULES = ("cv2", "numpy", "ultralytics", "torch")
SUBPROCESS_TIMEOUT_SEC = 60


# --- create_yolo_factory ---


def test_local_returns_jpeg_source_partial_with_device() -> None:
    factory = create_yolo_factory("local", "cpu", "http://h:8082")

    assert isinstance(factory, functools.partial)
    assert factory.func is create_jpeg_yolo_source
    assert factory.keywords == {"device": "cpu"}


def test_orin_returns_remote_source_partial_with_url() -> None:
    factory = create_yolo_factory("orin", "cpu", "http://h:8082")

    assert isinstance(factory, functools.partial)
    assert factory.func is create_remote_yolo_source
    assert factory.keywords == {"base_url": "http://h:8082"}


def test_unknown_kind_raises_value_error() -> None:
    with pytest.raises(ValueError):
        create_yolo_factory("gpu", "cpu", "http://h:8082")


# --- run_scenario 플래그 ---


def test_run_scenario_yolo_flags_default_to_local() -> None:
    args = run_scenario.build_arg_parser().parse_args(["--sim", "browser", "--operator-port", "1"])

    assert args.yolo == "local"
    assert args.yolo_url == "http://127.0.0.1:8082"
    assert DEFAULT_YOLO_URL == "http://127.0.0.1:8082"


def test_run_scenario_accepts_orin_and_rejects_unknown() -> None:
    parser = run_scenario.build_arg_parser()

    args = parser.parse_args(
        ["--sim", "browser", "--operator-port", "1", "--yolo", "orin", "--yolo-url", "http://10.0.0.5:8082"]
    )

    assert args.yolo == "orin"
    assert args.yolo_url == "http://10.0.0.5:8082"
    with pytest.raises(SystemExit):
        parser.parse_args(["--sim", "browser", "--operator-port", "1", "--yolo", "bad"])


# --- sim_demo 플래그 ---


def test_sim_demo_yolo_flags() -> None:
    default_args = sim_demo.parse_args([])
    orin_args = sim_demo.parse_args(["--live", "--yolo", "orin"])

    assert default_args.yolo == "local"
    assert default_args.yolo_url == DEFAULT_YOLO_URL
    assert orin_args.live
    assert orin_args.yolo == "orin"


# --- 가벼운 import ---


def test_yolo_factory_has_no_module_level_implementation_imports() -> None:
    tree = ast.parse(YOLO_FACTORY_PATH.read_text(encoding="utf-8"))

    imported = {node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}

    assert not imported & LAZY_ONLY_MODULES


def test_yolo_factory_imports_without_heavy_dependencies() -> None:
    # 무거운 모듈을 meta_path 에서 막아 두고 import 한다 — 하나라도 끌려오면 ImportError 로 실패한다
    script = (
        "import sys\n"
        f"BLOCKED = {HEAVY_MODULES!r}\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError(f'blocked: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import modules.drone.yolo_factory\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], cwd=WAVEPILOT_ROOT, capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT_SEC,
    )

    assert result.returncode == 0, result.stderr
