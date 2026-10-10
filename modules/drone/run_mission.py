"""임무 실행 진입점 — 규칙 선택기가 고른 rule_mode 로 이륙부터 착륙까지 한 번에 수행한다 (D-13, D-14, D-16).

pipeline.py / run_scenario.py 는 건드리지 않는 별도 컴포지션 루트다. 선택기는 WAVEPILOT_SELECTOR 로 고른다
(rule 기본, d1 이면 D1_SERVER_URL 필요 — SelectorConfig.from_env + create_decision_model).

실행 예시 (WavePilot 루트에서):
    uv run python -m modules.drone.run_mission --sim mock
    uv run --group yolo python -m modules.drone.run_mission --sim browser
    (browser: http://127.0.0.1:8766/drone_camera.html 을 열고 AI 모드로 전환한다)

--sim mock 은 헤드리스다. KinematicFlightControl 과 스크립트 대상(person 1명)으로 YOLO 없이 돈다.
--speed 0 은 벽시계를 기다리지 않고 최대 속도로 돈다. 로그(stderr)에 선택마다 selection 줄,
모드 종료마다 mode_end 줄이 나온다. return_home 이 착지하면 종료 코드 0, 그 밖의 종료는 1 이다.
"""

import argparse
import dataclasses
import functools
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence

from modules.decision.factory import SelectorConfig, create_decision_model
from modules.decision.interface import DecisionModel
from modules.decision.params import ParamDefaults
from modules.drone.mission_loop import (
    MissionConfig,
    MissionFrameSource,
    MissionRunner,
    MissionRunResult,
    build_rule_mode_settings,
)
from modules.drone.run_scenario import (
    DEFAULT_SIM_CONNECT_TIMEOUT_SEC,
    DEFAULT_SIM_HOST,
    DEFAULT_SIM_PORT,
    DEFAULT_YOLO_DEVICE,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    SIM_BROWSER,
    SIM_CHOICES,
    SIM_MOCK,
    SIM_PAGE_PATH,
    BrowserComponents,
    WallClockPacer,
)
from modules.flight_control.interface import FlightControl, Telemetry
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.interface import RuleModeSettings
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource
from modules.yolo_detection.interface import Frame

logger = logging.getLogger("modules.drone.run_mission")

DEFAULT_MAX_MISSION_SEC = 300.0
DEFAULT_TICK_HZ = 10.0
DEFAULT_AI_MODE_TIMEOUT_SEC = 120.0
SIM_CONNECT_POLL_SEC = 0.1
AI_MODE_POLL_SEC = 0.5

# mock 세계 — 홈 북쪽 수색 구역 안에 사람 1명. 시야 안에 들어와야 보이도록 탐지 거리를 제한한다
DEFAULT_MOCK_TARGETS = (ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0),)
MOCK_DETECTION_RANGE_M = 40.0


class RangeLimitedTargetSource:
    """ScriptedTargetSource 의 탐지를 드론에서 max_range_m 안의 대상으로 제한한다 (YOLO 의 탐지 거리 대역)."""

    def __init__(self, source: ScriptedTargetSource, flight_control: FlightControl, max_range_m: float) -> None:
        if not math.isfinite(max_range_m) or max_range_m <= 0:
            raise ValueError(f"max_range_m must be a finite number > 0, got {max_range_m!r}")

        self._source = source
        self._flight_control = flight_control
        self._max_range_m = max_range_m

    def read_frame(self) -> Frame:
        frame = self._source.read_frame()
        telemetry = self._flight_control.get_telemetry()
        position_by_track_id = {target.track_id: target for target in self._source.targets}

        in_range_detections = tuple(
            detection
            for detection in frame.detections
            if self._is_in_range(position_by_track_id.get(detection.track_id), telemetry)
        )

        return dataclasses.replace(frame, detections=in_range_detections)

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None:
        return self._source.telemetry_for(timestamp_sec)

    def _is_in_range(self, target: ScriptedTarget | None, telemetry: Telemetry) -> bool:
        if target is None:
            return False
        feet_north_m, feet_east_m, _ = target.get_position_ned(telemetry.time_sec)

        return math.hypot(feet_north_m - telemetry.north_m, feet_east_m - telemetry.east_m) <= self._max_range_m


