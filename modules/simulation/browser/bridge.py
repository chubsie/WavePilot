"""SimBridge — 브라우저 시뮬레이터(10 Hz push) ⇄ Python 사이의 유일한 공유 상태 (D-09, D-11, D-12).

- 텔레메트리·AI 모드·이벤트는 브라우저가 밀어 넣고, 연결 여부는 마지막 수신 후 LINK_TIMEOUT_SEC 로 판정한다.
- 브리지 연결(LINK_TIMEOUT_SEC) = 기체 내부 배선, 운용자 링크(OPERATOR_LINK_TIMEOUT_SEC) = 텔레메트리 POST 의 operator_link heartbeat (Phase 5 D-01).
- 프레임은 최신 한 칸만 유지한다 — 새 프레임이 오면 오래된 프레임은 버린다(YOLO 가 밀려도 지연이 쌓이지 않게).
- 명령은 큐에 쌓였다가 drain 한 번에 한 번만 배달된다(id 는 1 부터 단조 증가).
- heart_shot 이벤트(발마다 맞은 배우, Phase 6 D-10)는 recent_events 와 별도 큐 — drain 한 번에 한 번만 나온다.
- 파이프라인 state·report 는 종류별 최신 한 칸 — --operator browser 일 때 페이지가 /sim/telemetry 로 읽는다.

페이로드 검증(Telemetry.from_dict, 크기 상한)은 HTTP 서버(02-02) 몫이다 — bridge 는 타입이 맞는 객체만 받는다.
모든 상태는 Condition 하나(내부 Lock)로 보호하므로 서버 스레드·파이프라인 스레드가 동시에 불러도 된다.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from modules.flight_control.interface import Command, Telemetry

LINK_TIMEOUT_SEC = 1.0
OPERATOR_LINK_TIMEOUT_SEC = 3.0
RECENT_EVENT_LIMIT = 50
HEART_SHOT_EVENT = "heart_shot"
HEART_SHOT_LIMIT = 1000
FRAME_FPS_WINDOW_SEC = 1.0
PIPELINE_MESSAGE_KINDS = ("state", "report")


@dataclass(frozen=True, slots=True)
class BrowserFrame:
    seq: int
    timestamp_sec: float
    jpeg_bytes: bytes
    telemetry: Telemetry
    received_at_sec: float


class SimBridge:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._closed = False

        self._telemetry: Telemetry | None = None
        self._telemetry_received_at_sec: float | None = None
        self._ai_mode = False
        self._events: deque[dict] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._heart_shots: deque[dict] = deque(maxlen=HEART_SHOT_LIMIT)  # 넘치면 오래된 것부터 버린다

        self._operator_heartbeat_at_sec: float | None = None
        self._operator_heartbeat_time_sec: float | None = None

        self._mission_status: dict | None = None

        self._next_command_id = 1
        self._pending_commands: list[tuple[int, Command]] = []

        self._frame: BrowserFrame | None = None
        self._frame_received_times: deque[float] = deque()

        self._detections: dict | None = None
        self._detections_seq: int | None = None
        self._yolo_latency_ms: float | None = None

        self._pipeline_messages: dict[str, dict | None] = dict.fromkeys(PIPELINE_MESSAGE_KINDS)

    # --- 브라우저 → Python: 텔레메트리·AI 모드·이벤트 ---

    def update_from_browser(
        self,
        telemetry: Telemetry,
        ai_mode: bool,
        events: list[dict],
        operator_link: bool = True,
    ) -> None:
        with self._condition:
            received_at_sec = self._clock()
            self._telemetry = telemetry
            self._telemetry_received_at_sec = received_at_sec
            self._ai_mode = ai_mode
            # 호출자가 나중에 dict 를 고쳐도 저장본이 바뀌지 않게 복사한다
            for event in events:
                stored = dict(event)
                if stored.get("kind") == HEART_SHOT_EVENT:
                    self._heart_shots.append(stored)
                else:
                    self._events.append(stored)

            # 운용자 링크가 끊긴 POST 는 heartbeat 로 치지 않는다 — 브리지 연결은 위에서 그대로 갱신된다 (D-01)
            if operator_link:
                self._operator_heartbeat_at_sec = received_at_sec
                self._operator_heartbeat_time_sec = telemetry.time_sec

    def latest_telemetry(self) -> Telemetry | None:
        with self._condition:
            return self._telemetry

    def is_connected(self) -> bool:
        with self._condition:
            if self._telemetry_received_at_sec is None:
                return False
            return self._clock() - self._telemetry_received_at_sec <= LINK_TIMEOUT_SEC

    def operator_link_status(self) -> tuple[bool, float | None]:
        """(운용자 링크 연결 여부, 마지막 heartbeat 의 sim 시각). 05-06 BridgeOperatorInput 이 LinkStatus(*...) 로 쓴다.

        heartbeat 를 받은 적 없으면 (False, None). 연결 여부는 마지막 heartbeat 뒤 bridge clock 경과로 판정한다.
        """
        with self._condition:
            if self._operator_heartbeat_at_sec is None:
                return False, None

            elapsed_sec = self._clock() - self._operator_heartbeat_at_sec
            return elapsed_sec <= OPERATOR_LINK_TIMEOUT_SEC, self._operator_heartbeat_time_sec

    @property
    def ai_mode(self) -> bool:
        with self._condition:
            return self._ai_mode

    def recent_events(self) -> tuple[dict, ...]:
        with self._condition:
            return tuple(dict(event) for event in self._events)

    def drain_heart_shots(self) -> list[dict]:
        """쌓인 heart_shot 이벤트를 들어온 순서대로 꺼내고 비운다. 06-03 run_mission 이 매 틱 비워 hit 로그로 옮긴다."""
        with self._condition:
            shots = list(self._heart_shots)
            self._heart_shots.clear()

        return shots

    # --- Python → 브라우저: 명령 큐 ---

    def enqueue_command(self, command: Command) -> int:
        if not isinstance(command, Command):
            raise TypeError(f"command must be a flight Command, got {type(command).__name__}")

        with self._condition:
            command_id = self._next_command_id
            self._next_command_id += 1
            self._pending_commands.append((command_id, command))
            return command_id

    def drain_commands(self) -> list[dict]:
        with self._condition:
            pending = self._pending_commands
            self._pending_commands = []

        return [{"id": command_id, **command.to_dict()} for command_id, command in pending]

    # --- Python → 브라우저: 임무 상태 (Phase 5 패널) ---

    def set_mission_status(self, status: dict) -> None:
        if not isinstance(status, dict):
            raise TypeError(f"status must be a dict, got {type(status).__name__}")

        with self._condition:
            self._mission_status = dict(status)

    def mission_status(self) -> dict | None:
        with self._condition:
            if self._mission_status is None:
                return None
            return dict(self._mission_status)

    # --- 프레임 슬롯 ---

    def put_frame(self, frame: BrowserFrame) -> None:
        with self._condition:
            if self._closed:
                return

            self._frame = frame
            self._frame_received_times.append(frame.received_at_sec)
            self._condition.notify_all()

    def wait_frame(self, after_seq: int | None, timeout_sec: float) -> BrowserFrame | None:
        """after_seq 보다 새 프레임이 오면 돌려준다. 타임아웃이나 close 면 None."""
        # 대기 마감은 실제 시간으로 잰다 — 주입 clock 은 연결·fps 판정 전용이라
        # 테스트용 가짜 시계가 멈춰 있어도 대기가 끝나야 한다
        deadline = time.monotonic() + timeout_sec

        with self._condition:
            while True:
                if self._closed:
                    return None
                if self._is_newer_frame(after_seq):
                    return self._frame

                remaining_sec = deadline - time.monotonic()
                if remaining_sec <= 0:
                    return None
                self._condition.wait(remaining_sec)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _is_newer_frame(self, after_seq: int | None) -> bool:
        if self._frame is None:
            return False
        return after_seq is None or self._frame.seq > after_seq

    # --- 탐지 결과·상태 ---

    def publish_detections(self, frame_dict: dict, latency_ms: float, seq: int | None = None) -> None:
        """seq 는 추론한 브라우저 프레임 번호 — 페이지가 박스를 어느 프레임에 맞출지 고르는 데 쓴다."""
        with self._condition:
            self._detections = frame_dict
            self._detections_seq = seq
            self._yolo_latency_ms = latency_ms

    def latest_detections(self) -> dict | None:
        with self._condition:
            return self._detections

    def latest_browser_detections(self) -> dict | None:
        """/sim/telemetry 응답용 — seq 가 있으면 탐지 dict 에 "seq" 를 붙인다. Frame 인터페이스는 그대로 둔다."""
        with self._condition:
            if self._detections is None or self._detections_seq is None:
                return self._detections
            return {**self._detections, "seq": self._detections_seq}

    # --- Python → 브라우저: 파이프라인 state·report (운용자 콘솔 대신 페이지가 받는다) ---

    def publish_pipeline_message(self, kind: str, entry: dict) -> None:
        """kind 별 최신 한 칸만 둔다 — 페이지는 10 Hz 폴링이라 지난 값은 필요 없다."""
        if kind not in PIPELINE_MESSAGE_KINDS:
            raise ValueError(f"kind must be one of {PIPELINE_MESSAGE_KINDS}, got {kind!r}")
        if not isinstance(entry, dict):
            raise TypeError(f"entry must be a dict, got {type(entry).__name__}")

        with self._condition:
            self._pipeline_messages[kind] = dict(entry)

    def pipeline_messages(self) -> dict[str, dict | None]:
        with self._condition:
            return {kind: None if entry is None else dict(entry) for kind, entry in self._pipeline_messages.items()}

    def status(self) -> dict:
        with self._condition:
            window_start_sec = self._clock() - FRAME_FPS_WINDOW_SEC
            while self._frame_received_times and self._frame_received_times[0] < window_start_sec:
                self._frame_received_times.popleft()

            return {
                "frame_fps": len(self._frame_received_times) / FRAME_FPS_WINDOW_SEC,
                "yolo_latency_ms": self._yolo_latency_ms,
            }
