"""드론 측 실행 진입점이자 유일한 컴포지션 루트 (RESEARCH Pattern 2, SC4).

mock·인식·전송 구현체를 고르고 인터페이스 뒤에 끼워 DronePipeline 에 주입하는 곳은 여기뿐이다.
llm_module 은 최상위 파사드(LLMModuleConfig, create_llm_module)만 쓴다.

실행 예시 (WavePilot 루트에서):
    python -m drone.run_scenario --scenario scenarios/checkpoint.toml --operator-port 8765 --backend fake --speed 0
    python -m drone.run_scenario --scenario scenarios/checkpoint.toml --operator-port 8765 --backend llama_server --llm-url http://127.0.0.1:8080 --speed 1

운용자 콘솔(python -m operator_console)을 먼저 띄워야 한다 — 운용자가 listen 하고 드론이 connect 하는 구조라서,
콘솔이 없으면 드론은 --connect-timeout-sec 동안 재시도한 뒤 종료 코드 1로 끝난다.

--speed 0 은 벽시계를 기다리지 않고 최대 속도로 돌리며 보고도 동기로 만든다 — 같은 시나리오는 항상 같은
텔레메트리·상태·보고를 낸다(결정론). --speed 1 은 실시간, 2 는 2배속이다.
"""

import argparse
import logging
import sys
import time

from comm.tcp_link import TcpJsonLinesLink
from drone.pipeline import (
    DEFAULT_REPORT_INTERVAL_SEC,
    DEFAULT_TELEMETRY_INTERVAL_SEC,
    DronePipeline,
    PipelineConfig,
)
from interfaces.geometry import DEFAULT_CAMERA
from llm_module import LLMBackendError, LLMModuleConfig, create_llm_module
from mocks.clock import SimClock
from mocks.detection_source import ScenarioDetectionSource
from mocks.flight_control import MockFlightControl
from mocks.scenario import load_scenario
from perception.tracking import TrackAggregator

logger = logging.getLogger("drone.run_scenario")

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
        prog="python -m drone.run_scenario",
        description="Run a scenario through mocks, perception and the LLM module, sending results to the operator console.",
    )
    parser.add_argument("--scenario", required=True, help="path to a scenario TOML file")
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
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG, INFO, WARNING, ...)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    logging.basicConfig(
        stream=sys.stderr, level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        scenario = load_scenario(args.scenario)
    except (FileNotFoundError, ValueError) as error:
        logger.error("invalid scenario %s: %s", args.scenario, error)
        return EXIT_USAGE

    clock = SimClock()
    flight_control = MockFlightControl(clock)
    detection_source = ScenarioDetectionSource(scenario, flight_control, DEFAULT_CAMERA, clock, fps=args.fps)
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
        run_reports_in_background=False,
    )
    pace = WallClockPacer(args.speed) if args.speed > 0 else None
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
        scenario.name, summary.frame_count, summary.telemetry_count, summary.state_count, summary.report_count,
        summary.skipped_report_count, summary.command_count, summary.rejected_command_count, summary.link_lost,
    )
    return EXIT_FAILURE if summary.link_lost else EXIT_OK


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # parser.error 는 종료 코드 2로 끝낸다
    if args.speed < 0:
        parser.error(f"--speed must be >= 0, got {args.speed}")
    if args.fps <= 0:
        parser.error(f"--fps must be > 0, got {args.fps}")
    if args.report_interval_sec <= 0:
        parser.error(f"--report-interval-sec must be > 0, got {args.report_interval_sec}")
    if args.telemetry_interval_sec <= 0:
        parser.error(f"--telemetry-interval-sec must be > 0, got {args.telemetry_interval_sec}")


if __name__ == "__main__":
    sys.exit(main())
