"""임무 실행 진입점 — 규칙 선택기가 고른 rule_mode 로 이륙부터 착륙까지 한 번에 수행한다 (D-13, D-14, D-16).

pipeline.py / run_scenario.py 는 건드리지 않는 별도 컴포지션 루트다. 선택기는 WAVEPILOT_SELECTOR 로 고른다
(rule 기본, d1 이면 D1_SERVER_URL 필요 — SelectorConfig.from_env + create_decision_model).

실행 예시 (WavePilot 루트에서):
    uv run python -m modules.drone.run_mission --sim mock
    uv run python -m modules.drone.run_mission --sim mock --mission missions/red_heart.toml
    uv run python -m modules.drone.run_mission --sim mock --mission missions/red_heart.toml --log-dir logs/missions
    uv run python -m modules.drone.run_mission --sim mock --target-color none   (색 조건 없음, Phase 3 동작)
    uv run --group yolo python -m modules.drone.run_mission --sim browser --mission missions/red_heart.toml
    uv run python -m modules.drone.run_mission --sim browser --yolo orin --yolo-url http://<orin-ip>:8082
    (browser: http://127.0.0.1:8766/drone_camera.html 을 열고 AI 모드로 전환한다.
     패널에 AI mission 상태 줄(MODE·사유·링크)이 보이고, 이상 주입은 패널 Fault injection 버튼으로 한다 — D-19)
    (browser 실행이면 브라우저가 판정한 발마다의 맞은 배우가 같은 JSONL 에 hit 줄로 남는다 — Phase 6 비교 집계용)
    uv run --group yolo python -m modules.drone.run_mission --sim browser --mission missions/urban_demo.toml
    (mission_demo: http://127.0.0.1:8766/mission_demo/ — 선택·모드 종료가 브리지 pipeline 의 selection·mode_end
     칸으로 가고 페이지 LLM Decisions 패널이 읽는다)

--mission 임무 파일이 탐색 구역·상체 색·최대 비행 시간·안전 여유를 정한다(파일 색이 --target-color 보다 우선).
임무 파일이 없으면 params 기본값 + CLI 로 돈다(Phase 3·4 동작).

--sim mock 은 헤드리스다. KinematicFlightControl 과 스크립트 대상(빨간 옷 1명 + 다른 색 1명)으로 YOLO 없이 돈다.
--target-color red(기본)면 빨간 옷 사람에게만 하트를 쏜다. none 이면 색을 따지지 않는다.
--speed 0 은 벽시계를 기다리지 않고 최대 속도로 돈다. 로그(stderr)에 선택마다 selection 줄,
모드 종료마다 mode_end 줄이 나온다. 같은 사건이 logs/missions/<시작시각>.jsonl(JSON Lines)에도 남는다(--log-dir).
return_home 이 착지하면 종료 코드 0, 그 밖의 종료는 1 이다.
"""

import argparse
import dataclasses
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

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
    MISSION_DEMO_PAGE_PATH,
    SIM_BROWSER,
    SIM_CHOICES,
    SIM_MOCK,
    SIM_PAGE_PATH,
    BrowserComponents,
    WallClockPacer,
)
from modules.drone.yolo_factory import DEFAULT_YOLO_URL, YOLO_CHOICES, YOLO_LOCAL, create_yolo_factory
from modules.flight_control.interface import FlightControl, Telemetry
from modules.mission.mission_file import MissionSpec, load_mission_file
from modules.mission.mission_log import DEFAULT_LOG_DIR, KIND_HIT, KIND_MISSION, MissionLog, default_log_path
from modules.mission.operator import LinkStatus, OperatorInput, ScriptedOperator
from modules.mission.safety import SafetyConfig, SafetyMonitor
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.interface import RuleModeSettings, track_label
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource
from modules.yolo_detection.interface import Frame

logger = logging.getLogger("modules.drone.run_mission")

DEFAULT_MAX_MISSION_SEC = 300.0
DEFAULT_TICK_HZ = 10.0
DEFAULT_AI_MODE_TIMEOUT_SEC = 120.0
SIM_CONNECT_POLL_SEC = 0.1
AI_MODE_POLL_SEC = 0.5

