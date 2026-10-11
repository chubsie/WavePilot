"""YOLO Jetson 런북 정적 검사 (DET-02). 실제 엔진 export·측정은 사용자가 YOLO-RUNBOOK.md 로 한다.

run_yolo 명령은 계약 C4 플래그만 쓴다 — 런북과 CLI 가 어긋나면 여기서 잡는다.
stdlib + pytest 만 쓴다 (test_deploy_files.py 와 같은 규칙).
"""

import re
from pathlib import Path

import pytest

RUNBOOK_NAME = "YOLO-RUNBOOK.md"  # deploy/jetson 아래 사용자 실행 런북

RUN_YOLO_FLAGS = frozenset(
    {"--frames", "--video", "--weights", "--device", "--tracker", "--fps", "--output", "--bench", "--warmup"}
)  # 계약 C4: modules/yolo_detection/run_yolo.py 가 받는 플래그 전부

BENCH_SUMMARY_KEYS = (
    "weights",
    "device",
    "frames_measured",
    "warmup",
    "fps_mean",
    "latency_ms_mean",
    "latency_ms_p95",
    "image_width",
    "image_height",
)  # C4 --bench 요약 JSON 키

FLAG_PATTERN = re.compile(r"(?<!\S)--[a-z][a-z0-9-]*")  # 공백 뒤에서 시작하는 긴 옵션만 플래그로 본다

SECTION_HEADING_PATTERN = re.compile(r"^## (\d+)\. ", re.MULTILINE)  # 번호 단계 제목

EXPORT_COMMAND = "format=engine half=True imgsz=640"  # Pitfall 5: llama-server 중지 상태에서만 실행


@pytest.fixture
def yolo_runbook_text(wavepilot_root: Path) -> str:
    return (wavepilot_root / "modules" / "yolo_detection" / "deploy" / "jetson" / RUNBOOK_NAME).read_text(encoding="utf-8")


def get_run_yolo_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "modules/yolo_detection/run_yolo.py" in line]


def get_numbered_sections(text: str) -> list[str]:
    starts = [match.start() for match in SECTION_HEADING_PATTERN.finditer(text)]
    ends = starts[1:] + [len(text)]

    return [text[start:end] for start, end in zip(starts, ends)]


def test_yolo_runbook_states_user_only_execution(yolo_runbook_text: str) -> None:
    for expected in ("에이전트는 실행하지 않는다", "읽기 전용", "상태 변경", "보고"):
        assert expected in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected {expected!r}"


def test_yolo_runbook_checks_environment_assumptions(yolo_runbook_text: str) -> None:
    expected_list = (
        "cat /etc/nv_tegra_release",
        "ultralytics/ultralytics:latest-jetson-jetpack6",
        "latest-jetson-jetpack5",
        "--runtime nvidia",
        "PYTHONPATH=.",
        "python3 --version",
        "import lap",
        "lapx",
    )

    for expected in expected_list:
        assert expected in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected {expected!r}"


def test_yolo_runbook_places_official_weights(yolo_runbook_text: str) -> None:
    for expected in ("models/yolo11n.pt", "github.com/ultralytics/assets/releases", "sha256sum"):
        assert expected in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected {expected!r}"


def test_yolo_runbook_exports_engine_with_llama_stopped(yolo_runbook_text: str) -> None:
    stop_index = yolo_runbook_text.index("docker compose stop llama-report")
    export_index = yolo_runbook_text.index(EXPORT_COMMAND)
    restart_index = yolo_runbook_text.index("docker compose up -d llama-report", export_index)
    export_line = next(line for line in yolo_runbook_text.splitlines() if EXPORT_COMMAND in line)

    assert stop_index < export_index < restart_index
    assert "model=models/yolo11n.pt" in export_line


def test_yolo_runbook_fixes_power_mode(yolo_runbook_text: str) -> None:
    for expected in ("nvpmodel -m 0", "jetson_clocks"):
        assert expected in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected {expected!r}"


def test_yolo_runbook_measures_three_scenarios(yolo_runbook_text: str) -> None:
    expected_list = (
        "시나리오 A",
        "시나리오 B",
        "시나리오 C",
        "tegrastats",
        "docker stats",
        "free -h",
        "bench_A.json",
        "bench_C.json",
    )

    for expected in expected_list:
        assert expected in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected {expected!r}"


def test_run_yolo_commands_use_only_contract_flags(yolo_runbook_text: str) -> None:
    run_yolo_lines = get_run_yolo_lines(yolo_runbook_text)
    violations: list[str] = []
    for line in run_yolo_lines:
        for flag in FLAG_PATTERN.findall(line):
            if flag not in RUN_YOLO_FLAGS:
                violations.append(f"{line.strip()} -> {flag}")

    bench_lines = [
        line
        for line in run_yolo_lines
        if "--bench" in line and "--weights models/yolo11n.engine" in line and "--warmup 10" in line
    ]

    assert run_yolo_lines, "YOLO-RUNBOOK.md: no modules/yolo_detection/run_yolo.py command line"
    assert not violations, "\n".join(violations)
    assert bench_lines, "YOLO-RUNBOOK.md: no engine --bench --warmup 10 command line"


def test_yolo_runbook_mentions_bench_summary_keys(yolo_runbook_text: str) -> None:
    for key in BENCH_SUMMARY_KEYS:
        assert key in yolo_runbook_text, f"YOLO-RUNBOOK.md: expected bench key {key!r}"


def test_yolo_runbook_steps_have_failure_notes(yolo_runbook_text: str) -> None:
    sections = get_numbered_sections(yolo_runbook_text)

    assert sections, "YOLO-RUNBOOK.md: no numbered sections"
    for section in sections[:-1]:
        title = section.splitlines()[0]
        assert "**실패 시:**" in section, f"YOLO-RUNBOOK.md: no 실패 시 in {title!r}"


def test_yolo_runbook_report_form_lists_results(yolo_runbook_text: str) -> None:
    report_section = get_numbered_sections(yolo_runbook_text)[-1]

    for expected in ("fps_mean", "latency_ms_p95", "RAM", "GR3D", "JetPack", "TensorRT", "ultralytics"):
        assert expected in report_section, f"YOLO-RUNBOOK.md report step: expected {expected!r}"
