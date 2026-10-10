"""드론 측 프레임 루프 — 탐지 프레임을 track·상태 JSON·보고로 바꿔 CommLink 로 보낸다 (MOCK-01, RPT-01, COMM-01).

프레임 1개당 단계 순서:
  (1) 프레임 읽기(None 이면 끝) → (2) pace 훅(벽시계 맞추기) → (3) 텔레메트리 읽기
  → (4) track 갱신 → (5) 운용자 수신 메시지 처리 → (6) 텔레메트리 주기 송신 → (7) 상태·보고 주기 송신

메시지 4종 중 드론은 telemetry / state / report 3종을 보내고 command 1종을 받는다.
시계는 frame.timestamp_sec 하나뿐이다. 이 파일은 time.sleep·벽시계를 쓰지 않으므로 pace=None 실행은 결정론적이다.

이 파일은 인터페이스(interfaces.*)와 llm_module 최상위 파사드만 import 한다. mock·전송·인식 구현체를
몰라야 실제 YOLO·ArduPilot·무선 링크로 바꿀 때 이 파일이 그대로 남는다(SC4/SC7).
구현체 선택과 조립은 drone/run_scenario.py 만 한다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from interfaces.comm import CommLink, Message
from interfaces.detection import DetectionSource, Frame
from interfaces.flight_control import FlightControl, Telemetry, command_from_dict
from interfaces.track import Track
from llm_module import LLMModule

logger = logging.getLogger(__name__)

DEFAULT_REPORT_INTERVAL_SEC = 8.0  # SKELETON 시계 행 — 상태·보고 주기
DEFAULT_TELEMETRY_INTERVAL_SEC = 1.0  # SKELETON 시계 행 — 텔레메트리 주기
DEFAULT_REPORT_DRAIN_TIMEOUT_SEC = 90.0  # LLM 요청 타임아웃 60 s + 여유
MAX_INBOUND_MESSAGES_PER_FRAME = 10  # 명령 폭주가 프레임 루프를 막지 않게 (T-01-31)
SCHEDULE_EPSILON_SEC = 1e-6  # 부동소수 누적 비교용


class TrackProvider(Protocol):
    """track 집계기 자리. perception 구현을 import 하지 않고 구조적 타이핑으로 받는다 —
    인식 구현을 바꾸는 지점이 여기라는 것을 드러내기 위해서다."""

    def update(self, frame: Frame, telemetry: Telemetry) -> None: ...

    def snapshot(self, now_sec: float) -> list[Track]: ...


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    report_interval_sec: float = DEFAULT_REPORT_INTERVAL_SEC
    telemetry_interval_sec: float = DEFAULT_TELEMETRY_INTERVAL_SEC
    run_reports_in_background: bool = False
    report_drain_timeout_sec: float = DEFAULT_REPORT_DRAIN_TIMEOUT_SEC


@dataclass(frozen=True, slots=True)
class RunSummary:
    frame_count: int
    final_time_sec: float
    telemetry_count: int
    state_count: int
    report_count: int
    skipped_report_count: int
    failed_report_count: int
    command_count: int
    rejected_command_count: int
    link_lost: bool


class _LinkLostError(Exception):
    """송신 실패로 루프를 멈춰야 할 때 run() 까지 올리는 내부 신호. 밖으로 나가지 않는다."""


class DronePipeline:
    def __init__(
        self,
        detection_source: DetectionSource,
        flight_control: FlightControl,
        track_provider: TrackProvider,
        llm_module: LLMModule,
        link: CommLink,
        config: PipelineConfig = PipelineConfig(),
        pace: Callable[[float], None] | None = None,
    ) -> None:
        self._detection_source = detection_source
        self._flight_control = flight_control
        self._track_provider = track_provider
        self._llm_module = llm_module
        self._link = link
        self._config = config
        self._pace = pace

        self._next_seq = 0
        self._next_telemetry_sec = 0.0
        self._next_report_sec = config.report_interval_sec  # 첫 보고는 한 주기 뒤

        self._frame_count = 0
        self._final_time_sec = 0.0
        self._telemetry_count = 0
        self._state_count = 0
        self._report_count = 0
        self._skipped_report_count = 0
        self._failed_report_count = 0
        self._command_count = 0
        self._rejected_command_count = 0
        self._is_link_lost = False

    def run(self) -> RunSummary:
        try:
            self._run_frame_loop()
        except _LinkLostError:
            pass
        finally:
            self._link.close()

        return self._build_summary()

    # --- 프레임 루프 ---

    def _run_frame_loop(self) -> None:
        while True:
            frame = self._detection_source.read_frame()
            if frame is None:
                return

            now_sec = frame.timestamp_sec
            self._frame_count += 1
            self._final_time_sec = now_sec

            if self._pace is not None:
                self._pace(now_sec)

            telemetry = self._flight_control.get_telemetry()
            self._track_provider.update(frame, telemetry)
            self._handle_inbound_messages(now_sec)

            if self._is_due(now_sec, self._next_telemetry_sec):
                self._send_telemetry(self._flight_control.get_telemetry())
                self._next_telemetry_sec += self._config.telemetry_interval_sec

            if self._is_due(now_sec, self._next_report_sec):
                self._send_state_and_report(now_sec, telemetry)
                self._next_report_sec += self._config.report_interval_sec

    def _is_due(self, now_sec: float, due_sec: float) -> bool:
        return now_sec + SCHEDULE_EPSILON_SEC >= due_sec

    # --- 수신 ---

    def _handle_inbound_messages(self, now_sec: float) -> None:
        for _ in range(MAX_INBOUND_MESSAGES_PER_FRAME):
            message = self._link.receive(timeout_sec=0.0)
            if message is None:
                return

            if message.type != "command":
                logger.warning("ignored inbound %s message", message.type)
                continue

            self._handle_command_message(message, now_sec)

    def _handle_command_message(self, message: Message, now_sec: float) -> None:
        # 운용자 쪽 입력은 신뢰하지 않는다 — 화이트리스트 4종만 통과 (T-01-30)
        try:
            command = command_from_dict(message.payload)
        except ValueError as error:
            self._rejected_command_count += 1
            logger.warning("rejected command: %s", error)
            return

        ack = self._flight_control.send_command(command)
        self._command_count += 1
        logger.info("command applied: kind=%s accepted=%s reason=%s", command.KIND, ack.accepted, ack.reason)

        # 운용자가 다음 주기를 기다리지 않고 바뀐 비행 모드를 보게 텔레메트리를 바로 한 번 보낸다
        self._send_telemetry(self._flight_control.get_telemetry())
        self._next_telemetry_sec = now_sec + self._config.telemetry_interval_sec

    # --- 송신 ---

    def _send_telemetry(self, telemetry: Telemetry) -> None:
        self._send_message("telemetry", telemetry.to_dict())
        self._telemetry_count += 1

    def _send_state_and_report(self, now_sec: float, telemetry: Telemetry) -> None:
        tracks = self._track_provider.snapshot(now_sec)
        state = self._llm_module.build_state(tracks, telemetry)
        self._send_message("state", state.to_dict())
        self._state_count += 1

        # 보고 실패로 파이프라인을 멈추지 않는다 (AI-SPEC 실패 처리 정책)
        try:
            output = self._llm_module.process_state(state)
        except Exception as error:
            self._failed_report_count += 1
            logger.error("report generation failed: %s", error)
            return

        payload = output.to_report_payload()
        self._send_message("report", payload)
        self._report_count += 1
        logger.info("report sent: time_sec=%s verdict=%s", payload["time_sec"], payload["decision"]["verdict"])

    def _send_message(self, message_type: str, payload: dict) -> None:
        # sent_at 은 운용자 콘솔 줄 머리와 같은 형식(로컬 ISO 8601, 밀리초)
        sent_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        message = Message(type=message_type, seq=self._next_seq, sent_at=sent_at, payload=payload)
        self._next_seq += 1

        try:
            self._link.send(message)
        except OSError as error:
            self._is_link_lost = True
            logger.error("link lost while sending %s: %s", message_type, error)
            raise _LinkLostError from error

    # --- 결과 ---

    def _build_summary(self) -> RunSummary:
        return RunSummary(
            frame_count=self._frame_count,
            final_time_sec=self._final_time_sec,
            telemetry_count=self._telemetry_count,
            state_count=self._state_count,
            report_count=self._report_count,
            skipped_report_count=self._skipped_report_count,
            failed_report_count=self._failed_report_count,
            command_count=self._command_count,
            rejected_command_count=self._rejected_command_count,
            link_lost=self._is_link_lost,
        )
