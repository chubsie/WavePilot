"""run_compare 비교 진입점 — 가짜 프로세스·브라우저·health·시계로 실제 Playwright·Orin·run_mission 없이 검사한다."""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from modules.drone import run_compare
from modules.drone.run_compare import (
    CompareDeps,
    CompareOptions,
    build_arg_parser,
    build_page_url,
    build_run_command,
    build_run_env,
    get_run_timeout_sec,
    plan_runs,
)
from modules.simulation.compare_scenario import build_ground_truth, list_scenario_ids, load_compare_scenario

YOLO_URL = "http://127.0.0.1:18082"
D1_URL = "http://127.0.0.1:18081"
FIXED_NOW = datetime(2026, 10, 11, 12, 0, 0)


# ---------------------------------------------------------------- 가짜 도구


class FakeClock:
    def __init__(self, value: datetime = FIXED_NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeHealth:
    """url → 오류 문자열. 등록 안 된 URL 은 정상(None)."""

    def __init__(self, errors: dict[str, str] | None = None) -> None:
        self.errors = errors or {}
        self.called_urls: list[str] = []

    def __call__(self, url: str, timeout_sec: float) -> str | None:
        self.called_urls.append(url)
        return self.errors.get(url)


def _not_called(*_args):
    raise AssertionError("must not be called")


def make_deps(**overrides) -> CompareDeps:
    deps = CompareDeps(
        launch_process=_not_called,
        open_browser=_not_called,
        probe_health=FakeHealth(),
        wait_for_port=_not_called,
        now=FakeClock(),
    )
    return replace(deps, **overrides)


def make_options(tmp_path: Path, **overrides) -> CompareOptions:
    options = CompareOptions(
        scenarios=(load_compare_scenario("basic"), load_compare_scenario("no_red")),
        selectors=("rule", "d1"),
        repeats=1,
        out_dir=tmp_path / "compare",
        yolo_url=YOLO_URL,
        d1_url=D1_URL,
        sim_port=8766,
        headless=True,
        run_timeout_sec=None,
    )
    return replace(options, **overrides)


def parse_args(*argv: str) -> argparse.Namespace:
    return build_arg_parser().parse_args(list(argv))


# ---------------------------------------------------------------- 서버 없음 (D-14)


@pytest.fixture
def report_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"server_missing": [], "summary": [], "run_all": []}

    def fake_server_missing(out_dir, missing, checked_at):
        calls["server_missing"].append((out_dir, missing, checked_at))
        return out_dir / "summary.md", out_dir / "summary.csv"

    def fake_summary(out_dir, outcomes):
        calls["summary"].append((out_dir, list(outcomes)))
        return out_dir / "summary.md", out_dir / "summary.csv"

    def fake_run_all(options, deps, outcomes):
        calls["run_all"].append(options)

    monkeypatch.setattr(run_compare, "write_server_missing", fake_server_missing)
    monkeypatch.setattr(run_compare, "write_summary", fake_summary)
    monkeypatch.setattr(run_compare, "_run_all", fake_run_all)
    return calls


def test_missing_d1_server_stops_before_any_run(tmp_path: Path, report_calls: dict[str, list]) -> None:
    options = make_options(tmp_path)
    health = FakeHealth({D1_URL + "/health": "connection refused"})

    exit_code = run_compare.run_comparison(options, make_deps(probe_health=health))

    assert exit_code == 1
    assert report_calls["server_missing"] == [
        (options.out_dir, [("d1", D1_URL + "/health", "connection refused")], FIXED_NOW.isoformat(timespec="seconds")),
    ]
    assert report_calls["run_all"] == []
    assert report_calls["summary"] == []


def test_missing_yolo_server_stops(tmp_path: Path, report_calls: dict[str, list]) -> None:
    options = make_options(tmp_path)
    health = FakeHealth({YOLO_URL + "/health": "timed out"})

    exit_code = run_compare.run_comparison(options, make_deps(probe_health=health))

    assert exit_code == 1
    assert report_calls["server_missing"][0][1] == [("yolo", YOLO_URL + "/health", "timed out")]
    assert report_calls["run_all"] == []


def test_both_servers_missing_lists_d1_first(tmp_path: Path, report_calls: dict[str, list]) -> None:
    options = make_options(tmp_path)
    health = FakeHealth({D1_URL + "/health": "refused", YOLO_URL + "/health": "refused"})

    exit_code = run_compare.run_comparison(options, make_deps(probe_health=health))

    assert exit_code == 1
    assert [name for name, _, _ in report_calls["server_missing"][0][1]] == ["d1", "yolo"]


