"""Orin 실시간 YOLO 서버 배포 파일 정적 검사 (Phase 7, D-06). 실제 기동은 사용자가 YOLO-RUNBOOK.md 로 한다.

compose.yaml 과 런북 "실시간 YOLO 서버 (Phase 7)" 절이 서버·벤치 CLI(D-02, D-04)와
--yolo 플래그(D-05)에서 어긋나지 않는지 문자열로만 확인한다 — 서버 모듈을 import 하지 않는다.
stdlib + pytest 만 쓴다 (PyYAML 금지). compose 의 flow sequence 는 JSON 호환이라 json.loads 로 읽는다.
헬퍼는 다른 테스트 모듈에서 import 하지 않고 여기 복사해 둔다.
"""

import json
import re
from pathlib import Path

import pytest

YOLO_JETSON_PARTS = ("modules", "yolo_detection", "deploy", "jetson")
RUNBOOK_NAME = "YOLO-RUNBOOK.md"
PHASE7_HEADING = "실시간 YOLO 서버 (Phase 7)"

YOLO_PORT = "8082"
ENGINE_PATH = "models/yolo11n.engine"
SERVER_COMMAND = (
    f"python -m modules.yolo_detection.remote.server --weights {ENGINE_PATH} "
    f"--host 0.0.0.0 --port {YOLO_PORT} --device 0 --imgsz 640"
)  # D-02 서버 CLI 그대로
LAP_GUARD = "python -c 'import lap' 2>/dev/null || pip install lapx"  # 런북 3단계의 lapx 대체
PORT_MAPPING = f'"${{YOLO_BIND_ADDR:-0.0.0.0}}:{YOLO_PORT}:{YOLO_PORT}"'
PORT_MAPPING_PATTERN = re.compile(r'"[^"\n]*:\d+:\d+"')

# 런북 Phase 7 절에 있어야 하는 사용자 실행 명령 (D-04 벤치, D-05 전환 플래그, D-06 배포 순서)
PHASE7_REQUIRED_SNIPPETS = (
    "rsync -avR modules/__init__.py modules/yolo_detection",
    f"ls -la ~/wavepilot/{ENGINE_PATH}",
    "docker compose up -d yolo-server",
    f"curl -s http://<orin-ip>:{YOLO_PORT}/health",
    f"python -m modules.yolo_detection.remote.bench --url http://<orin-ip>:{YOLO_PORT} --images",
    f"python -m modules.drone.run_scenario --sim browser --yolo orin --yolo-url http://<orin-ip>:{YOLO_PORT}",
    "--operator-port",
    f"python -m modules.drone.sim_demo --live --yolo orin --yolo-url http://<orin-ip>:{YOLO_PORT}",
    "--yolo local",
    "YOLO_BIND_ADDR",
    f"ufw allow from <mac-ip> to any port {YOLO_PORT}",
    "docker compose down",
    "docker compose logs --tail 50 yolo-server",
)
PHASE7_STEP_LABELS = tuple(f"### R{n}. " for n in range(9))
REPORT_FIELDS = ("orin yolo server ok", "p50", "p95", "max", "total_ms", "orin_ms", "net_ms", "/health", "07-ORIN-RESULTS.md")

# test_decision_deploy_files.py 와 같은 규칙: 실제 호스트는 <orin-ip> 같은 placeholder 로만 쓴다.
ALLOWED_IPV4 = frozenset({"127.0.0.1", "0.0.0.0"})
IPV4_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# 프로젝트 결정: 인증 없음 — 바인딩 주소 + ufw 로만 접근을 좁힌다.
API_KEY_PATTERN = re.compile(r"api[ _-]?key", re.IGNORECASE)


@pytest.fixture
def yolo_jetson_dir(wavepilot_root: Path) -> Path:
    return wavepilot_root.joinpath(*YOLO_JETSON_PARTS)


@pytest.fixture
def yolo_compose_text(yolo_jetson_dir: Path) -> str:
    return (yolo_jetson_dir / "compose.yaml").read_text(encoding="utf-8")


@pytest.fixture
def yolo_runbook_text(yolo_jetson_dir: Path) -> str:
    return (yolo_jetson_dir / RUNBOOK_NAME).read_text(encoding="utf-8")


def load_flow_sequence(text: str, key: str) -> list[str]:
    # 주석 속 같은 단어에 걸리지 않게 줄 맨 앞(들여쓰기 허용)의 `key:` 만 찾는다.
    key_match = re.search(rf"^\s*{re.escape(key)}:", text, re.MULTILINE)
    assert key_match is not None, f"compose.yaml: no {key}: key"

    open_index = text.index("[", key_match.end())
    close_index = text.index("]", open_index)

    return json.loads(text[open_index : close_index + 1])


def get_code_text(text: str) -> str:
    code_lines = (line for line in text.splitlines() if not line.lstrip().startswith("#"))

    return "\n".join(code_lines)


def get_phase7_section(text: str) -> str:
    heading_index = text.index(f"## {PHASE7_HEADING}")

    return text[heading_index:]


def test_yolo_compose_runs_server_cli(yolo_compose_text: str) -> None:
    command = load_flow_sequence(yolo_compose_text, "command")

    assert command[:2] == ["sh", "-c"], f"compose.yaml command: expected sh -c wrapper, got {command[:2]}"
    script = command[-1]
    assert f"exec {SERVER_COMMAND}" in script, f"compose.yaml command: expected exact D-02 CLI, got {script!r}"
    assert script.endswith(SERVER_COMMAND), f"compose.yaml command: no extra flags after the D-02 CLI, got {script!r}"
    assert script.index(LAP_GUARD) < script.index(SERVER_COMMAND), "compose.yaml command: lap guard must run before the server"


