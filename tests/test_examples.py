"""사용자 진입 스크립트 계약: examples/use_llm_module.py (SC7, IF-06) 와 scripts/*.sh.

예제와 셸 스크립트는 외부 사용자처럼 subprocess 로 실행한다. 셸 스크립트는 실행 비트에 기대지 않고
`bash scripts/...` 로 부른다. llama-server 는 띄우지 않는다 — llama_server 경로는 닫힌 포트로
"서버 없음" 실패만 확인한다.
"""

import ast
import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

EXAMPLE_PATH = "examples/use_llm_module.py"
REPORT_SENTENCE_PATTERN = r"REPORT +\[(T\d+(?:,T\d+)*)\] \((observation|suggestion)\) \S"
SAMPLE_TRACK_IDS = {"T1", "T2", "T3"}
FORBIDDEN_EXAMPLE_IMPORTS = ("mocks", "comm", "perception", "drone", "operator_console")
DEMO_TIMEOUT_SEC = 90


# --- 헬퍼 ---


def _run(
    wavepilot_root: Path, args: list[str], *, env_extra: dict | None = None, timeout_sec: float = 30
) -> subprocess.CompletedProcess:
    # 예제는 설치되지 않은 패키지를 외부 스크립트처럼 import 하므로 PYTHONPATH 로 루트를 잡아 준다
    env = dict(os.environ)
    env["PYTHONPATH"] = str(wavepilot_root)
    env.update(env_extra or {})

    return subprocess.run(
        args, cwd=wavepilot_root, capture_output=True, text=True, timeout=timeout_sec, env=env
    )


def _closed_port_url() -> str:
    # 고정 포트(9 번 등)에 기대지 않고, 방금 비운 임의 포트를 "아무도 듣지 않는 곳"으로 쓴다
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    return f"http://127.0.0.1:{port}"


def _get_cited_track_ids(stdout: str) -> set[str]:
    cited_ids: set[str] = set()
    for match in re.finditer(REPORT_SENTENCE_PATTERN, stdout):
        cited_ids.update(match.group(1).split(","))

    return cited_ids


def _make_single_track_state(track_id: str) -> dict:
    return {
        "schema_version": 1,
        "time_sec": 12.0,
        "drone": {
            "position_ned_m": {"north": 0.0, "east": 0.0, "down": -40.0},
            "altitude_m": 40.0,
            "attitude_deg": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "gimbal_deg": {"pitch": -55.0, "yaw": 0.0},
            "battery_pct": 90.0,
            "flight_mode": "hover",
        },
        "tracks": [
            {
                "track_id": track_id,
                "class": "car",
                "position_ned_m": {"north": 30.0, "east": 5.0},
                "first_seen_sec": 2.0,
                "last_seen_sec": 12.0,
                "observation_count": 40,
                "mean_confidence": 0.8,
                "is_active": True,
            }
        ],
        "counts_by_class": {"car": 1},
        "omitted_track_count": 0,
    }


# --- examples/use_llm_module.py (SC7) ---


def test_example_fake_backend_prints_decision_and_cited_report(wavepilot_root: Path) -> None:
    result = _run(wavepilot_root, [sys.executable, EXAMPLE_PATH, "--backend", "fake"])

    assert result.returncode == 0, result.stderr
    assert len(re.findall(r"^DECISION verdict=(act|escalate)", result.stdout, re.MULTILINE)) == 1
    assert re.search(REPORT_SENTENCE_PATTERN, result.stdout), result.stdout
    assert _get_cited_track_ids(result.stdout) <= SAMPLE_TRACK_IDS


def test_example_json_output_is_single_payload(wavepilot_root: Path) -> None:
    result = _run(wavepilot_root, [sys.executable, EXAMPLE_PATH, "--json"])

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert {"time_sec", "decision", "report", "report_error"} <= payload.keys()


def test_example_state_file_cites_only_its_tracks(wavepilot_root: Path, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_make_single_track_state("T42")), encoding="utf-8")

    result = _run(wavepilot_root, [sys.executable, EXAMPLE_PATH, "--state", str(state_path)])

    assert result.returncode == 0, result.stderr
    assert _get_cited_track_ids(result.stdout) == {"T42"}


def test_example_llama_server_unreachable_exits_2_with_hint(wavepilot_root: Path) -> None:
    result = _run(
        wavepilot_root,
        [
            sys.executable, EXAMPLE_PATH, "--backend", "llama_server",
            "--backend-url", _closed_port_url(), "--ready-timeout-sec", "1",
        ],
    )

    assert result.returncode == 2
    assert "scripts/run_llama_server_mac.sh" in result.stderr
    assert "Traceback" not in result.stderr


def test_example_imports_only_stdlib_and_top_level_facade(wavepilot_root: Path) -> None:
    tree = ast.parse((wavepilot_root / EXAMPLE_PATH).read_text(encoding="utf-8"))

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")

    # IF-06: 외부 사용자는 llm_module 최상위 파사드만 안다
    for module_name in imported_modules:
        top_level_name = module_name.split(".")[0]
        assert not module_name.startswith("llm_module."), module_name
        assert top_level_name not in FORBIDDEN_EXAMPLE_IMPORTS, module_name
        assert top_level_name == "llm_module" or top_level_name in sys.stdlib_module_names, module_name
    assert "llm_module" in imported_modules


# --- scripts/*.sh ---


