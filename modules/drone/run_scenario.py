"""드론 측 실행 진입점이자 유일한 컴포지션 루트 (RESEARCH Pattern 2, SC4).

mock·인식·전송 구현체를 고르고 인터페이스 뒤에 끼워 DronePipeline 에 주입하는 곳은 여기뿐이다.
llm_module 은 최상위 파사드(LLMModuleConfig, create_llm_module)만 쓴다.

실행 예시 (WavePilot 루트에서):
    python -m modules.drone.run_scenario --scenario modules/simulation/scenarios/checkpoint.toml --operator-port 8765 --backend fake --speed 0
    python -m modules.drone.run_scenario --scenario modules/simulation/scenarios/checkpoint.toml --operator-port 8765 --backend llama_server --llm-url http://127.0.0.1:8080 --speed 1

운용자 콘솔(python -m operator_console)을 먼저 띄워야 한다 — 운용자가 listen 하고 드론이 connect 하는 구조라서,
콘솔이 없으면 드론은 --connect-timeout-sec 동안 재시도한 뒤 종료 코드 1로 끝난다.

--speed 0 은 벽시계를 기다리지 않고 최대 속도로 돌리며 보고도 동기로 만든다 — 같은 시나리오는 항상 같은
텔레메트리·상태·보고를 낸다(결정론). --speed 1 은 실시간, 2 는 2배속이다.

--sim browser 는 같은 DronePipeline 을 브라우저 시뮬레이터(BrowserFlightControl + BrowserDetectionSource)에 붙인다 (SC4, X-11).
시뮬레이터 서버를 띄운 뒤 페이지가 연결되기를 기다리고, 프레임은 yolo 그룹의 실제 YOLO 로 분석한다:
    uv run --group yolo python -m modules.drone.run_scenario --sim browser --operator-port 8765 --backend fake
    브라우저로 http://127.0.0.1:8766/drone_camera.html 을 연다
"""

import argparse
import functools
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from modules.comm.tcp_link import TcpJsonLinesLink
from modules.drone.pipeline import (
    DEFAULT_REPORT_INTERVAL_SEC,
    DEFAULT_TELEMETRY_INTERVAL_SEC,
    DronePipeline,
    PipelineConfig,
)
from modules.perception.interface.geometry import DEFAULT_CAMERA
from modules.llm_report import LLMBackendError, LLMModuleConfig, create_llm_module
from modules.simulation.clock import SimClock
from modules.simulation.detection_source import ScenarioDetectionSource
from modules.flight_control.mock import MockFlightControl
from modules.simulation.scenario import load_scenario
from modules.perception.tracking import TrackAggregator
from modules.yolo_detection.interface import DetectionSource
from modules.flight_control.interface import FlightControl

logger = logging.getLogger("modules.drone.run_scenario")

DEFAULT_OPERATOR_HOST = "127.0.0.1"
DEFAULT_LLM_URL = "http://127.0.0.1:8080"
DEFAULT_LLM_TIMEOUT_SEC = 60.0  # 01-04 파사드 기본값과 같다
DEFAULT_LLM_READY_TIMEOUT_SEC = 120.0  # 모델 로딩이 수십 초 걸린다
DEFAULT_FPS = 10.0  # SKELETON FPS
DEFAULT_SPEED = 1.0
DEFAULT_CONNECT_TIMEOUT_SEC = 10.0
REPORT_DRAIN_MARGIN_SEC = 30.0  # 마지막 보고 요청이 타임아웃까지 가도 기다려 줄 여유
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

SIM_MOCK = "mock"
SIM_BROWSER = "browser"
SIM_CHOICES = (SIM_MOCK, SIM_BROWSER)
DEFAULT_SIM_HOST = "127.0.0.1"
# README 예시는 운용자 콘솔을 8765 에 띄우고, 시뮬레이터 서버 단독 실행 기본값도 8765 다 — 함께 띄울 때 충돌하지 않게 피한다
DEFAULT_SIM_PORT = 8766
DEFAULT_SIM_CONNECT_TIMEOUT_SEC = 30.0
SIM_CONNECT_POLL_SEC = 0.1
DEFAULT_YOLO_DEVICE = "auto"
SIM_PAGE_PATH = "/drone_camera.html"
BROWSER_RUN_LABEL = "browser"