def test_yolo_compose_pins_project_image_and_port(yolo_compose_text: str) -> None:
    for expected in (
        "name: wavepilot-yolo",
        "yolo-server:",
        "image: ultralytics/ultralytics:latest-jetson-jetpack6",
        "runtime: nvidia",
        "ipc: host",
        PORT_MAPPING,
        '"PYTHONPATH=."',
        '"~/wavepilot:/workspace/wavepilot"',
        "working_dir: /workspace/wavepilot",
        'restart: "no"',
    ):
        assert expected in yolo_compose_text, f"compose.yaml: expected {expected!r}"


def test_yolo_compose_maps_only_yolo_port(yolo_compose_text: str) -> None:
    # d1 판단 서버(8081)·보고 LLM(8080) 포트와 겹치면 안 된다
    port_mappings = PORT_MAPPING_PATTERN.findall(yolo_compose_text)

    assert port_mappings == [PORT_MAPPING], f"compose.yaml: expected only the 8082 mapping, got {port_mappings}"
    for other_port in ("8080", "8081"):
        assert other_port not in yolo_compose_text, f"compose.yaml: port {other_port} belongs to another server"


def test_yolo_compose_healthcheck_hits_health(yolo_compose_text: str) -> None:
    health_test = load_flow_sequence(yolo_compose_text, "test")
    health_command = " ".join(health_test)

    assert health_test[0] == "CMD", f"compose.yaml healthcheck: expected CMD form, got {health_test}"
    assert f"127.0.0.1:{YOLO_PORT}/health" in health_command, f"compose.yaml healthcheck: expected /health on {YOLO_PORT}"
    assert "start_period:" in yolo_compose_text, "compose.yaml healthcheck: expected start_period for engine load"


def test_yolo_compose_has_no_build_or_api_key(yolo_compose_text: str) -> None:
    code_text = get_code_text(yolo_compose_text)

    assert re.search(r"^\s*build:", code_text, re.MULTILINE) is None, "compose.yaml: official image only, no build:"
    assert API_KEY_PATTERN.findall(yolo_compose_text) == [], "compose.yaml: no-auth project decision, API key must not appear"


def test_yolo_deploy_files_have_no_real_ip(yolo_compose_text: str, yolo_runbook_text: str) -> None:
    for file_name, text in (("compose.yaml", yolo_compose_text), (RUNBOOK_NAME, yolo_runbook_text)):
        found = set(IPV4_PATTERN.findall(text))
        assert found <= ALLOWED_IPV4, f"{file_name}: only 127.0.0.1 / 0.0.0.0 allowed, use placeholders, got {found - ALLOWED_IPV4}"


def test_yolo_runbook_phase7_states_user_only_execution(yolo_runbook_text: str) -> None:
    section = get_phase7_section(yolo_runbook_text)

    for expected in ("사용자가 실행", "에이전트는 실행하지 않는다", "상태 변경", "읽기 전용", "보고"):
        assert expected in section, f"{RUNBOOK_NAME} Phase 7: expected {expected!r}"


def test_yolo_runbook_phase7_documents_user_commands(yolo_runbook_text: str) -> None:
    section = get_phase7_section(yolo_runbook_text)

    for expected in PHASE7_REQUIRED_SNIPPETS:
        assert expected in section, f"{RUNBOOK_NAME} Phase 7: expected command {expected!r}"
    assert SERVER_COMMAND in section, f"{RUNBOOK_NAME} Phase 7: server CLI must match compose.yaml"


def test_yolo_runbook_phase7_steps_have_failure_notes(yolo_runbook_text: str) -> None:
    section = get_phase7_section(yolo_runbook_text)
    step_starts = [section.index(label) for label in PHASE7_STEP_LABELS]
    step_ends = step_starts[1:] + [section.index("### 보고 양식")]

    assert step_starts == sorted(step_starts), f"{RUNBOOK_NAME} Phase 7: R0 ~ R8 must be in order"
    for label, start, end in zip(PHASE7_STEP_LABELS, step_starts, step_ends):
        assert "**실패 시:**" in section[start:end], f"{RUNBOOK_NAME} Phase 7: no 실패 시 in {label.strip()!r}"


def test_yolo_runbook_phase7_report_lists_latency_split(yolo_runbook_text: str) -> None:
    report = get_phase7_section(yolo_runbook_text).split("### 보고 양식", 1)[1]

    for expected in REPORT_FIELDS:
        assert expected in report, f"{RUNBOOK_NAME} Phase 7 report: expected {expected!r}"


def test_yolo_runbook_phase7_has_no_api_key(yolo_runbook_text: str) -> None:
    api_key_mentions = API_KEY_PATTERN.findall(get_phase7_section(yolo_runbook_text))

    assert api_key_mentions == [], f"{RUNBOOK_NAME} Phase 7: no-auth project decision, API key must not appear, got {api_key_mentions}"


def test_yolo_runbook_no_longer_says_live_sim_out_of_scope(yolo_runbook_text: str) -> None:
    assert "라이브 시뮬레이터 연동과 드론 조립부 (`modules.drone.run_scenario`) 교체는 범위 밖이다" not in yolo_runbook_text
    assert PHASE7_HEADING in yolo_runbook_text.split(f"## {PHASE7_HEADING}", 1)[0], (
        f"{RUNBOOK_NAME}: intro table / closing note should point to the Phase 7 section"
    )