def test_rule_only_skips_d1_check(tmp_path: Path, report_calls: dict[str, list]) -> None:
    options = make_options(tmp_path, selectors=("rule",), d1_url=None)
    health = FakeHealth()

    exit_code = run_compare.run_comparison(options, make_deps(probe_health=health))

    assert exit_code == 0
    assert health.called_urls == [YOLO_URL + "/health"]
    assert report_calls["run_all"] == [options]
    assert len(report_calls["summary"]) == 1


def test_missing_server_writes_real_result_file(tmp_path: Path) -> None:
    options = make_options(tmp_path)
    health = FakeHealth({D1_URL + "/health": "connection refused"})

    exit_code = run_compare.run_comparison(options, make_deps(probe_health=health))

    summary_text = (options.out_dir / "summary.md").read_text(encoding="utf-8")
    assert exit_code == 1
    assert D1_URL + "/health" in summary_text
    assert (options.out_dir / "summary.csv").is_file()


def test_health_url_ignores_trailing_slash(tmp_path: Path, report_calls: dict[str, list]) -> None:
    options = make_options(tmp_path, yolo_url=YOLO_URL + "/", d1_url=D1_URL + "/")
    health = FakeHealth()

    run_compare.run_comparison(options, make_deps(probe_health=health))

    assert health.called_urls == [D1_URL + "/health", YOLO_URL + "/health"]


# ---------------------------------------------------------------- 실행 계획·명령줄·env·URL


def test_plan_runs_interleaves_selectors_within_each_repeat() -> None:
    runs = plan_runs(["basic", "no_red"], ["rule", "d1"], repeats=2)

    assert runs == [
        (1, "basic", "rule"), (1, "basic", "d1"), (1, "no_red", "rule"), (1, "no_red", "d1"),
        (2, "basic", "rule"), (2, "basic", "d1"), (2, "no_red", "rule"), (2, "no_red", "d1"),
    ]


def test_build_run_command_is_an_argument_list(tmp_path: Path) -> None:
    options = make_options(tmp_path, sim_port=8800)
    scenario = load_compare_scenario("basic")
    rundir = tmp_path / "basic__rule__r1"

    command = build_run_command(options, scenario, rundir)

    assert command == [
        sys.executable, "-m", "modules.drone.run_mission",
        "--sim", "browser",
        "--yolo", "orin",
        "--yolo-url", YOLO_URL,
        "--mission", scenario.mission_path,
        "--log-dir", str(rundir),
        "--sim-port", "8800",
    ]


def test_build_run_env_copies_and_sets_selector() -> None:
    base_env = {"PATH": "/usr/bin", "WAVEPILOT_SELECTOR": "d1"}

    run_env = build_run_env(base_env, "rule", D1_URL)

    assert run_env["WAVEPILOT_SELECTOR"] == "rule"
    assert run_env["D1_SERVER_URL"] == D1_URL
    assert run_env["PATH"] == "/usr/bin"
    assert base_env == {"PATH": "/usr/bin", "WAVEPILOT_SELECTOR": "d1"}


def test_build_run_env_without_d1_url_keeps_base() -> None:
    run_env = build_run_env({"PATH": "/usr/bin"}, "rule", None)

    assert run_env == {"PATH": "/usr/bin", "WAVEPILOT_SELECTOR": "rule"}


def test_build_page_url_has_scenario_and_autostart() -> None:
    assert build_page_url(8766, "two_red") == "http://127.0.0.1:8766/drone_camera.html?scenario=two_red&autostart=ai"


def test_default_run_timeout_is_max_flight_plus_margin(tmp_path: Path) -> None:
    options = make_options(tmp_path)

    timeout_sec = get_run_timeout_sec(options, load_compare_scenario("basic"))

    assert timeout_sec == 300.0


def test_explicit_run_timeout_wins(tmp_path: Path) -> None:
    options = make_options(tmp_path, run_timeout_sec=42.0)

    assert get_run_timeout_sec(options, load_compare_scenario("basic")) == 42.0


# ---------------------------------------------------------------- 인자