DEFAULT_TARGET_COLOR = "red"
TARGET_COLOR_NONE = "none"
TARGET_COLOR_CHOICES = (DEFAULT_TARGET_COLOR, TARGET_COLOR_NONE)

DEFAULT_MISSION_LOG_DIR = DEFAULT_LOG_DIR  # WavePilot 루트(작업 디렉터리) 상대 경로
MISSION_CONSOLE_LOGGER_NAME = "modules.drone.mission_loop"  # 기존 selection/mode_end 줄과 같은 로거

# 브리지 PIPELINE_MESSAGE_KINDS 의 값 — bridge 는 browser 실행에서만 import 하므로 여기 둔다
PIPELINE_KIND_SELECTION = "selection"
PIPELINE_KIND_MODE_END = "mode_end"

# mock 세계 — 홈 북쪽 수색 구역 안에 빨간 옷 1명 + 다른 색 1명 (D-06, D-07).
# 시야 안에 들어와야 보이도록 탐지 거리를 제한한다
DEFAULT_MOCK_TARGETS = (
    ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0, upper_color="red"),
    ScriptedTarget(track_id=2, class_name="person", north_m=95.0, east_m=30.0, upper_color="other"),
)
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


@dataclasses.dataclass(frozen=True)
class MissionSetup:
    """임무 한 번에 필요한 값 묶음 — 임무 파일·CLI 를 합친 결과를 실행 함수로 한꺼번에 넘긴다."""

    defaults: ParamDefaults
    settings: RuleModeSettings
    safety_config: SafetyConfig | None  # None 이면 MissionRunner 기본 안전 감시
    max_mission_sec: float
    mission_path: str | None
    spec: MissionSpec | None
    selector_kind: str
    mission_log: MissionLog


class BridgeOperatorInput:
    """브라우저 브리지를 OperatorInput 으로 — 조종자 모드 버튼과 운용자 링크 heartbeat 를 읽는다.

    브리지 자체 연결(페이지가 POST 를 보내는지)은 should_abort 가 따로 본다 (D-01).
    """

    def __init__(self, bridge) -> None:
        self._bridge = bridge

    def is_ai_requested(self) -> bool:
        return bool(self._bridge.ai_mode)

    def link_status(self, now_sec: float) -> LinkStatus:
        # 마지막 heartbeat 시각은 브리지가 sim 시각으로 들고 있어 now_sec 는 쓰지 않는다
        return LinkStatus(*self._bridge.operator_link_status())


def record_heart_shots(bridge, mission_log: MissionLog) -> int:
    """브라우저가 발마다 보낸 heart_shot(맞은 배우 판정, C2)을 꺼내 hit 줄로 남기고 개수를 돌려준다.

    판정은 브라우저 레이캐스트 결과 그대로 기록만 한다 — 값 검증은 브리지가 이미 했다.
    """
    heart_shot_list = bridge.drain_heart_shots()

    for event in heart_shot_list:
        track_id = event["track_id"]
        mission_log.record(
            KIND_HIT,
            event["time_sec"],
            track=track_label(track_id),
            track_id=track_id,
            actor=event.get("actor_id"),
        )

    return len(heart_shot_list)


