"""d1-3B 판단 서버 Orin 배포 파일 정적 검사. 실제 빌드·기동은 사용자가 RUNBOOK 으로 한다(D-10).

stdlib 만 쓴다 (PyYAML 금지). compose 의 command 는 JSON 호환 flow sequence 로
작성돼 있어 json.loads 로 읽는다. 헬퍼는 test_deploy_files.py 와 같은 로직을 복사해 둔다 —
테스트 모듈끼리 import 하지 않는다.
"""

import json
import re
from pathlib import Path

import pytest

DECISION_JETSON_PARTS = ("modules", "decision", "deploy", "jetson")
MODEL_PATH = "/models/d1-3B-Q4_K_M.gguf"
DECISION_PORT = "8081"
LLAMA_CPP_REF = "b11541"
# 이미지 태그는 빌드 인자와 같은 변수로 만든다 — 다른 ref·베이스로 빌드해도 이름이 내용과 맞는다 (IN-08)
COMPOSE_IMAGE = "image: wavepilot/llama-server:${LLAMA_CPP_REF:-b11541}-${L4T_TAG:-r36.4.0}"
HARDCODED_IMAGE_PATTERN = re.compile(r"^\s*image:\s*wavepilot/llama-server:b11541-r36\.4\.0\s*$", re.MULTILINE)
DOCKERFILE_LIBRARY_PATH = "LIBRARY_PATH=/usr/local/cuda/lib64/stubs"
SMOKE_BODY_COMMAND = "uv run python -m modules.decision.sample_request > /tmp/d1_smoke.json"
SMOKE_BODY_COPY = "scp /tmp/d1_smoke.json <user>@<orin-ip>:/tmp/"

# 값이 뒤따르는 플래그.
VALUE_FLAGS = {"-m", "--alias", "--host", "--port", "-c", "-np", "-ngl"}
FORBIDDEN_FLAGS = ("--jinja", "--chat-template-kwargs", "--mmproj", "-hf", "--api-key")

# test_readme_commands.py 와 같은 규칙: 실제 호스트는 <orin-ip> 같은 placeholder 로만 쓴다.
ALLOWED_IPV4 = frozenset({"127.0.0.1", "0.0.0.0"})
IPV4_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# 프로젝트 결정: 인증 없음 — RUNBOOK 은 바인딩 주소 + ufw 로만 접근을 좁힌다.
API_KEY_PATTERN = re.compile(r"api[ _-]?key", re.IGNORECASE)


@pytest.fixture
def decision_jetson_dir(wavepilot_root: Path) -> Path:
    return wavepilot_root.joinpath(*DECISION_JETSON_PARTS)