def test_shell_scripts_follow_conventions(wavepilot_root: Path) -> None:
    script_paths = sorted((wavepilot_root / "scripts").glob("*.sh"))
    assert script_paths

    for script_path in script_paths:
        text = script_path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "#!/usr/bin/env bash", script_path.name
        assert "set -euo pipefail" in text, script_path.name

        syntax_check = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
        assert syntax_check.returncode == 0, f"{script_path.name}: {syntax_check.stderr}"


def test_run_demo_rejects_unknown_backend(wavepilot_root: Path) -> None:
    result = _run(wavepilot_root, ["bash", "scripts/run_demo.sh", "--backend", "bogus"])

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_run_demo_help_exits_0(wavepilot_root: Path) -> None:
    result = _run(wavepilot_root, ["bash", "scripts/run_demo.sh", "--help"])

    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_run_demo_llama_server_unreachable_fails_before_children(wavepilot_root: Path) -> None:
    result = _run(
        wavepilot_root,
        ["bash", "scripts/run_demo.sh", "--backend", "llama_server", "--backend-url", _closed_port_url()],
    )

    assert result.returncode == 1
    assert "scripts/run_llama_server_mac.sh" in result.stderr
    # 자식 프로세스를 띄우기 전에 실패했다면 운용자 콘솔 LISTEN 줄이 없다
    assert "LISTEN" not in result.stdout


@pytest.mark.e2e
def test_run_demo_fake_backend_end_to_end(wavepilot_root: Path) -> None:
    result = _run(
        wavepilot_root,
        ["bash", "scripts/run_demo.sh", "--backend", "fake", "--speed", "0", "--port", "0"],
        timeout_sec=DEMO_TIMEOUT_SEC,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert re.search(REPORT_SENTENCE_PATTERN, output), output
    assert "LINK disconnected" in output


# --- scripts/run_llama_server_mac.sh ---

EXPECTED_SERVER_FLAGS = (
    "-hf nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M",
    "--alias nemotron-3-nano-4b",
    "--host 127.0.0.1",
    "--port 8080",
    "-c 4096",
    "-np 1",
    "-ngl 999",
    "--jinja",
    "--chat-template-kwargs",
    "enable_thinking",
)


def test_llama_server_print_command_has_ai_spec_flags(wavepilot_root: Path) -> None:
    result = _run(wavepilot_root, ["bash", "scripts/run_llama_server_mac.sh", "--print-command"])

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 1
    for flag in EXPECTED_SERVER_FLAGS:
        assert flag in result.stdout, flag


def test_llama_server_port_is_overridable(wavepilot_root: Path) -> None:
    result = _run(
        wavepilot_root, ["bash", "scripts/run_llama_server_mac.sh", "--print-command"], env_extra={"LLAMA_PORT": "8099"}
    )

    assert result.returncode == 0, result.stderr
    assert "--port 8099" in result.stdout


# --- scripts/smoke_llama_server.py ---

SMOKE_CHECK_NAMES = ("health", "models", "json_schema_nested", "json_schema_flat", "enum_grammar", "thinking_off")


def _get_requested_schema(response_format: dict) -> dict:
    # 중첩형(OpenAI)과 평평한 형태(llama.cpp json_object + schema) 둘 다 받는다
    if "json_schema" in response_format:
        return response_format["json_schema"].get("schema", {})

    return response_format.get("schema", {})


def _make_fake_llama_handler(think_leak: bool) -> type[BaseHTTPRequestHandler]:
    class FakeLlamaHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json({"status": "ok"})
            elif self.path == "/v1/models":
                self._send_json({"data": [{"id": "nemotron-3-nano-4b"}]})
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            schema = _get_requested_schema(body.get("response_format", {}))

            has_track_ids = "track_ids" in schema.get("properties", {})
            content = json.dumps({"track_ids": ["T-7"]} if has_track_ids else {"answer": "ok"})
            if think_leak:
                content = "<think>x</think>" + content

            self._send_json(
                {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
            )

        def _send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return FakeLlamaHandler


@pytest.fixture
def fake_llama_server(request):
    think_leak = getattr(request, "param", {}).get("think_leak", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_fake_llama_handler(think_leak))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    yield f"http://127.0.0.1:{server.server_address[1]}"

    server.shutdown()
    server.server_close()


def test_smoke_passes_against_healthy_server(wavepilot_root: Path, fake_llama_server: str) -> None:
    result = _run(
        wavepilot_root,
        [sys.executable, "scripts/smoke_llama_server.py", "--url", fake_llama_server, "--ready-timeout-sec", "5"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    passed_names = re.findall(r"^PASS (\w+)", result.stdout, re.MULTILINE)
    assert passed_names == list(SMOKE_CHECK_NAMES)
    assert "FAIL" not in result.stdout


@pytest.mark.parametrize("fake_llama_server", [{"think_leak": True}], indirect=True)
def test_smoke_fails_when_thinking_leaks(wavepilot_root: Path, fake_llama_server: str) -> None:
    result = _run(
        wavepilot_root,
        [sys.executable, "scripts/smoke_llama_server.py", "--url", fake_llama_server, "--ready-timeout-sec", "5"],
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert re.search(r"^FAIL thinking_off", result.stdout, re.MULTILINE), result.stdout


def test_smoke_unreachable_server_exits_2(wavepilot_root: Path) -> None:
    result = _run(
        wavepilot_root,
        [sys.executable, "scripts/smoke_llama_server.py", "--url", _closed_port_url(), "--ready-timeout-sec", "1"],
    )

    assert result.returncode == 2
    assert "server not reachable" in result.stderr
    assert "Traceback" not in result.stderr