def test_default_arguments() -> None:
    options = run_compare.parse_compare_options(parse_args(), {"D1_SERVER_URL": D1_URL}, FakeClock())

    assert options.selectors == ("rule", "d1")
    assert options.repeats == 3
    assert options.sim_port == 8766
    assert options.headless is False
    assert options.d1_url == D1_URL
    assert options.run_timeout_sec is None
    assert tuple(scenario.scenario_id for scenario in options.scenarios) == list_scenario_ids()
    assert options.out_dir == Path("logs") / "compare" / "20261011-120000"


def test_scenarios_are_comma_separated() -> None:
    args = parse_args("--scenarios", "basic, two_red", "--selectors", "rule", "--out", "x")

    options = run_compare.parse_compare_options(args, {}, FakeClock())

    assert [scenario.scenario_id for scenario in options.scenarios] == ["basic", "two_red"]
    assert options.selectors == ("rule",)
    assert options.out_dir == Path("x")


def test_d1_url_flag_beats_env() -> None:
    args = parse_args("--d1-url", D1_URL)

    options = run_compare.parse_compare_options(args, {"D1_SERVER_URL": "http://127.0.0.1:1"}, FakeClock())

    assert options.d1_url == D1_URL


@pytest.mark.parametrize(
    "argv",
    [
        ["--selectors", "foo"],
        ["--repeats", "0"],
        ["--scenarios", "nope_missing"],
        ["--scenarios", "../basic"],
        ["--selectors", "rule,d1"],  # d1 URL 없음
        ["--run-timeout-sec", "0"],
    ],
)
def test_bad_arguments_exit_usage_without_out_folder(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("D1_SERVER_URL", raising=False)
    out_dir = tmp_path / "out"

    exit_code = run_compare.main([*argv, "--out", str(out_dir)])

    assert exit_code == 2
    assert not out_dir.exists()


# ---------------------------------------------------------------- 실행 루프 가짜 도구


MISSION_RECORDS = [
    {"t": 0.0, "kind": "mission", "event": "start", "selector": "rule"},
    {"t": 3.0, "kind": "selection", "mode": "approach", "source": "rule", "confidence": 1.0, "fallback": None},
    {"t": 9.0, "kind": "hit", "track": "T1", "track_id": 1, "actor": "red_1"},
    {"t": 40.0, "kind": "mission", "event": "end", "end_reason": "landed", "elapsed": 40.0,
     "return_reason": None, "forced_return": False},
]
NO_END_RECORDS = MISSION_RECORDS[:-1]


def write_mission_log(rundir: Path, records: list[dict]) -> Path:
    path = rundir / "20261011-120000.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


class FakeProcess:
    def __init__(self, exit_code: int | None = 0, wait_raises: BaseException | None = None) -> None:
        self.exit_code = exit_code
        self.wait_raises = wait_raises
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.wait_raises is not None and not self.killed:
            raise self.wait_raises
        return self.exit_code if not self.killed else -9

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


class FakeLauncher:
    """실행마다 rundir 에 JSONL 을 쓰고(records 가 None 이면 안 쓴다) 가짜 프로세스를 돌려준다."""

    def __init__(self, records: list[dict] | None = MISSION_RECORDS, make_process=FakeProcess) -> None:
        self.records = records
        self.make_process = make_process
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, command: list[str], env: dict[str, str], stderr_path: Path) -> FakeProcess:
        self.calls.append((command, env, stderr_path))
        if self.records is not None:
            write_mission_log(stderr_path.parent, self.records)
        process = self.make_process()
        self.processes.append(process)
        return process


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, open_error: Exception | None = None) -> None:
        self.open_error = open_error
        self.pages: list[FakePage] = []
        self.headless_values: list[bool] = []
        self.close_count = 0

    def __call__(self, headless: bool) -> "FakeBrowser":
        self.headless_values.append(headless)
        return self

    def open_page(self, url: str) -> FakePage:
        if self.open_error is not None:
            raise self.open_error
        page = FakePage(url)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.close_count += 1


def port_open(*_args) -> bool:
    return True


def port_closed(*_args) -> bool:
    return False


def make_run_deps(launcher: FakeLauncher, browser: FakeBrowser, wait_for_port=port_open) -> CompareDeps:
    return make_deps(launch_process=launcher, open_browser=browser, wait_for_port=wait_for_port)


def run_once(tmp_path: Path, launcher: FakeLauncher, browser: FakeBrowser, wait_for_port=port_open, selector: str = "rule"):
    options = make_options(tmp_path)
    deps = make_run_deps(launcher, browser, wait_for_port)
    scenario = load_compare_scenario("basic")

    return run_compare.execute_run(options, deps, browser, scenario, selector, 1)


