"""README Python 절의 명령·경로가 코드와 어긋나면 실패한다 — README는 코드를 따라간다.

README.md 끝의 `## Python: LLM overwatch pipeline` 절만 읽어 정적으로 대조한다(프로세스를 띄우지 않는다).
플래그는 argparse 정의 소스에 문자열 리터럴로 있는지, 경로는 실제로 있는지, 마커는 pyproject 에
정의돼 있는지, 파사드 이름은 import 가능한지 본다. 기존 README 본문(D-01)은 검사 대상이 아니다.
"""

import importlib
import re
import tomllib
from pathlib import Path

import pytest

PYTHON_SECTION_HEADING = "## Python: LLM overwatch pipeline"
PREVIOUS_LAST_HEADING = "## Third-party code"

# README 패키지 지도는 들여쓰기 트리라 마지막 경로 조각만 적힌다 — 디스크 검사는 전체 경로로 한다
PACKAGE_MAP_PATHS = (
    "modules/",
    "modules/llm_report/", "modules/llm_report/interface/", "modules/llm_report/deploy/jetson/",
    "modules/llm_report/scripts/", "modules/llm_report/examples/",
    "modules/yolo_detection/", "modules/yolo_detection/interface/", "modules/yolo_detection/deploy/jetson/",
    "modules/perception/", "modules/perception/interface/",
    "modules/flight_control/", "modules/flight_control/interface/",
    "modules/comm/", "modules/comm/interface/",
    "modules/simulation/", "modules/drone/", "modules/operator_console/",
    "scripts/", "tests/",
)
# 01-09 가 같은 wave 4 에서 병렬로 만드는 경로. 패키지 지도 검사에서는 빼고, 실패가 01-09 를
# 가리키도록 test_same_wave_paths_exist 에서 따로 존재를 검사한다
SAME_WAVE_PENDING_PATHS = frozenset({
    "scripts/run_demo.sh", "modules/llm_report/scripts/run_llama_server_mac.sh", "modules/llm_report/examples/use_llm_module.py",
})

CONSOLE_TAGS = ("LISTEN", "LINK", "TELEMETRY", "STATE", "DECISION", "REPORT", "COMMAND", "WARN")
FACADE_NAMES = ("LLMModule", "LLMModuleConfig", "create_llm_module")
INTERFACE_NAMES = ("DetectionSource", "FlightControl")
INTERFACE_MODULES = ("modules.yolo_detection.interface", "modules.flight_control.interface")
ALLOWED_IPV4 = frozenset({"127.0.0.1", "0.0.0.0"})
RUNBOOK_PATH = "modules/llm_report/deploy/jetson/RUNBOOK.md"

DRONE_RUNNER_SOURCE = "modules/drone/run_scenario.py"
OPERATOR_CONSOLE_DIR = "modules/operator_console"

FLAG_PATTERN = re.compile(r"(?<!\S)--[a-z][a-z0-9-]*")
SCENARIO_PATH_PATTERN = re.compile(r"modules/simulation/scenarios/[A-Za-z0-9_]+\.toml")
MARKER_PATTERN = re.compile(r"pytest -m (\w+)")
SUBMODULE_IMPORT_PATTERN = re.compile(r"\b(from|import)\s+modules\.llm_report\.[a-z_]")
IPV4_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# 사용자 결정(2026-10-10): 인증 키는 어디에도 없다 — README 도 언급하지 않는다
API_KEY_PATTERN = re.compile(r"api[ _-]?key", re.IGNORECASE)


# --- 헬퍼 (README 읽기 → 명령 줄 추출 → 플래그 추출 → 소스 대조 순서) ---


def _read_python_section(root: Path) -> str:
    readme_text = (root / "README.md").read_text(encoding="utf-8")

    start = readme_text.find(PYTHON_SECTION_HEADING)
    if start < 0:
        pytest.fail(f"README is missing the Python section {PYTHON_SECTION_HEADING!r}")

    end = readme_text.find("\n## ", start + len(PYTHON_SECTION_HEADING))
    return readme_text[start:] if end < 0 else readme_text[start:end]


