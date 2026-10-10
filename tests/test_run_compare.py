"""run_compare 비교 진입점 — 가짜 프로세스·브라우저·health·시계로 실제 Playwright·Orin·run_mission 없이 검사한다."""

import argparse
import sys
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
from modules.simulation.compare_scenario import list_scenario_ids, load_compare_scenario

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
