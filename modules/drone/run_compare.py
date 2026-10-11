"""선택기 비교 진입점 — 시나리오 × 선택기(rule, d1) × 반복을 브라우저 시뮬레이터로 끝까지 돌려 비교표를 만든다 (CMP-01).

실행 예시 (WavePilot 루트에서):
    uv run --group compare playwright install chromium        (처음 한 번)
    uv run --group compare python -m modules.drone.run_compare --repeats 1 \\
        --yolo-url http://<orin-ip>:8082 --d1-url http://<orin-ip>:8081
    uv run --group compare python -m modules.drone.run_compare --scenarios basic,confusion --selectors rule \\
        --headless --yolo-url http://<orin-ip>:8082

흐름: 서버 확인(d1·YOLO /health) → 실행 계획(반복 r → 시나리오 → 선택기 교차) → 실행마다 run_mission 하위 프로세스
(env WAVEPILOT_SELECTOR·D1_SERVER_URL) + 새 페이지 drone_camera.html?scenario=<id>&autostart=ai → 종료 대기(타임아웃이면 kill)
→ 실행 폴더의 JSONL 로 판정 → 끝에 summary.md·summary.csv.

결과 폴더 (기본 logs/compare/<YYYYmmdd-HHMMSS>/, --out 으로 바꾼다):
    <scenario>__<selector>__r<k>/   truth.json(정답) + run_mission JSONL + stderr.log
    summary.md, summary.csv         시나리오×선택기 행 + 선택기 합계 행

서버가 하나라도 응답하지 않으면 비교를 시작하지 않고 그 사실을 summary.md 에 남긴 뒤 종료 코드 1 이다 (D-14).

playwright 는 uv 그룹 compare 에만 있고, 이 파일의 함수 안에서만 import 한다 — 모듈 import·--help·단위 테스트는
playwright 없이 돈다. 하위 프로세스·브라우저·health·포트 대기·시계는 CompareDeps 로 주입한다.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from modules.decision.factory import D1_URL_ENV, SELECTOR_D1, SELECTOR_ENV, SELECTOR_RULE
from modules.decision.systemone_client import HEALTH_PATH as D1_HEALTH_PATH
from modules.drone.compare_report import (
    EVENT_END,
    KIND_MISSION_RECORD,
    RunOutcome,
    aborted_run,
    evaluate_run,
    read_jsonl,
    write_server_missing,
    write_summary,
)
from modules.drone.run_scenario import (
    DEFAULT_SIM_CONNECT_TIMEOUT_SEC,
    DEFAULT_SIM_PORT,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    SIM_PAGE_PATH,
)
from modules.drone.yolo_factory import DEFAULT_YOLO_URL
from modules.mission.mission_file import load_mission_file
from modules.simulation.compare_scenario import (
    CompareScenario,
    build_ground_truth,
    list_scenario_ids,
    load_compare_scenario,
)
from modules.yolo_detection.remote.protocol import HEALTH_PATH as YOLO_HEALTH_PATH

DEFAULT_REPEATS = 3
SELECTOR_CHOICES = (SELECTOR_RULE, SELECTOR_D1)
SERVER_D1 = "d1"
SERVER_YOLO = "yolo"
HEALTH_TIMEOUT_SEC = 3.0
RUN_TIMEOUT_MARGIN_SEC = 120.0  # 임무 max_flight_sec 위에 브리지 연결·AI 전환·착륙 여유
SIM_HOST = "127.0.0.1"
DEFAULT_COMPARE_DIR = Path("logs") / "compare"
OUT_DIR_TIME_FORMAT = "%Y%m%d-%H%M%S"
RUN_DIR_FORMAT = "{scenario}__{selector}__r{repeat}"
TRUTH_FILE = "truth.json"
STDERR_FILE = "stderr.log"
AUTOSTART_AI = "ai"
PORT_POLL_SEC = 0.2
PORT_CONNECT_TIMEOUT_SEC = 0.5
STOP_WAIT_SEC = 5.0
PLAYWRIGHT_INSTALL_COMMAND = "uv run --group compare playwright install chromium"
# 창이 가려지거나 포커스를 잃어도 렌더·타이머가 느려지지 않게 (헤드풀 기본)
CHROMIUM_KEEP_RUNNING_ARGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
# 헤드리스 Chromium 은 GPU 가 없어 WebGL 이 꺼질 수 있다 — 소프트웨어 렌더러를 켠다
CHROMIUM_HEADLESS_WEBGL_ARGS = ("--use-angle=swiftshader", "--enable-unsafe-swiftshader")

# 하위 프로세스 cwd — 시나리오의 임무 경로가 WavePilot 루트 기준 상대 경로다
WAVEPILOT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("modules.drone.run_compare")


# ---------------------------------------------------------------- 옵션·주입 계약


@dataclass(frozen=True, slots=True)
class CompareOptions:
    scenarios: tuple[CompareScenario, ...]
    selectors: tuple[str, ...]
    repeats: int
    out_dir: Path
    yolo_url: str
    d1_url: str | None
    sim_port: int
    headless: bool
    run_timeout_sec: float | None  # None 이면 실행마다 임무 파일 max_flight_sec + 여유
    sim_connect_timeout_sec: float = DEFAULT_SIM_CONNECT_TIMEOUT_SEC  # 시뮬레이터 포트가 열릴 때까지 대기 상한


class RunningProcess(Protocol):
    """subprocess.Popen 이 그대로 맞는 최소 모양."""

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


ProcessLauncher = Callable[[list[str], dict[str, str], Path], RunningProcess]  # (명령, env, stderr 로그 경로)


class PageHandle(Protocol):
    def close(self) -> None: ...


class BrowserSession(Protocol):
    def open_page(self, url: str) -> PageHandle: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CompareDeps:
    launch_process: ProcessLauncher
    open_browser: Callable[[bool], BrowserSession]  # headless 여부
    probe_health: Callable[[str, float], str | None]  # (url, timeout) → 오류 문자열, 정상이면 None
    wait_for_port: Callable[[str, int, float, RunningProcess], bool]
    now: Callable[[], datetime]


# ---------------------------------------------------------------- 인자


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare the rule and d1 selectors on recorded scenarios in the browser simulator")
    parser.add_argument("--scenarios", default=None, help="comma-separated scenario ids (default: every scenarios/*.toml)")
    parser.add_argument("--selectors", default=",".join(SELECTOR_CHOICES), help="comma-separated selectors: rule, d1 (default rule,d1)")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help=f"runs per scenario and selector (default {DEFAULT_REPEATS})")
    parser.add_argument(
        "--out",
        default=None,
        help="output folder, relative to the working directory (run from WavePilot root); default logs/compare/<start time>",
    )
    parser.add_argument("--yolo-url", default=DEFAULT_YOLO_URL, help=f"Orin YOLO server URL (default {DEFAULT_YOLO_URL})")
    parser.add_argument("--d1-url", default=None, help=f"d1 server URL (default: env {D1_URL_ENV})")
    parser.add_argument("--sim-port", type=int, default=DEFAULT_SIM_PORT, help=f"browser simulator server port (default {DEFAULT_SIM_PORT})")
    parser.add_argument("--headless", action="store_true", help="run Chromium headless (headed by default; WebGL may need a GPU)")
    parser.add_argument("--run-timeout-sec", type=float, default=None, help="per-run timeout in seconds (default: mission max_flight_sec + 120)")
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG, INFO, WARNING, ...)")
    return parser


def parse_compare_options(args: argparse.Namespace, env: Mapping[str, str], now: Callable[[], datetime]) -> CompareOptions:
    """CLI 인자 → CompareOptions. 잘못된 값은 ValueError, 없는 시나리오 파일은 FileNotFoundError 를 그대로 올린다."""
    selectors = _split_csv(args.selectors)
    unknown_selectors = [selector for selector in selectors if selector not in SELECTOR_CHOICES]
    if not selectors or unknown_selectors:
        raise ValueError(f"field 'selectors' must be a comma-separated subset of {SELECTOR_CHOICES}, got {args.selectors!r}")

    if args.repeats < 1:
        raise ValueError(f"field 'repeats' must be >= 1, got {args.repeats!r}")
    if args.run_timeout_sec is not None and not args.run_timeout_sec > 0:
        raise ValueError(f"field 'run_timeout_sec' must be > 0, got {args.run_timeout_sec!r}")

    scenario_ids = _split_csv(args.scenarios) if args.scenarios is not None else list(list_scenario_ids())
    if not scenario_ids:
        raise ValueError("no scenario to run (scenarios/ is empty or --scenarios is blank)")
    scenarios = tuple(load_compare_scenario(scenario_id) for scenario_id in scenario_ids)

    d1_url = (args.d1_url or env.get(D1_URL_ENV) or "").strip() or None
    if SELECTOR_D1 in selectors and d1_url is None:
        raise ValueError(f"--d1-url or env {D1_URL_ENV} is required when selectors include {SELECTOR_D1}")

    out_dir = Path(args.out) if args.out else DEFAULT_COMPARE_DIR / now().strftime(OUT_DIR_TIME_FORMAT)

    return CompareOptions(
        scenarios=scenarios,
        selectors=tuple(selectors),
        repeats=args.repeats,
        out_dir=out_dir,
        yolo_url=args.yolo_url,
        d1_url=d1_url,
        sim_port=args.sim_port,
        headless=args.headless,
        run_timeout_sec=args.run_timeout_sec,
    )


def _split_csv(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


# ---------------------------------------------------------------- 사전 단계 도우미


def check_servers(options: CompareOptions, probe_health: Callable[[str, float], str | None]) -> list[tuple[str, str, str]]:
    """응답하지 않는 서버 목록 [(이름, health URL, 오류)]. d1 은 선택기에 있을 때만, YOLO 는 늘 확인한다 (D-03, D-14)."""
    targets: list[tuple[str, str]] = []
    if SELECTOR_D1 in options.selectors and options.d1_url:
        targets.append((SERVER_D1, options.d1_url.rstrip("/") + D1_HEALTH_PATH))
    targets.append((SERVER_YOLO, options.yolo_url.rstrip("/") + YOLO_HEALTH_PATH))

    missing: list[tuple[str, str, str]] = []
    for server_name, url in targets:
        error = probe_health(url, HEALTH_TIMEOUT_SEC)
        if error is not None:
            missing.append((server_name, url, error))

    return missing


def plan_runs(scenario_ids: Sequence[str], selectors: Sequence[str], repeats: int) -> list[tuple[int, str, str]]:
    """(반복, 시나리오, 선택기) 실행 순서 — 반복 r → 시나리오 → 선택기 교차라 시간대 편향이 두 선택기에 고르게 간다."""
    return [
        (repeat, scenario_id, selector)
        for repeat in range(1, repeats + 1)
        for scenario_id in scenario_ids
        for selector in selectors
    ]


def get_run_dir(out_dir: Path, scenario_id: str, selector: str, repeat: int) -> Path:
    return out_dir / RUN_DIR_FORMAT.format(scenario=scenario_id, selector=selector, repeat=repeat)


def get_run_timeout_sec(options: CompareOptions, scenario: CompareScenario) -> float:
    if options.run_timeout_sec is not None:
        return options.run_timeout_sec

    mission = load_mission_file(WAVEPILOT_ROOT / scenario.mission_path)
    return mission.max_flight_sec + RUN_TIMEOUT_MARGIN_SEC


def build_run_command(options: CompareOptions, scenario: CompareScenario, rundir: Path) -> list[str]:
    # 리스트 그대로 shell=False 로 띄운다 — 시나리오 id·경로가 셸을 거치지 않는다
    return [
        sys.executable, "-m", "modules.drone.run_mission",
        "--sim", "browser",
        "--yolo", "orin",
        "--yolo-url", options.yolo_url,
        "--mission", scenario.mission_path,
        "--log-dir", str(rundir),
        "--sim-port", str(options.sim_port),
    ]


def build_run_env(base_env: Mapping[str, str], selector: str, d1_url: str | None) -> dict[str, str]:
    run_env = dict(base_env)
    run_env[SELECTOR_ENV] = selector
    if d1_url:
        run_env[D1_URL_ENV] = d1_url
    return run_env


def build_page_url(port: int, scenario_id: str) -> str:
    query = urllib.parse.urlencode({"scenario": scenario_id, "autostart": AUTOSTART_AI})
    return f"http://{SIM_HOST}:{port}{SIM_PAGE_PATH}?{query}"


# ---------------------------------------------------------------- 비교 실행


def run_comparison(options: CompareOptions, deps: CompareDeps) -> int:
    options.out_dir.mkdir(parents=True, exist_ok=True)

    missing = check_servers(options, deps.probe_health)
    if missing:
        for server_name, url, error in missing:
            logger.error("%s server not reachable at %s: %s — comparison not started", server_name, url, error)
        md_path, _ = write_server_missing(options.out_dir, missing, deps.now().isoformat(timespec="seconds"))
        logger.error("server check result: %s", md_path)
        return EXIT_FAILURE

    outcomes: list[RunOutcome] = []
    try:
        _run_all(options, deps, outcomes)
    except KeyboardInterrupt:
        # 긴 비교를 중간에 끊어도 그때까지의 표는 남긴다
        md_path, _ = write_summary(options.out_dir, outcomes)
        logger.warning("interrupted after %d runs, partial summary: %s", len(outcomes), md_path)
        return EXIT_INTERRUPTED

    md_path, _ = write_summary(options.out_dir, outcomes)
    logger.info("comparison summary: %s", md_path)
    return EXIT_OK


def _run_all(options: CompareOptions, deps: CompareDeps, outcomes: list[RunOutcome]) -> None:
    """계획한 실행을 차례로 돌려 outcomes 에 쌓는다 — 중간에 끊겨도 호출 쪽이 쌓인 만큼 표를 쓴다."""
    scenario_by_id = {scenario.scenario_id: scenario for scenario in options.scenarios}
    run_list = plan_runs(list(scenario_by_id), options.selectors, options.repeats)

    browser = deps.open_browser(options.headless)
    try:
        for repeat, scenario_id, selector in run_list:
            outcome = execute_run(options, deps, browser, scenario_by_id[scenario_id], selector, repeat)
            outcomes.append(outcome)
            logger.info(
                "run %s/%s r%d: %s success=%s%s",
                scenario_id,
                selector,
                repeat,
                outcome.status,
                outcome.is_success,
                f" ({outcome.abort_detail})" if outcome.abort_detail else "",
            )
    finally:
        browser.close()


def execute_run(
    options: CompareOptions,
    deps: CompareDeps,
    browser: BrowserSession,
    scenario: CompareScenario,
    selector: str,
    repeat: int,
) -> RunOutcome:
    """실행 1회 = run_mission 하위 프로세스 1개 + 새 페이지 1개. 끝까지 못 가면 aborted 로 돌려준다."""
    scenario_id = scenario.scenario_id
    rundir = get_run_dir(options.out_dir, scenario_id, selector, repeat)
    rundir.mkdir(parents=True, exist_ok=True)
    truth = build_ground_truth(scenario)
    (rundir / TRUTH_FILE).write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    timeout_sec = get_run_timeout_sec(options, scenario)

    process = deps.launch_process(
        build_run_command(options, scenario, rundir),
        build_run_env(os.environ, selector, options.d1_url),
        rundir / STDERR_FILE,
    )
    try:
        drive_result = _drive_run(options, deps, browser, process, scenario_id, selector, repeat, timeout_sec)
    except KeyboardInterrupt:
        _stop_process(process)
        raise

    if isinstance(drive_result, RunOutcome):
        return drive_result
    return _judge_run(truth, rundir, drive_result, scenario_id, selector, repeat)


def _drive_run(
    options: CompareOptions,
    deps: CompareDeps,
    browser: BrowserSession,
    process: RunningProcess,
    scenario_id: str,
    selector: str,
    repeat: int,
    timeout_sec: float,
) -> int | RunOutcome:
    """포트 대기 → 페이지 열기 → 종료 대기. 프로세스 종료 코드, 또는 끝까지 못 가면 aborted RunOutcome."""
    if not deps.wait_for_port(SIM_HOST, options.sim_port, options.sim_connect_timeout_sec, process):
        _stop_process(process)
        detail = f"simulator server did not open {SIM_HOST}:{options.sim_port} within {options.sim_connect_timeout_sec:g}s"
        return aborted_run(scenario_id, selector, repeat, detail)

    try:
        page = browser.open_page(build_page_url(options.sim_port, scenario_id))
    except Exception as error:
        _stop_process(process)
        return aborted_run(scenario_id, selector, repeat, f"page open failed: {error}")

    try:
        return process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        return aborted_run(scenario_id, selector, repeat, f"timeout after {timeout_sec:g}s")
    finally:
        _close_page(page)


def _judge_run(truth: dict, rundir: Path, exit_code: int, scenario_id: str, selector: str, repeat: int) -> RunOutcome:
    # 0(착지)·1(그 밖의 임무 종료)만 임무가 끝까지 간 것이다 — 2(사용법)·130(중단) 등은 판정하지 않는다
    if exit_code not in (EXIT_OK, EXIT_FAILURE):
        return aborted_run(scenario_id, selector, repeat, f"run_mission exit {exit_code}")

    jsonl_list = sorted(rundir.glob("*.jsonl"))
    if not jsonl_list:
        return aborted_run(scenario_id, selector, repeat, "no mission log")

    records = read_jsonl(jsonl_list[-1])
    has_end = any(record.get("kind") == KIND_MISSION_RECORD and record.get("event") == EVENT_END for record in records)
    if not has_end:
        return aborted_run(scenario_id, selector, repeat, "mission log has no end")

    return evaluate_run(truth, records, scenario_id, selector, repeat)


def _stop_process(process: RunningProcess) -> None:
    # 아직 돌고 있으면 죽이고 거둔다 — 다음 실행이 같은 포트를 쓰고, 좀비를 남기지 않게
    if process.poll() is not None:
        return

    process.kill()
    try:
        process.wait(timeout=STOP_WAIT_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("run_mission did not exit %gs after kill", STOP_WAIT_SEC)


def _close_page(page: PageHandle) -> None:
    try:
        page.close()
    except Exception as error:
        logger.warning("page close failed: %s", error)


# ---------------------------------------------------------------- 기본 구현 (테스트는 가짜로 바꾼다)


def probe_http_health(url: str, timeout_sec: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            status = response.status
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return str(error)

    if not 200 <= status < 300:
        return f"HTTP {status}"
    return None


def launch_run_process(command: list[str], env: dict[str, str], stderr_path: Path) -> subprocess.Popen:
    # run_mission 은 로그를 stderr 로 쓴다 — stdout 도 같은 파일로 모은다. 부모 쪽 핸들은 Popen 뒤 바로 닫는다(자식은 상속본 유지)
    with stderr_path.open("w", encoding="utf-8") as log_file:
        return subprocess.Popen(
            command,
            env=env,
            cwd=WAVEPILOT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=False,
        )


def wait_for_sim_port(host: str, port: int, timeout_sec: float, process: RunningProcess) -> bool:
    """시뮬레이터 서버 포트가 열리면 True. 마감이 지나거나 프로세스가 먼저 끝나면 False."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False

        try:
            with socket.create_connection((host, port), timeout=PORT_CONNECT_TIMEOUT_SEC):
                return True
        except OSError:
            time.sleep(PORT_POLL_SEC)

    return False