def _command_lines(section: str, module_name: str) -> list[str]:
    # 백슬래시 줄이음은 한 명령이므로 합친 뒤 판정한다
    joined_text = section.replace("\\\n", " ")
    return [line for line in joined_text.splitlines() if module_name in line]


def _flags_in(line: str) -> set[str]:
    return set(FLAG_PATTERN.findall(line))


def _source_text(paths: list[Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _operator_console_sources(root: Path) -> list[Path]:
    return sorted((root / OPERATOR_CONSOLE_DIR).rglob("*.py"))


# --- 테스트 ---


def test_python_section_is_appended_after_existing_body(wavepilot_root: Path) -> None:
    readme_text = (wavepilot_root / "README.md").read_text(encoding="utf-8")
    _read_python_section(wavepilot_root)

    assert PREVIOUS_LAST_HEADING in readme_text, f"existing heading {PREVIOUS_LAST_HEADING!r} was removed"
    assert readme_text.index(PYTHON_SECTION_HEADING) > readme_text.index(PREVIOUS_LAST_HEADING), (
        f"{PYTHON_SECTION_HEADING!r} must come after {PREVIOUS_LAST_HEADING!r} (append-only, D-01)"
    )
    assert readme_text.count(PYTHON_SECTION_HEADING) == 1


def test_package_map_paths_are_listed_and_exist(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    missing_in_readme = [path for path in PACKAGE_MAP_PATHS if f"{Path(path).name}/" not in section]
    missing_on_disk = [
        path for path in PACKAGE_MAP_PATHS
        if path not in SAME_WAVE_PENDING_PATHS and not (wavepilot_root / path).is_dir()
    ]

    assert not missing_in_readme, f"package map in README does not list: {missing_in_readme}"
    assert not missing_on_disk, f"README package map lists directories that do not exist: {missing_on_disk}"


def test_same_wave_paths_exist(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    mentioned_paths = sorted(path for path in SAME_WAVE_PENDING_PATHS if path in section)
    missing_paths = [path for path in mentioned_paths if not (wavepilot_root / path).exists()]

    assert mentioned_paths, "README does not mention any 01-09 script/example path"
    assert not missing_paths, f"README mentions 01-09 paths that do not exist yet: {missing_paths}"


def test_drone_runner_flags_exist_in_source(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)
    runner_source = _source_text([wavepilot_root / DRONE_RUNNER_SOURCE])

    lines = _command_lines(section, "python -m modules.drone.run_scenario")
    unknown_flags = sorted(
        {flag for line in lines for flag in _flags_in(line) if f'"{flag}"' not in runner_source}
    )

    assert lines, "README has no `python -m modules.drone.run_scenario` command line"
    assert not unknown_flags, f"README drone runner flags not defined in {DRONE_RUNNER_SOURCE}: {unknown_flags}"


def test_operator_console_flags_exist_in_source(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)
    console_source = _source_text(_operator_console_sources(wavepilot_root))

    lines = _command_lines(section, "python -m modules.operator_console")
    unknown_flags = sorted(
        {flag for line in lines for flag in _flags_in(line) if f'"{flag}"' not in console_source}
    )

    assert lines, "README has no `python -m modules.operator_console` command line"
    assert not unknown_flags, f"README operator console flags not defined in {OPERATOR_CONSOLE_DIR}/: {unknown_flags}"


def test_same_wave_script_flags_exist_in_source(wavepilot_root: Path) -> None:
    # 01-09 스크립트 명령 줄의 플래그도 그 스크립트 소스에 있어야 한다(파일이 없으면 위 테스트가 잡는다)
    section = _read_python_section(wavepilot_root)
    script_paths = sorted(path for path in SAME_WAVE_PENDING_PATHS if not path.endswith("/"))

    unknown_flags: list[str] = []
    for script_path in script_paths:
        script_file = wavepilot_root / script_path
        if not script_file.is_file():
            continue
        script_source = script_file.read_text(encoding="utf-8")
        for line in _command_lines(section, script_path):
            unknown_flags.extend(
                f"{script_path} {flag}" for flag in sorted(_flags_in(line)) if flag not in script_source
            )

    assert not unknown_flags, f"README script flags not handled by the script: {unknown_flags}"


def test_scenario_paths_exist(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    scenario_paths = sorted(set(SCENARIO_PATH_PATTERN.findall(section)))
    missing_paths = [path for path in scenario_paths if not (wavepilot_root / path).is_file()]

    assert scenario_paths, "README Python section names no scenario file"
    assert not missing_paths, f"README names scenario files that do not exist: {missing_paths}"


def test_pytest_markers_are_defined(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)
    pyproject = tomllib.loads((wavepilot_root / "pyproject.toml").read_text(encoding="utf-8"))

    marker_lines = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    defined_markers = {line.split(":", 1)[0].strip() for line in marker_lines}
    used_markers = set(MARKER_PATTERN.findall(section))

    assert used_markers, "README Python section shows no `pytest -m <marker>` command"
    assert used_markers <= defined_markers, (
        f"README uses pytest markers not defined in pyproject.toml: {sorted(used_markers - defined_markers)}"
    )


def test_console_tags_are_documented_and_emitted(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)
    console_source = _source_text(_operator_console_sources(wavepilot_root))

    undocumented_tags = [tag for tag in CONSOLE_TAGS if f"`{tag}`" not in section]
    unemitted_tags = [tag for tag in CONSOLE_TAGS if f'"{tag}"' not in console_source]

    assert not undocumented_tags, f"README does not explain console tags: {undocumented_tags}"
    assert not unemitted_tags, f"console tags documented in README but absent from {OPERATOR_CONSOLE_DIR}/: {unemitted_tags}"


def test_facade_and_interface_names_exist(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)
    llm_module = importlib.import_module("modules.llm_report")
    interface_modules = [importlib.import_module(name) for name in INTERFACE_MODULES]

    unmentioned_names = [name for name in (*FACADE_NAMES, *INTERFACE_NAMES) if name not in section]
    missing_facade_names = [name for name in FACADE_NAMES if not hasattr(llm_module, name)]
    missing_interface_names = [
        name for name in INTERFACE_NAMES if not any(hasattr(module, name) for module in interface_modules)
    ]

    assert not unmentioned_names, f"README simulator section does not mention: {unmentioned_names}"
    assert not missing_facade_names, f"modules.llm_report does not export: {missing_facade_names}"
    assert not missing_interface_names, f"{INTERFACE_MODULES} do not export: {missing_interface_names}"


def test_no_llm_module_submodule_imports_in_section(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    violations = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(section.splitlines(), start=1)
        if SUBMODULE_IMPORT_PATTERN.search(line)
    ]

    assert not violations, f"README must import only the top-level modules.llm_report facade (D-03): {violations}"


def test_runbook_link_exists(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    assert f"]({RUNBOOK_PATH})" in section, f"README Python section has no markdown link to {RUNBOOK_PATH}"
    assert (wavepilot_root / RUNBOOK_PATH).is_file(), f"{RUNBOOK_PATH} does not exist"


def test_only_local_ipv4_addresses(wavepilot_root: Path) -> None:
    # Orin 실주소가 문서로 새지 않게 한다 — 원격 호스트는 <orin-ip> 자리표시자만 쓴다
    section = _read_python_section(wavepilot_root)

    addresses = set(IPV4_PATTERN.findall(section))

    assert addresses <= ALLOWED_IPV4, f"README shows non-local IPv4 addresses: {sorted(addresses - ALLOWED_IPV4)}"


def test_no_api_key_mentioned(wavepilot_root: Path) -> None:
    section = _read_python_section(wavepilot_root)

    matches = sorted(set(API_KEY_PATTERN.findall(section)))

    assert not matches, f"README must not mention API keys (no auth key exists): {matches}"