class BridgePipelinePublisher:
    """선택·모드 종료 payload 를 브리지 pipeline 칸에 {seq, sent_at, payload} 로 넣는다. seq 는 kind 별 단조 증가."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self._seq_by_kind: dict[str, int] = {}

    def on_selection(self, payload: dict) -> None:
        self.publish(PIPELINE_KIND_SELECTION, payload)

    def on_mode_end(self, payload: dict) -> None:
        self.publish(PIPELINE_KIND_MODE_END, payload)

    def publish(self, kind: str, payload: dict) -> None:
        seq = self._seq_by_kind.get(kind, 0) + 1
        self._seq_by_kind[kind] = seq

        # sent_at 은 pipeline.py 와 같은 형식(로컬 ISO 8601, 밀리초)
        sent_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self._bridge.publish_pipeline_message(kind, {"seq": seq, "sent_at": sent_at, "payload": payload})


def _build_browser_status_handler(bridge, mission_log: MissionLog) -> Callable[[dict], None]:
    """매 틱 상태 콜백 — 패널 상태를 먼저 push 하고, 그 뒤 쌓인 heart_shot 을 로그로 옮긴다."""

    def handle_status(status: dict) -> None:
        bridge.set_mission_status(status)
        record_heart_shots(bridge, mission_log)

    return handle_status


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.drone.run_mission",
        description="Run a full rule_mode mission (take off, selector-driven modes, return home, land).",
    )
    parser.add_argument("--sim", choices=SIM_CHOICES, default=SIM_MOCK, help="simulator implementation (default mock)")
    parser.add_argument(
        "--mission", default=None,
        help="TOML mission file; overrides search area, target color, safety limits",
    )
    parser.add_argument(
        "--log-dir", default=DEFAULT_MISSION_LOG_DIR,
        help=f"directory for <start time>.jsonl mission logs, relative to the working directory "
        f"(run from WavePilot root; default {DEFAULT_MISSION_LOG_DIR})",
    )
    parser.add_argument(
        "--max-mission-sec", type=float, default=DEFAULT_MAX_MISSION_SEC,
        help="sim seconds after which return_home is forced; ignored when --mission is given "
        "(mission max_flight_sec wins)",
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
    # 기본값 None — 임무 파일과 함께 지정했는지 구분한다 (지정 안 하면 red)
    parser.add_argument(
        "--target-color", choices=TARGET_COLOR_CHOICES, default=None,
        help=f"upper-body color the mission targets (default {DEFAULT_TARGET_COLOR}); "
        "none = any color (Phase 3 behavior); a --mission file color wins",
    )
    parser.add_argument("--device", default=DEFAULT_YOLO_DEVICE, help="YOLO device for --sim browser: auto | mps | cpu | 0")
    parser.add_argument(
        "--yolo", choices=YOLO_CHOICES, default=YOLO_LOCAL, help="YOLO for --sim browser: local (Mac) | orin (remote server)"
    )
    parser.add_argument("--yolo-url", default=DEFAULT_YOLO_URL, help=f"Orin YOLO server URL for --yolo orin (default {DEFAULT_YOLO_URL})")
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

    # 잘못된 임무 파일은 비행·로그 파일 생성 전에 멈춘다 (D-14)
    spec = None
    if args.mission is not None:
        try:
            spec = load_mission_file(args.mission)
        except (ValueError, OSError) as error:
            logger.error("invalid mission file %s: %s", args.mission, error)
            return EXIT_USAGE

    log_path = default_log_path(Path(args.log_dir), datetime.now())
    logger.info("mission log: %s", log_path)

    try:
        with MissionLog(log_path, console=logging.getLogger(MISSION_CONSOLE_LOGGER_NAME)) as mission_log:
            setup = _create_mission_setup(args, config, spec, mission_log)
            if args.sim == SIM_BROWSER:
                result = _run_browser_mission(args, decision_model, setup)
            else:
                result = _run_mock_mission(args, decision_model, setup)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return EXIT_INTERRUPTED

    if result is None:
        return EXIT_FAILURE

    return EXIT_OK if result.is_success else EXIT_FAILURE


def resolve_target_upper_color(target_color_arg: str | None, spec: MissionSpec | None) -> str | None:
    """대상 상체 색 — 임무 파일 > --target-color > 기본 red. none 은 색 조건 없음(None)."""
    if spec is not None:
        if target_color_arg is not None:
            logger.info(
                "mission file target color %s overrides --target-color %s",
                spec.target_upper_color or "any", target_color_arg,
            )
        return spec.target_upper_color

    if target_color_arg is None:
        return DEFAULT_TARGET_COLOR
    if target_color_arg == TARGET_COLOR_NONE:
        return None
    return target_color_arg


def apply_mission_spec(defaults: ParamDefaults, spec: MissionSpec) -> ParamDefaults:
    """임무 파일의 탐색 구역(과 있으면 탐색 고도)으로 기본값을 덮는다 (D-13). 복귀 지점(home_*)은 파일에 없어 그대로다 (D-12)."""
    search_altitude_m = defaults.search_altitude_m if spec.search_altitude_m is None else spec.search_altitude_m
    return dataclasses.replace(
        defaults,
        search_area_north_min_m=spec.search_north_min_m,
        search_area_north_max_m=spec.search_north_max_m,
        search_area_east_min_m=spec.search_east_min_m,
        search_area_east_max_m=spec.search_east_max_m,
        search_altitude_m=search_altitude_m,
    )


def build_safety_config(spec: MissionSpec, settings: RuleModeSettings, defaults: ParamDefaults) -> SafetyConfig:
    return SafetyConfig(
        search_area=settings.search_area,
        geofence_margin_m=spec.geofence_margin_m,
        battery_reserve_pct=spec.battery_reserve_pct,
        battery_check=spec.battery_check,
        max_flight_sec=spec.max_flight_sec,
        home_north_m=defaults.home_north_m,
        home_east_m=defaults.home_east_m,
    )


def build_mock_world(
    targets: Sequence[ScriptedTarget] = DEFAULT_MOCK_TARGETS,
) -> tuple[KinematicFlightControl, RangeLimitedTargetSource]:
    """mock 임무 세계 — 가짜 비행과 탐지 거리 제한 스크립트 대상. CLI 와 헤드리스 E2E 가 같은 세계를 쓴다."""
    flight_control = KinematicFlightControl()
    frame_source = RangeLimitedTargetSource(
        ScriptedTargetSource(flight_control, targets), flight_control, MOCK_DETECTION_RANGE_M
    )

    return flight_control, frame_source


def _create_mission_setup(
    args: argparse.Namespace, config: SelectorConfig, spec: MissionSpec | None, mission_log: MissionLog
) -> MissionSetup:
    if spec is None:
        defaults = config.param_defaults
        target_class = "person"
    else:
        defaults = apply_mission_spec(config.param_defaults, spec)
        target_class = spec.target_class

    target_upper_color = resolve_target_upper_color(args.target_color, spec)
    settings = build_rule_mode_settings(defaults, target_class=target_class, target_upper_color=target_upper_color)

    if spec is None:
        safety_config = None
        max_mission_sec = args.max_mission_sec
    else:
        safety_config = build_safety_config(spec, settings, defaults)
        max_mission_sec = spec.max_flight_sec
        logger.info("mission file max_flight_sec %.1f s replaces --max-mission-sec", max_mission_sec)

    return MissionSetup(
        defaults=defaults,
        settings=settings,
        safety_config=safety_config,
        max_mission_sec=max_mission_sec,
        mission_path=str(args.mission) if args.mission is not None else None,
        spec=spec,
        selector_kind=config.kind,
        mission_log=mission_log,
    )


def _run_mock_mission(
    args: argparse.Namespace, decision_model: DecisionModel, setup: MissionSetup
) -> MissionRunResult:
    flight_control, frame_source = build_mock_world()
    pacer = WallClockPacer(args.speed) if args.speed > 0 else None

    def advance(dt_sec: float) -> None:
        flight_control.advance(dt_sec)
        if pacer is not None:
            pacer(flight_control.now_sec)

    return _build_runner(
        args, flight_control, frame_source, decision_model, setup,
        clock=lambda: flight_control.now_sec, advance=advance, operator=ScriptedOperator(),
    ).run()


def _run_browser_mission(
    args: argparse.Namespace, decision_model: DecisionModel, setup: MissionSetup
) -> MissionRunResult | None:
    """브라우저가 벽시계로 물리를 돌리고 Python 은 따라간다 — 시계는 브라우저 sim 시각이다."""
    components = _load_browser_components()
    bridge = components.sim_bridge()
    server = components.start_sim_server(bridge, args.sim_host, args.sim_port)
    logger.info("simulator page: http://%s:%d%s", args.sim_host, args.sim_port, SIM_PAGE_PATH)
    logger.info("mission demo page: http://%s:%d%s", args.sim_host, args.sim_port, MISSION_DEMO_PAGE_PATH)

    detection_source = None
    frame_reader = None
    try:
        if not _wait_for_simulator(bridge, args.sim_connect_timeout_sec):
            logger.error("simulator not connected within %.1fs", args.sim_connect_timeout_sec)
            return None

        # YOLO 는 페이지가 붙자마자 돌린다 — MANUAL 에서도 YOLO log·FPV 박스가 보이게
        yolo_factory = create_yolo_factory(args.yolo, args.device, args.yolo_url)
        detection_source = components.browser_detection_source(bridge, yolo_factory=yolo_factory)
        frame_reader = LatestFrameReader(detection_source)

        if not _wait_for_ai_mode(bridge, args.ai_mode_timeout_sec):
            logger.error("AI mode not enabled within %.1fs", args.ai_mode_timeout_sec)
            return None

        flight_control = components.browser_flight_control(bridge)

        # 조종자 모드·운용자 링크는 매 틱 operator 로 읽고, 브리지 자체가 끊기면 중단한다 (D-01, D-02)
        publisher = BridgePipelinePublisher(bridge)
        result = _build_runner(
            args, flight_control, frame_reader, decision_model, setup,
            clock=lambda: flight_control.get_telemetry().time_sec,
            advance=WallTickSleeper(args.tick_hz),
            operator=BridgeOperatorInput(bridge),
            should_abort=lambda: not bridge.is_connected(),
            on_status=_build_browser_status_handler(bridge, setup.mission_log),
            on_selection=publisher.on_selection,
            on_mode_end=publisher.on_mode_end,
        ).run()

        # 마지막 틱 뒤에 도착한 연사 발까지 남긴다 (C3)
        record_heart_shots(bridge, setup.mission_log)

        return result
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
    setup: MissionSetup,
    clock: Callable[[], float],
    advance: Callable[[float], None],
    operator: OperatorInput,
    should_abort: Callable[[], bool] | None = None,
    on_status: Callable[[dict], None] | None = None,
    on_selection: Callable[[dict], None] | None = None,
    on_mode_end: Callable[[dict], None] | None = None,
) -> MissionRunner:
    _record_mission_start(setup, flight_control)

    safety = SafetyMonitor(setup.safety_config) if setup.safety_config is not None else None

    return MissionRunner(
        flight_control=flight_control,
        frame_source=frame_source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=decision_model,
        param_defaults=setup.defaults,
        settings=setup.settings,
        clock=clock,
        advance=advance,
        config=MissionConfig(max_mission_sec=setup.max_mission_sec, tick_hz=args.tick_hz),
        should_abort=should_abort,
        operator=operator,
        safety=safety,
        mission_log=setup.mission_log,
        on_status=on_status,
        on_selection=on_selection,
        on_mode_end=on_mode_end,
    )


def _record_mission_start(setup: MissionSetup, flight_control: FlightControl) -> None:
    # 홈 = 첫 텔레메트리 GPS(운용자 위치) — 파일에 복귀 지점이 없어 로그로 남긴다 (D-12)
    telemetry = flight_control.get_telemetry()
    home_gps = telemetry.gps.to_dict() if telemetry.gps is not None else None

    setup.mission_log.record(
        KIND_MISSION,
        telemetry.time_sec,
        event="start",
        mission_file=setup.mission_path,
        mission=dataclasses.asdict(setup.spec) if setup.spec is not None else None,
        selector=setup.selector_kind,
        home_gps=home_gps,
    )


def _load_browser_components() -> BrowserComponents:
    # browser 모드에서만 import 한다 — YOLO 어댑터는 무겁고, mock 실행은 브라우저 구현 없이도 돌아야 한다
    from modules.simulation.browser.bridge import SimBridge
    from modules.simulation.browser.detection_source import BrowserDetectionSource
    from modules.simulation.browser.flight_control import BrowserFlightControl
    from modules.simulation.browser.server import start_sim_server

    return BrowserComponents(
        sim_bridge=SimBridge,
        start_sim_server=start_sim_server,
        browser_flight_control=BrowserFlightControl,
        browser_detection_source=BrowserDetectionSource,
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
    logger.info("waiting for AI mode: press M in the simulator page to start the AI mission")

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