class PlaywrightMissingError(RuntimeError):
    """compare 그룹 없이 실행했다 — 사용법 오류로 끝낸다."""


class PlaywrightBrowserSession:
    """Chromium 하나를 비교 전체에 쓰고 실행마다 새 페이지를 연다. playwright 타입은 모듈 수준에서 참조하지 않는다."""

    def __init__(self, playwright, browser) -> None:
        self._playwright = playwright
        self._browser = browser

    def open_page(self, url: str) -> PageHandle:
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="load")
        except Exception:
            page.close()
            raise
        return page

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()


def open_playwright_browser(headless: bool) -> BrowserSession:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise PlaywrightMissingError(f"playwright is not installed: run `{PLAYWRIGHT_INSTALL_COMMAND}`") from error

    launch_args = list(CHROMIUM_KEEP_RUNNING_ARGS)
    if headless:
        launch_args.extend(CHROMIUM_HEADLESS_WEBGL_ARGS)

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=headless, args=launch_args)
    except Exception:
        playwright.stop()
        raise

    return PlaywrightBrowserSession(playwright, browser)


def create_default_deps() -> CompareDeps:
    return CompareDeps(
        launch_process=launch_run_process,
        open_browser=open_playwright_browser,
        probe_health=probe_http_health,
        wait_for_port=wait_for_sim_port,
        now=datetime.now,
    )


# ---------------------------------------------------------------- 진입점


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        options = parse_compare_options(args, os.environ, datetime.now)
    except (ValueError, FileNotFoundError) as error:
        logger.error("%s", error)
        return EXIT_USAGE

    try:
        return run_comparison(options, create_default_deps())
    except PlaywrightMissingError as error:
        logger.error("%s", error)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