@pytest.fixture
def decision_dockerfile_text(decision_jetson_dir: Path) -> str:
    return (decision_jetson_dir / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture
def decision_compose_text(decision_jetson_dir: Path) -> str:
    return (decision_jetson_dir / "compose.yaml").read_text(encoding="utf-8")


@pytest.fixture
def decision_runbook_text(decision_jetson_dir: Path) -> str:
    return (decision_jetson_dir / "RUNBOOK.md").read_text(encoding="utf-8")


def load_compose_command(text: str) -> list[str]:
    command_start = text.index("command:")
    open_index = text.index("[", command_start)
    close_index = text.index("]", open_index)

    return json.loads(text[open_index : close_index + 1])


def flag_values(cmd: list[str]) -> dict[str, str]:
    values = {}
    for i, token in enumerate(cmd[:-1]):
        if token in VALUE_FLAGS:
            values[token] = cmd[i + 1]

    return values


def get_code_lines(dockerfile: str) -> list[str]:
    stripped_lines = (line.strip() for line in dockerfile.splitlines())

    return [line for line in stripped_lines if line and not line.startswith("#")]


def test_decision_dockerfile_pins_base_and_llama_ref(decision_dockerfile_text: str) -> None:
    assert "ARG L4T_TAG=r36.4.0" in decision_dockerfile_text, "Dockerfile: expected ARG L4T_TAG=r36.4.0"
    assert f"ARG LLAMA_CPP_REF={LLAMA_CPP_REF}" in decision_dockerfile_text, (
        f"Dockerfile: expected ARG LLAMA_CPP_REF={LLAMA_CPP_REF}"
    )
    assert "ARG LLAMA_CPP_REF=v0.6.0" not in decision_dockerfile_text, (
        "Dockerfile: v0.6.0 cannot load d1-3B (lfm2-d1), expected b11541"
    )

    from_lines = [line for line in get_code_lines(decision_dockerfile_text) if line.startswith("FROM ")]
    base_from_count = sum("FROM nvcr.io/nvidia/l4t-jetpack:${L4T_TAG}" in line for line in from_lines)
    assert base_from_count == 2, f"Dockerfile: expected 2 l4t-jetpack FROM lines (build+runtime), got {from_lines}"


def test_decision_dockerfile_builds_cuda_sm87_llama_server(decision_dockerfile_text: str) -> None:
    for expected in (
        "ARG CUDA_ARCH=87",
        "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}",
        "-DGGML_CUDA=ON",
        "--target llama-server",
        "https://github.com/ggml-org/llama.cpp",
    ):
        assert expected in decision_dockerfile_text, f"Dockerfile: expected {expected!r}"


def test_decision_dockerfile_library_path_has_no_empty_entry(decision_dockerfile_text: str) -> None:
    # 베이스 이미지에 LIBRARY_PATH 가 없어 ${LIBRARY_PATH} 를 이어 붙이면 빈 항목(현재 디렉터리)이 생긴다 (IN-09)
    code_lines = get_code_lines(decision_dockerfile_text)

    library_path_lines = [line for line in code_lines if "LIBRARY_PATH=" in line]
    assert library_path_lines == [DOCKERFILE_LIBRARY_PATH], (
        f"Dockerfile: expected exactly {DOCKERFILE_LIBRARY_PATH!r}, got {library_path_lines}"
    )
    assert "${LIBRARY_PATH}" not in "\n".join(code_lines),"Dockerfile: LIBRARY_PATH must not append the unset ${LIBRARY_PATH}"


def test_decision_runtime_image_contains_only_llama_server(decision_dockerfile_text: str) -> None:
    assert 'ENTRYPOINT ["llama-server"]' in decision_dockerfile_text, 'Dockerfile: expected ENTRYPOINT ["llama-server"]'
    assert f"EXPOSE {DECISION_PORT}" in decision_dockerfile_text, f"Dockerfile: expected EXPOSE {DECISION_PORT}"

    code_lines = get_code_lines(decision_dockerfile_text)
    copy_lines = [line for line in code_lines if line.startswith(("COPY", "ADD"))]
    foreign_copies = [line for line in copy_lines if not line.startswith("COPY --from=build")]
    assert foreign_copies == [], f"Dockerfile: only COPY --from=build allowed, got {foreign_copies}"

    code_text = "\n".join(code_lines).lower()
    for forbidden in ("pip", "python", ".gguf", "modules/"):
        assert forbidden not in code_text, f"Dockerfile: {forbidden!r} must not appear outside comments"

    cmd_lines = [line for line in code_lines if line.startswith("CMD")]
    assert cmd_lines == [], f"Dockerfile: server flags live only in compose.yaml, got CMD {cmd_lines}"


def test_decision_compose_project_and_service(decision_compose_text: str) -> None:
    for expected in (
        "name: wavepilot-decision",
        "llama-decision:",
        "runtime: nvidia",
        COMPOSE_IMAGE,
        "LLAMA_CPP_REF: ${LLAMA_CPP_REF:-b11541}",
        "L4T_TAG: ${L4T_TAG:-r36.4.0}",
        'CUDA_ARCH: "87"',
        'restart: "no"',
    ):
        assert expected in decision_compose_text, f"compose.yaml: expected {expected!r}"

    assert "llama-report" not in decision_compose_text, "compose.yaml: llama-report belongs to modules/llm_report"
    assert HARDCODED_IMAGE_PATTERN.search(decision_compose_text) is None, (
        "compose.yaml: image tag must follow LLAMA_CPP_REF / L4T_TAG, not be hardcoded"
    )


def test_decision_compose_command_pins_every_server_flag(decision_compose_text: str) -> None:
    command = load_compose_command(decision_compose_text)
    values = flag_values(command)

    expected_values = {
        "-m": MODEL_PATH,
        "--alias": "d1-3b",
        "--host": "0.0.0.0",
        "--port": DECISION_PORT,
        "-c": "4096",
        "-np": "1",
        "-ngl": "999",
    }
    for flag, expected in expected_values.items():
        assert values.get(flag) == expected, f"compose.yaml command: expected {flag} {expected}, got {values.get(flag)}"

    for forbidden in FORBIDDEN_FLAGS:
        assert forbidden not in command, f"compose.yaml command: {forbidden} must not be passed to the decision server"


def test_decision_compose_healthcheck_port_and_volume(decision_compose_text: str) -> None:
    for expected in (
        f"http://localhost:{DECISION_PORT}/health",
        "start_period:",
        f'"${{LLAMA_BIND_ADDR:-0.0.0.0}}:{DECISION_PORT}:{DECISION_PORT}"',
        '"${MODELS_DIR:-../../../../models}:/models:ro"',
    ):
        assert expected in decision_compose_text, f"compose.yaml: expected {expected!r}"

    assert "8080" not in decision_compose_text, "compose.yaml: the old report port must not appear"


def test_decision_deploy_files_have_no_real_ip(decision_jetson_dir: Path) -> None:
    for file_name in ("compose.yaml", "Dockerfile", "RUNBOOK.md"):
        text = (decision_jetson_dir / file_name).read_text(encoding="utf-8")
        found = set(IPV4_PATTERN.findall(text))
        assert found <= ALLOWED_IPV4, f"{file_name}: only 127.0.0.1 / 0.0.0.0 allowed, use <orin-ip> placeholders, got {found - ALLOWED_IPV4}"


def test_decision_runbook_covers_user_steps(decision_runbook_text: str) -> None:
    for expected in (
        "D-10",
        "cat /etc/nv_tegra_release",
        "nvcc --version",
        "hf download LiquidAI/d1-3B-GGUF d1-3B-Q4_K_M.gguf",
        "sha256sum",
        "1674456672",
        "docker compose build llama-decision",
        "docker compose up -d llama-decision",
        "decision model type: lfm2-d1",
        "/v1/models",
        f"http://localhost:{DECISION_PORT}/health",
        f"http://localhost:{DECISION_PORT}/v1/systemone",
        f"http://<orin-ip>:{DECISION_PORT}",
        "docker compose down",
        LLAMA_CPP_REF,
    ):
        assert expected in decision_runbook_text, f"RUNBOOK.md: expected {expected!r}"


def test_decision_runbook_has_latency_cross_check(decision_runbook_text: str) -> None:
    for expected in (
        "%{time_connect}",
        "%{time_starttransfer}",
        "%{time_total}",
        f"localhost:{DECISION_PORT}",
        f"<orin-ip>:{DECISION_PORT}",
        "network_rtt_ms",
        "orin_processing_ms",
    ):
        assert expected in decision_runbook_text, f"RUNBOOK.md: expected latency cross-check token {expected!r}"


def test_decision_runbook_access_control_without_api_key(decision_runbook_text: str) -> None:
    for expected in ("LLAMA_BIND_ADDR", "ufw"):
        assert expected in decision_runbook_text, f"RUNBOOK.md: expected access control via {expected!r}"

    api_key_mentions = API_KEY_PATTERN.findall(decision_runbook_text)
    assert api_key_mentions == [], f"RUNBOOK.md: no-auth project decision, API key must not appear, got {api_key_mentions}"


def test_decision_runbook_states_agent_does_not_execute(decision_runbook_text: str) -> None:
    assert "에이전트는 실행하지 않는다" in decision_runbook_text, "RUNBOOK.md: expected '에이전트는 실행하지 않는다' (D-10)"


def test_decision_runbook_checks_gpu_with_list_devices(decision_runbook_text: str) -> None:
    # --version 은 CUDA 장치를 출력하지 않는다(실측) — GPU 인식은 --list-devices, 사용은 tegrastats 로 본다
    for expected in (
        "docker compose run --rm --no-deps llama-decision --list-devices",
        "CUDA0: Orin",
        "build 1",
        "GR3D_FREQ",
        "tmux",
    ):
        assert expected in decision_runbook_text, f"RUNBOOK.md: expected {expected!r}"


def test_decision_runbook_image_name_follows_build_args(decision_runbook_text: str) -> None:
    assert "${LLAMA_CPP_REF}-${L4T_TAG}" in decision_runbook_text, "RUNBOOK.md: expected the variable image tag"
    assert "이미지 이름은 `b11541-r36.4.0` 그대로" not in decision_runbook_text, (
        "RUNBOOK.md: the image name now follows LLAMA_CPP_REF, the old 'name stays the same' note is stale"
    )
    assert "`image:` 이름은 그대로" not in decision_runbook_text, (
        "RUNBOOK.md: the image name now follows L4T_TAG, the old 'name stays the same' note is stale"
    )


def test_decision_runbook_smoke_body_comes_from_code(wavepilot_root: Path, decision_runbook_text: str) -> None:
    # 스모크 본문은 실제 v2 요청과 같아야 7단계 지연 분해가 의미 있다 (IN-07)
    assert (wavepilot_root / "modules" / "decision" / "sample_request.py").is_file()
    for expected in (SMOKE_BODY_COMMAND, SMOKE_BODY_COPY):
        assert expected in decision_runbook_text, f"RUNBOOK.md: expected smoke body step {expected!r}"

    for stale in ("cat > /tmp/d1_smoke.json", "class_name", "target_class", "distance_m"):
        assert stale not in decision_runbook_text, f"RUNBOOK.md: hand-written v1-shaped smoke body remnant {stale!r}"