# ---------------------------------------------------------------- 실행 1회


def test_completed_run_writes_truth_launches_and_evaluates(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    browser = FakeBrowser()
    options = make_options(tmp_path)
    scenario = load_compare_scenario("basic")
    rundir = tmp_path / "compare" / "basic__rule__r1"

    outcome = run_compare.execute_run(options, make_run_deps(launcher, browser), browser, scenario, "rule", 1)

    truth = json.loads((rundir / "truth.json").read_text(encoding="utf-8"))
    command, env, stderr_path = launcher.calls[0]
    assert truth == build_ground_truth(scenario)
    assert command == build_run_command(options, scenario, rundir)
    assert env == build_run_env(os.environ, "rule", D1_URL)
    assert stderr_path == rundir / "stderr.log"
    assert [page.url for page in browser.pages] == [build_page_url(8766, "basic")]
    assert browser.pages[0].closed
    assert launcher.processes[0].wait_timeouts == [300.0]
    assert (outcome.status, outcome.scenario_id, outcome.selector, outcome.repeat) == ("completed", "basic", "rule", 1)


def test_exit_code_one_with_end_record_is_completed(tmp_path: Path) -> None:
    launcher = FakeLauncher(make_process=lambda: FakeProcess(exit_code=1))

    outcome = run_once(tmp_path, launcher, FakeBrowser())

    assert outcome.status == "completed"


def test_timeout_kills_process_and_aborts(tmp_path: Path) -> None:
    timeout_error = subprocess.TimeoutExpired(cmd="run_mission", timeout=300.0)
    launcher = FakeLauncher(make_process=lambda: FakeProcess(wait_raises=timeout_error))
    browser = FakeBrowser()

    outcome = run_once(tmp_path, launcher, browser)

    assert launcher.processes[0].killed
    assert browser.pages[0].closed
    assert outcome.status == "aborted"
    assert "timeout" in outcome.abort_detail and "300" in outcome.abort_detail


def test_port_not_open_kills_without_page(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    browser = FakeBrowser()

    outcome = run_once(tmp_path, launcher, browser, wait_for_port=port_closed)

    assert launcher.processes[0].killed
    assert browser.pages == []
    assert outcome.status == "aborted"
    assert outcome.abort_detail.startswith("simulator server did not open")


def test_page_open_error_kills_and_aborts(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    browser = FakeBrowser(open_error=RuntimeError("net::ERR_CONNECTION_REFUSED"))

    outcome = run_once(tmp_path, launcher, browser)

    assert launcher.processes[0].killed
    assert outcome.status == "aborted"
    assert "net::ERR_CONNECTION_REFUSED" in outcome.abort_detail


def test_missing_jsonl_aborts(tmp_path: Path) -> None:
    launcher = FakeLauncher(records=None, make_process=lambda: FakeProcess(exit_code=1))

    outcome = run_once(tmp_path, launcher, FakeBrowser())

    assert outcome.status == "aborted"
    assert outcome.abort_detail == "no mission log"


def test_jsonl_without_end_aborts(tmp_path: Path) -> None:
    launcher = FakeLauncher(records=NO_END_RECORDS, make_process=lambda: FakeProcess(exit_code=1))

    outcome = run_once(tmp_path, launcher, FakeBrowser())

    assert outcome.status == "aborted"
    assert outcome.abort_detail == "mission log has no end"


@pytest.mark.parametrize("exit_code", [2, 130, -9])
def test_unexpected_exit_code_aborts(tmp_path: Path, exit_code: int) -> None:
    launcher = FakeLauncher(make_process=lambda: FakeProcess(exit_code=exit_code))

    outcome = run_once(tmp_path, launcher, FakeBrowser())

    assert outcome.status == "aborted"
    assert outcome.abort_detail == f"run_mission exit {exit_code}"


# ---------------------------------------------------------------- 비교 전체


def test_comparison_runs_in_plan_order_and_writes_summary_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_calls: list[list] = []
    real_write_summary = run_compare.write_summary

    def spy_write_summary(out_dir, outcomes):
        summary_calls.append(list(outcomes))
        return real_write_summary(out_dir, outcomes)

    monkeypatch.setattr(run_compare, "write_summary", spy_write_summary)
    options = make_options(tmp_path, repeats=2)
    launcher = FakeLauncher()
    browser = FakeBrowser()

    exit_code = run_compare.run_comparison(options, make_run_deps(launcher, browser))

    expected_runs = plan_runs(["basic", "no_red"], ["rule", "d1"], 2)
    run_dir_names = [stderr_path.parent.name for _, _, stderr_path in launcher.calls]
    assert exit_code == 0
    assert run_dir_names == [f"{scenario_id}__{selector}__r{repeat}" for repeat, scenario_id, selector in expected_runs]
    assert [env["WAVEPILOT_SELECTOR"] for _, env, _ in launcher.calls] == [selector for _, _, selector in expected_runs]
    assert {env["D1_SERVER_URL"] for _, env, _ in launcher.calls} == {D1_URL}
    assert [page.url for page in browser.pages] == [build_page_url(8766, scenario_id) for _, scenario_id, _ in expected_runs]
    assert browser.headless_values == [True]
    assert browser.close_count == 1
    assert len(summary_calls) == 1
    assert [(o.repeat, o.scenario_id, o.selector) for o in summary_calls[0]] == expected_runs
    assert (options.out_dir / "summary.md").is_file()


def test_aborted_run_does_not_stop_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary_calls: list[list] = []
    monkeypatch.setattr(run_compare, "write_summary", lambda out_dir, outcomes: summary_calls.append(list(outcomes)) or (None, None))
    options = make_options(tmp_path)
    launcher = FakeLauncher()
    port_results = iter([False, True, True, True])

    exit_code = run_compare.run_comparison(options, make_run_deps(launcher, FakeBrowser(), lambda *_: next(port_results)))

    assert exit_code == 0
    assert [outcome.status for outcome in summary_calls[0]] == ["aborted", "completed", "completed", "completed"]


def test_interrupt_writes_partial_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary_calls: list[list] = []
    monkeypatch.setattr(run_compare, "write_summary", lambda out_dir, outcomes: summary_calls.append(list(outcomes)) or (None, None))
    launcher = FakeLauncher(make_process=lambda: FakeProcess())
    process_count = iter(range(10))

    def interrupting_launcher(command, env, stderr_path):
        process = launcher(command, env, stderr_path)
        if next(process_count) == 1:
            process.wait_raises = KeyboardInterrupt()
        return process

    browser = FakeBrowser()
    deps = make_run_deps(interrupting_launcher, browser)

    exit_code = run_compare.run_comparison(make_options(tmp_path), deps)

    assert exit_code == 130
    assert len(summary_calls[0]) == 1
    assert launcher.processes[1].killed
    assert browser.close_count == 1


# ---------------------------------------------------------------- README


README_PYTHON_SECTION = "## Python: LLM overwatch pipeline"
README_FLAG_PATTERN = re.compile(r"(?<!\S)--[a-z][a-z0-9-]*")
PLAYWRIGHT_INSTALL_LINE = "uv run --group compare playwright install chromium"


def _read_readme_python_section(root: Path) -> str:
    readme_text = (root / "README.md").read_text(encoding="utf-8")
    start = readme_text.index(README_PYTHON_SECTION)
    end = readme_text.find("\n## ", start + len(README_PYTHON_SECTION))
    return readme_text[start:] if end < 0 else readme_text[start:end]


def test_readme_compare_flags_exist(wavepilot_root: Path) -> None:
    section = _read_readme_python_section(wavepilot_root).replace("\\\n", " ")
    known_flags = set(build_arg_parser()._option_string_actions)
    pyproject = tomllib.loads((wavepilot_root / "pyproject.toml").read_text(encoding="utf-8"))

    lines = [line for line in section.splitlines() if line.startswith("uv run") and "modules.drone.run_compare" in line]
    # `uv run --group compare` 의 uv 플래그는 빼고 모듈 이름 뒤 인자만 본다
    module_args = [line.split("modules.drone.run_compare", 1)[1] for line in lines]
    unknown_flags = sorted({flag for args in module_args for flag in README_FLAG_PATTERN.findall(args)} - known_flags)

    assert lines, "README has no `modules.drone.run_compare` command line"
    assert not unknown_flags, f"README run_compare flags not defined: {unknown_flags}"
    assert PLAYWRIGHT_INSTALL_LINE in section
    assert all("--group compare" in line for line in lines)
    assert "compare" in pyproject["dependency-groups"]