class WallClockPacer:
    """sim 시각을 벽시계에 맞춘다. 첫 호출 시각을 기준으로 (sim - 기준 sim) / speed 만큼 지나기를 기다린다."""

    def __init__(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be > 0, got {speed}")

        self._speed = speed
        self._start_wall_sec: float | None = None
        self._start_sim_sec = 0.0

    def __call__(self, sim_time_sec: float) -> None:
        if self._start_wall_sec is None:
            self._start_wall_sec = time.monotonic()
            self._start_sim_sec = sim_time_sec
            return

        target_wall_sec = self._start_wall_sec + (sim_time_sec - self._start_sim_sec) / self._speed
        remaining_sec = target_wall_sec - time.monotonic()
        if remaining_sec > 0:
            time.sleep(remaining_sec)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.drone.run_scenario",
        description="Run a scenario through the simulation, perception and the LLM module, sending results to the operator console.",
    )
    parser.add_argument("--sim", choices=SIM_CHOICES, default=SIM_MOCK, help="simulator implementation (default mock)")
    parser.add_argument("--scenario", default=None, help="path to a scenario TOML file (required with --sim mock)")
    parser.add_argument("--operator-host", default=DEFAULT_OPERATOR_HOST, help="operator console host")
    parser.add_argument("--operator-port", type=int, required=True, help="operator console port")
    parser.add_argument("--backend", choices=["fake", "llama_server"], default="fake", help="report LLM backend")
    parser.add_argument("--llm-url", default=DEFAULT_LLM_URL, help="llama-server base URL")
    parser.add_argument("--llm-timeout-sec", type=float, default=DEFAULT_LLM_TIMEOUT_SEC, help="per-request LLM timeout")
    parser.add_argument(
        "--llm-ready-timeout-sec", type=float, default=DEFAULT_LLM_READY_TIMEOUT_SEC,
        help="how long to wait for the LLM backend to become ready",
    )
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="detection frames per sim second")
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help="sim seconds per wall second; 0 = as fast as possible (deterministic)",
    )
    parser.add_argument(
        "--report-interval-sec", type=float, default=DEFAULT_REPORT_INTERVAL_SEC,
        help="sim seconds between state/report messages",
    )
    parser.add_argument(
        "--telemetry-interval-sec", type=float, default=DEFAULT_TELEMETRY_INTERVAL_SEC,
        help="sim seconds between telemetry messages",
    )
    parser.add_argument(
        "--connect-timeout-sec", type=float, default=DEFAULT_CONNECT_TIMEOUT_SEC,
        help="how long to retry connecting to the operator console",
    )
    parser.add_argument("--sim-host", default=DEFAULT_SIM_HOST, help="browser simulator server host (--sim browser)")
    parser.add_argument(
        "--sim-port", type=int, default=DEFAULT_SIM_PORT, help=f"browser simulator server port (default {DEFAULT_SIM_PORT})"
    )
    parser.add_argument(
        "--sim-connect-timeout-sec", type=float, default=DEFAULT_SIM_CONNECT_TIMEOUT_SEC,
        help="how long to wait for the simulator page to connect (--sim browser)",
    )
    parser.add_argument(
        "--duration-sec", type=float, default=None,
        help="stop the browser run after this many seconds (default: until Ctrl-C or the page stops)",
    )
    parser.add_argument("--device", default=DEFAULT_YOLO_DEVICE, help="YOLO device for --sim browser: auto | mps | cpu | 0")
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG, INFO, WARNING, ...)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    logging.basicConfig(
        stream=sys.stderr, level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.sim == SIM_BROWSER:
        return _run_browser_sim(args)

    try:
        scenario = load_scenario(args.scenario)
    except (FileNotFoundError, ValueError) as error:
        logger.error("invalid scenario %s: %s", args.scenario, error)
        return EXIT_USAGE

    clock = SimClock()
    flight_control = MockFlightControl(clock)
    detection_source = ScenarioDetectionSource(scenario, flight_control, DEFAULT_CAMERA, clock, fps=args.fps)

    # --speed 0 은 보고를 동기로 만들어 결정론을 지킨다(SKELETON 시계 행). 실시간이면 보고만 워커로 돌려
    # llama_server 가 느려도 프레임·텔레메트리 송신이 멈추지 않게 한다 (RESEARCH Pitfall 6)
    pace = WallClockPacer(args.speed) if args.speed > 0 else None
    return _run_pipeline(
        args, detection_source, flight_control, pace=pace, run_reports_in_background=args.speed > 0, label=scenario.name
    )


def _run_pipeline(
    args: argparse.Namespace,
    detection_source: DetectionSource,
    flight_control: FlightControl,
    pace: Callable[[float], None] | None,
    run_reports_in_background: bool,
    label: str,
) -> int:
    """mock·browser 공통 — LLM·운용자 링크를 붙여 DronePipeline 을 끝까지 돌린다. pipeline 자체는 구현을 모른다 (X-11)."""
    track_provider = TrackAggregator(DEFAULT_CAMERA)

    llm_module = create_llm_module(
        LLMModuleConfig(backend=args.backend, backend_url=args.llm_url, request_timeout_sec=args.llm_timeout_sec)
    )
    try:
        llm_module.wait_until_ready(args.llm_ready_timeout_sec)
    except LLMBackendError as error:
        logger.error("llm backend not ready: %s", error)
        return EXIT_FAILURE

    try:
        link = TcpJsonLinesLink.connect(
            args.operator_host, args.operator_port, retry_timeout_sec=args.connect_timeout_sec
        )
    except OSError as error:
        logger.error("could not connect to operator console: %s", error)
        return EXIT_FAILURE

    config = PipelineConfig(
        report_interval_sec=args.report_interval_sec,
        telemetry_interval_sec=args.telemetry_interval_sec,
        run_reports_in_background=run_reports_in_background,
        report_drain_timeout_sec=args.llm_timeout_sec + REPORT_DRAIN_MARGIN_SEC,
    )
    pipeline = DronePipeline(
        detection_source, flight_control, track_provider, llm_module, link, config=config, pace=pace
    )

    # DronePipeline 이 끝에서 link.close() 하므로 운용자 콘솔이 "LINK disconnected" 를 출력한다
    try:
        summary = pipeline.run()
    except KeyboardInterrupt:
        link.close()
        logger.info("interrupted")
        return EXIT_INTERRUPTED

    logger.info(
        "scenario finished: scenario=%s frames=%d telemetry=%d states=%d reports=%d skipped_reports=%d "
        "commands=%d rejected_commands=%d link_lost=%s",
        label, summary.frame_count, summary.telemetry_count, summary.state_count, summary.report_count,
        summary.skipped_report_count, summary.command_count, summary.rejected_command_count, summary.link_lost,
    )
    return EXIT_FAILURE if summary.link_lost else EXIT_OK


@dataclass(frozen=True, slots=True)
class BrowserComponents:
    sim_bridge: Callable
    start_sim_server: Callable
    browser_flight_control: Callable
    browser_detection_source: Callable
    create_jpeg_yolo_source: Callable


def _load_browser_components() -> BrowserComponents:
    # browser 모드에서만 import 한다 — YOLO 어댑터는 무겁고, mock 실행은 브라우저 구현 없이도 돌아야 한다
    from modules.simulation.browser.bridge import SimBridge
    from modules.simulation.browser.detection_source import BrowserDetectionSource
    from modules.simulation.browser.flight_control import BrowserFlightControl
    from modules.simulation.browser.server import start_sim_server
    from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source

    return BrowserComponents(
        sim_bridge=SimBridge,
        start_sim_server=start_sim_server,
        browser_flight_control=BrowserFlightControl,
        browser_detection_source=BrowserDetectionSource,
        create_jpeg_yolo_source=create_jpeg_yolo_source,
    )


def _run_browser_sim(args: argparse.Namespace) -> int:
    """브라우저가 벽시계로 물리를 돌리고 Python 은 따라간다(D-10) — 프레임이 실시간으로 오므로 pace 는 None 이다."""
    components = _load_browser_components()
    bridge = components.sim_bridge()
    server = components.start_sim_server(bridge, args.sim_host, args.sim_port)
    logger.info("simulator page: http://%s:%d%s", args.sim_host, args.sim_port, SIM_PAGE_PATH)

    detection_source = None
    try:
        if not _wait_for_simulator(bridge, args.sim_connect_timeout_sec):
            logger.error("simulator not connected within %.1fs", args.sim_connect_timeout_sec)
            return EXIT_FAILURE

        flight_control = components.browser_flight_control(bridge)
        yolo_factory = functools.partial(components.create_jpeg_yolo_source, device=args.device)
        detection_source = components.browser_detection_source(bridge, yolo_factory=yolo_factory)

        if args.duration_sec:
            # 소스를 닫으면 read_frame 이 None 을 내고 파이프라인이 정상 종료 경로로 끝난다
            timer = threading.Timer(args.duration_sec, detection_source.close)
            timer.daemon = True
            timer.start()

        return _run_pipeline(
            args, detection_source, flight_control, pace=None, run_reports_in_background=True, label=BROWSER_RUN_LABEL
        )
    except KeyboardInterrupt:
        logger.info("interrupted")
        return EXIT_INTERRUPTED
    finally:
        if detection_source is not None:
            detection_source.close()
        server.shutdown()
        server.server_close()


def _wait_for_simulator(bridge, timeout_sec: float) -> bool:
    deadline_sec = time.monotonic() + timeout_sec

    while not bridge.is_connected():
        if time.monotonic() >= deadline_sec:
            return False
        time.sleep(SIM_CONNECT_POLL_SEC)
    return True


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # parser.error 는 종료 코드 2로 끝낸다
    if args.sim == SIM_MOCK and args.scenario is None:
        parser.error("--scenario is required with --sim mock")
    if args.speed < 0:
        parser.error(f"--speed must be >= 0, got {args.speed}")
    if args.fps <= 0:
        parser.error(f"--fps must be > 0, got {args.fps}")
    if args.report_interval_sec <= 0:
        parser.error(f"--report-interval-sec must be > 0, got {args.report_interval_sec}")
    if args.telemetry_interval_sec <= 0:
        parser.error(f"--telemetry-interval-sec must be > 0, got {args.telemetry_interval_sec}")
    if args.sim_connect_timeout_sec <= 0:
        parser.error(f"--sim-connect-timeout-sec must be > 0, got {args.sim_connect_timeout_sec}")
    if args.duration_sec is not None and args.duration_sec <= 0:
        parser.error(f"--duration-sec must be > 0, got {args.duration_sec}")


if __name__ == "__main__":
    sys.exit(main())