class LatestFrameReader:
    """막히는 read_frame() 을 데몬 스레드로 돌려, 아직 안 본 최신 프레임 하나만 논블로킹으로 내준다."""

    def __init__(self, source) -> None:
        self._source = source
        self._lock = threading.Lock()
        self._latest_frame: Frame | None = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="latest-frame-reader", daemon=True)
        self._thread.start()

    def read_frame(self) -> Frame | None:
        with self._lock:
            frame, self._latest_frame = self._latest_frame, None

        return frame

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None:
        return self._source.telemetry_for(timestamp_sec)

    def close(self) -> None:
        self._stop_event.set()

    def _pump(self) -> None:
        while not self._stop_event.is_set():
            frame = self._source.read_frame()
            if frame is None:
                return
            with self._lock:
                self._latest_frame = frame


class WallTickSleeper:
    """호출될 때마다 monotonic 기준 다음 1/tick_hz 마감까지 기다린다."""

    def __init__(self, tick_hz: float) -> None:
        self._tick_sec = 1.0 / tick_hz
        self._next_deadline_sec: float | None = None

    def __call__(self, dt_sec: float) -> None:
        now_sec = time.monotonic()
        if self._next_deadline_sec is None:
            self._next_deadline_sec = now_sec

        self._next_deadline_sec += self._tick_sec
        remaining_sec = self._next_deadline_sec - now_sec
        if remaining_sec > 0:
            time.sleep(remaining_sec)
        else:
            self._next_deadline_sec = now_sec  # 밀렸으면 따라잡으려 몰아치지 않는다


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.drone.run_mission",
        description="Run a full rule_mode mission (take off, selector-driven modes, return home, land).",
    )
    parser.add_argument("--sim", choices=SIM_CHOICES, default=SIM_MOCK, help="simulator implementation (default mock)")
    parser.add_argument(
        "--max-mission-sec", type=float, default=DEFAULT_MAX_MISSION_SEC,
        help="sim seconds after which return_home is forced",
    )
    parser.add_argument("--tick-hz", type=float, default=DEFAULT_TICK_HZ, help="mission loop ticks per sim second")
    parser.add_argument(
        "--speed", type=float, default=0.0,
        help="mock only: sim seconds per wall second; 0 = as fast as possible",
    )
    parser.add_argument("--sim-host", default=DEFAULT_SIM_HOST, help="browser simulator server host (--sim browser)")
    parser.add_argument("--sim-port", type=int, default=DEFAULT_SIM_PORT, help="browser simulator server port")
    parser.add_argument(
        "--sim-connect-timeout-sec", type=float, default=DEFAULT_SIM_CONNECT_TIMEOUT_SEC,
        help="how long to wait for the simulator page to connect (--sim browser)",
    )
    parser.add_argument(
        "--ai-mode-timeout-sec", type=float, default=DEFAULT_AI_MODE_TIMEOUT_SEC,
        help="how long to wait for AI mode to be switched on in the page (--sim browser)",
    )
    parser.add_argument("--device", default=DEFAULT_YOLO_DEVICE, help="YOLO device for --sim browser: auto | mps | cpu | 0")
    parser.add_argument("--log-level", default="INFO", help="logging level (DEBUG, INFO, WARNING, ...)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    logging.basicConfig(
        stream=sys.stderr, level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        config = SelectorConfig.from_env(os.environ)
        decision_model = create_decision_model(config)
    except ValueError as error:
        logger.error("invalid selector configuration: %s", error)
        return EXIT_USAGE

    settings = build_rule_mode_settings(config.param_defaults)
    try:
        if args.sim == SIM_BROWSER:
            result = _run_browser_mission(args, decision_model, config.param_defaults, settings)
        else:
            result = _run_mock_mission(args, decision_model, config.param_defaults, settings)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return EXIT_INTERRUPTED

    if result is None:
        return EXIT_FAILURE

    return EXIT_OK if result.is_success else EXIT_FAILURE


def _run_mock_mission(
    args: argparse.Namespace, decision_model: DecisionModel, defaults: ParamDefaults, settings: RuleModeSettings
) -> MissionRunResult:
    flight_control = KinematicFlightControl()
    frame_source = RangeLimitedTargetSource(
        ScriptedTargetSource(flight_control, DEFAULT_MOCK_TARGETS), flight_control, MOCK_DETECTION_RANGE_M
    )
    pacer = WallClockPacer(args.speed) if args.speed > 0 else None

    def advance(dt_sec: float) -> None:
        flight_control.advance(dt_sec)
        if pacer is not None:
            pacer(flight_control.now_sec)

    return _build_runner(
        args, flight_control, frame_source, decision_model, defaults, settings,
        clock=lambda: flight_control.now_sec, advance=advance,
    ).run()


def _run_browser_mission(
    args: argparse.Namespace, decision_model: DecisionModel, defaults: ParamDefaults, settings: RuleModeSettings
) -> MissionRunResult | None:
    """브라우저가 벽시계로 물리를 돌리고 Python 은 따라간다 — 시계는 브라우저 sim 시각이다."""
    components = _load_browser_components()
    bridge = components.sim_bridge()
    server = components.start_sim_server(bridge, args.sim_host, args.sim_port)
    logger.info("simulator page: http://%s:%d%s", args.sim_host, args.sim_port, SIM_PAGE_PATH)

    detection_source = None
    frame_reader = None
    try:
        if not _wait_for_simulator(bridge, args.sim_connect_timeout_sec):
            logger.error("simulator not connected within %.1fs", args.sim_connect_timeout_sec)
            return None
        if not _wait_for_ai_mode(bridge, args.ai_mode_timeout_sec):
            logger.error("AI mode not enabled within %.1fs", args.ai_mode_timeout_sec)
            return None

        flight_control = components.browser_flight_control(bridge)
        yolo_factory = functools.partial(components.create_jpeg_yolo_source, device=args.device)
        detection_source = components.browser_detection_source(bridge, yolo_factory=yolo_factory)
        frame_reader = LatestFrameReader(detection_source)

        return _build_runner(
            args, flight_control, frame_reader, decision_model, defaults, settings,
            clock=lambda: flight_control.get_telemetry().time_sec,
            advance=WallTickSleeper(args.tick_hz),
            should_abort=lambda: not bridge.is_connected(),
        ).run()
    finally:
        if frame_reader is not None:
            frame_reader.close()
        if detection_source is not None:
            detection_source.close()
        server.shutdown()
        server.server_close()


def _build_runner(
    args: argparse.Namespace,
    flight_control: FlightControl,
    frame_source: MissionFrameSource,
    decision_model: DecisionModel,
    defaults: ParamDefaults,
    settings: RuleModeSettings,
    clock: Callable[[], float],
    advance: Callable[[float], None],
    should_abort: Callable[[], bool] | None = None,
) -> MissionRunner:
    return MissionRunner(
        flight_control=flight_control,
        frame_source=frame_source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=decision_model,
        param_defaults=defaults,
        settings=settings,
        clock=clock,
        advance=advance,
        config=MissionConfig(max_mission_sec=args.max_mission_sec, tick_hz=args.tick_hz),
        should_abort=should_abort,
    )


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


def _wait_for_simulator(bridge, timeout_sec: float) -> bool:
    deadline_sec = time.monotonic() + timeout_sec

    while not bridge.is_connected():
        if time.monotonic() >= deadline_sec:
            return False
        time.sleep(SIM_CONNECT_POLL_SEC)
    return True


def _wait_for_ai_mode(bridge, timeout_sec: float) -> bool:
    deadline_sec = time.monotonic() + timeout_sec
    logger.info("waiting for AI mode: press M in the simulator page")

    while not bridge.ai_mode:
        if time.monotonic() >= deadline_sec:
            return False
        time.sleep(AI_MODE_POLL_SEC)
    return True


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # parser.error 는 종료 코드 2로 끝낸다
    if args.max_mission_sec <= 0:
        parser.error(f"--max-mission-sec must be > 0, got {args.max_mission_sec}")
    if args.tick_hz <= 0:
        parser.error(f"--tick-hz must be > 0, got {args.tick_hz}")
    if args.speed < 0:
        parser.error(f"--speed must be >= 0, got {args.speed}")
    if args.sim_connect_timeout_sec <= 0:
        parser.error(f"--sim-connect-timeout-sec must be > 0, got {args.sim_connect_timeout_sec}")
    if args.ai_mode_timeout_sec <= 0:
        parser.error(f"--ai-mode-timeout-sec must be > 0, got {args.ai_mode_timeout_sec}")


if __name__ == "__main__":
    sys.exit(main())
