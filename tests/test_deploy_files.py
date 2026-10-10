"""Orin 배포 파일 정적 검사. 실제 빌드·기동은 사용자가 RUNBOOK 으로 한다(D-09).

stdlib 만 쓴다 (PyYAML 금지, D-07). compose 의 command 는 JSON 호환 flow sequence 로
작성돼 있어 json.loads 로 읽는다.
"""

import json
from pathlib import Path

import pytest

MODEL_PATH = "/models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf"

# 값이 뒤따르는 플래그. 나머지(--jinja 등)는 단독 스위치다.
VALUE_FLAGS = {"-m", "--alias", "--host", "--port", "-c", "-np", "-ngl", "--chat-template-kwargs"}


@pytest.fixture
def jetson_dir(wavepilot_root: Path) -> Path:
    return wavepilot_root / "deploy" / "jetson"


@pytest.fixture
def dockerfile_text(jetson_dir: Path) -> str:
    return (jetson_dir / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture
def compose_text(jetson_dir: Path) -> str:
    return (jetson_dir / "compose.yaml").read_text(encoding="utf-8")


@pytest.fixture
def runbook_text(jetson_dir: Path) -> str:
    return (jetson_dir / "RUNBOOK.md").read_text(encoding="utf-8")


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


def test_dockerfile_pins_base_and_llama_ref(dockerfile_text: str) -> None:
    assert "ARG L4T_TAG=r36.4.0" in dockerfile_text, "Dockerfile: expected ARG L4T_TAG=r36.4.0"
    assert "ARG LLAMA_CPP_REF=v0.6.0" in dockerfile_text, "Dockerfile: expected ARG LLAMA_CPP_REF=v0.6.0"

    from_lines = [line for line in get_code_lines(dockerfile_text) if line.startswith("FROM ")]
    base_from_count = sum("FROM nvcr.io/nvidia/l4t-jetpack:${L4T_TAG}" in line for line in from_lines)
    assert base_from_count == 2, f"Dockerfile: expected 2 l4t-jetpack FROM lines (build+runtime), got {from_lines}"


def test_dockerfile_builds_cuda_sm87_llama_server(dockerfile_text: str) -> None:
    for expected in (
        "ARG CUDA_ARCH=87",
        "-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}",
        "-DGGML_CUDA=ON",
        "--target llama-server",
    ):
        assert expected in dockerfile_text, f"Dockerfile: expected {expected!r}"


def test_runtime_image_contains_only_llama_server(dockerfile_text: str) -> None:
    assert 'ENTRYPOINT ["llama-server"]' in dockerfile_text, 'Dockerfile: expected ENTRYPOINT ["llama-server"]'

    code_lines = get_code_lines(dockerfile_text)
    copy_lines = [line for line in code_lines if line.startswith(("COPY", "ADD"))]
    foreign_copies = [line for line in copy_lines if not line.startswith("COPY --from=build")]
    assert foreign_copies == [], f"Dockerfile: only COPY --from=build allowed (D-08), got {foreign_copies}"

    code_text = "\n".join(code_lines).lower()
    for forbidden in ("pip", "python", "llm_module", ".gguf"):
        assert forbidden not in code_text, f"Dockerfile: {forbidden!r} must not appear outside comments (D-08)"


def test_compose_service_and_runtime(compose_text: str) -> None:
    assert "llama-report:" in compose_text, "compose.yaml: expected service llama-report"
    assert "llama-decision" not in compose_text, "compose.yaml: llama-decision belongs to Phase 02.1"

    for expected in (
        "runtime: nvidia",
        "image: wavepilot/llama-server:v0.6.0-r36.4.0",
        "LLAMA_CPP_REF: ${LLAMA_CPP_REF:-v0.6.0}",
        'CUDA_ARCH: "87"',
    ):
        assert expected in compose_text, f"compose.yaml: expected {expected!r}"


def test_compose_command_pins_every_server_flag(compose_text: str) -> None:
    command = load_compose_command(compose_text)
    values = flag_values(command)

    expected_values = {
        "-m": MODEL_PATH,
        "--alias": "nemotron-3-nano-4b",
        "--host": "0.0.0.0",
        "--port": "8080",
        "-c": "4096",
        "-np": "1",
        "-ngl": "999",
    }
    for flag, expected in expected_values.items():
        assert values.get(flag) == expected, f"compose.yaml command: expected {flag} {expected}, got {values.get(flag)}"

    assert "--jinja" in command, "compose.yaml command: expected --jinja"

    template_kwargs = json.loads(values["--chat-template-kwargs"])
    assert template_kwargs == {"enable_thinking": False}, (
        f"compose.yaml command: expected --chat-template-kwargs {{\"enable_thinking\": false}}, got {template_kwargs}"
    )


def test_compose_healthcheck_and_model_volume(compose_text: str) -> None:
    for expected in (
        "http://localhost:8080/health",
        "start_period: 120s",
        '"${MODELS_DIR:-../../models}:/models:ro"',
        "8080:8080",
    ):
        assert expected in compose_text, f"compose.yaml: expected {expected!r}"


def test_runbook_covers_user_steps(runbook_text: str) -> None:
    for expected in (
        "D-09",
        "cat /etc/nv_tegra_release",
        "nvcc --version",
        "sha256sum",
        "docker compose build llama-report",
        "docker compose up -d llama-report",
        "/health",
        "http://<orin-ip>:8080",
        "docker compose down",
        "ghcr.io/nvidia-ai-iot/llama_cpp",
        "NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf",
    ):
        assert expected in runbook_text, f"RUNBOOK.md: expected {expected!r}"


def test_no_model_weights_in_repo_tree(wavepilot_root: Path) -> None:
    gguf_files = list((wavepilot_root / "deploy").rglob("*.gguf"))
    assert gguf_files == [], f"deploy/: model weights must not be in the repo tree, found {gguf_files}"

    gitignore_lines = (wavepilot_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.gguf" in (line.strip() for line in gitignore_lines), ".gitignore: expected a '*.gguf' line"
