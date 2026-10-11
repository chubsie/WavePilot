"""SimBridge — 브라우저 시뮬레이터(10 Hz push) ⇄ Python 사이의 유일한 공유 상태 (D-09, D-11, D-12).

- 텔레메트리·AI 모드·이벤트는 브라우저가 밀어 넣고, 연결 여부는 마지막 수신 후 LINK_TIMEOUT_SEC 로 판정한다.
- 브리지 연결(LINK_TIMEOUT_SEC) = 기체 내부 배선, 운용자 링크(OPERATOR_LINK_TIMEOUT_SEC) = 텔레메트리 POST 의 operator_link heartbeat (Phase 5 D-01).
- 프레임은 최신 한 칸만 유지한다 — 새 프레임이 오면 오래된 프레임은 버린다(YOLO 가 밀려도 지연이 쌓이지 않게).
- 명령은 큐에 쌓였다가 drain 한 번에 한 번만 배달된다(id 는 1 부터 단조 증가).
- heart_shot 이벤트(발마다 맞은 배우, Phase 6 D-10)는 recent_events 와 별도 큐 — drain 한 번에 한 번만 나온다.
- 파이프라인 state·report 는 종류별 최신 한 칸 — --operator browser 일 때 페이지가 /sim/telemetry 로 읽는다.
- 선택기 selection·mode_end 도 같은 칸 방식 — run_mission --sim browser 가 넣고 mission_demo LLM Decisions 패널이 읽는다.

단일 세션 (최신 탭 우선):
- 페이지는 POST 마다 session_id(없으면 익명 세션 None)를 붙인다. 브리지는 활성 세션 하나의 텔레메트리·프레임·이벤트만
  받는다. 처음 보는 session_id 가 오면 그 세션이 활성이 된다(새로 연 탭·새로고침이 이긴다). 밀려난 세션은 활성
  세션이 LINK_TIMEOUT_SEC 넘게 조용할 때만 다시 활성이 된다(새 탭을 닫으면 옛 탭이 이어받는다).
- 비활성 세션의 POST 는 상태를 바꾸지 않고 False 를 돌려받는다. 세션이 바뀌면 옛 탭 몫의 대기 명령은 버린다.

sim 시계 보정:
- 페이지 시계(time_sec)는 새로고침하면 0 부터 다시 시작한다. 세션이 바뀌거나 같은 세션에서 CLOCK_REWIND_TOLERANCE_SEC
  넘게 뒤로 가면, 오프셋을 다시 잡아 보정 시계가 마지막 값에서 이어지게 한다. 텔레메트리·프레임·이벤트의 time_sec
  는 모두 보정 시계로 바꿔 저장하므로 임무 시계·트랙 TTL·안전 감시·로그가 한 시간축을 본다.
  (임무 루프 시계를 monotonic 으로 바꾸면 프레임 timestamp·heartbeat·hit 시각과 축이 갈라진다 — 경계에서 한 번 보정한다.)

페이로드 검증(Telemetry.from_dict, 크기 상한)은 HTTP 서버(02-02) 몫이다 — bridge 는 타입이 맞는 객체만 받는다.
모든 상태는 Condition 하나(내부 Lock)로 보호하므로 서버 스레드·파이프라인 스레드가 동시에 불러도 된다.
"""

import dataclasses
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from modules.flight_control.interface import Command, Telemetry

LINK_TIMEOUT_SEC = 1.0
OPERATOR_LINK_TIMEOUT_SEC = 3.0
RECENT_EVENT_LIMIT = 50
HEART_SHOT_EVENT = "heart_shot"
HEART_SHOT_LIMIT = 1000
FRAME_FPS_WINDOW_SEC = 1.0
PIPELINE_MESSAGE_KINDS = ("state", "report", "selection", "mode_end")
DETECTION_LOG_SIZE = 30  # 페이지 폴링 한 번 사이에 YOLO 가 낸 결과를 빠짐없이 넘길 만큼
DETECTION_LOG_TTL_SEC = 2.0  # 전달용 버퍼라 짧게 — YOLO 가 멈추면 옛 결과를 계속 보내지 않는다
PIPELINE_MESSAGE_TTL_SEC = 10.0  # 보고 주기(기본 8 s)보다 조금 길게 — 파이프라인이 멈추면 state·report 도 사라진다
RETIRED_SESSION_LIMIT = 64  # 밀려난 session_id 기억 개수 — 넘치면 가장 오래된 것부터 잊는다
# 프레임·텔레메트리 POST 는 따로 와서 조금 뒤섞일 수 있다 — 이보다 크게 뒤로 가야 시계 재시작으로 본다
CLOCK_REWIND_TOLERANCE_SEC = 1.0


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
        self._detection_log: deque[tuple[float, dict]] = deque(maxlen=DETECTION_LOG_SIZE)  # (발행 시각, 결과)
        self._yolo_latency_ms: float | None = None

        self._pipeline_messages: dict[str, tuple[float, dict] | None] = dict.fromkeys(PIPELINE_MESSAGE_KINDS)  # (발행 시각, 항목)

        self._has_active_session = False
        self._active_session_id: str | None = None
        self._active_seen_at_sec: float | None = None
        self._retired_session_ids: OrderedDict[str | None, None] = OrderedDict()  # 순서 있는 집합

        self._needs_clock_anchor = False
        self._clock_offset_sec = 0.0
        self._last_raw_time_sec: float | None = None
        self._last_sim_time_sec: float | None = None

    # --- 브라우저 → Python: 텔레메트리·AI 모드·이벤트 ---

    def update_from_browser(
        self,
        telemetry: Telemetry,
        ai_mode: bool,
        events: list[dict],
        operator_link: bool = True,
        session_id: str | None = None,
    ) -> bool:
        """활성 세션이면 상태를 갱신하고 True. 비활성 세션이면 아무것도 바꾸지 않고 False."""
        with self._condition:
            received_at_sec = self._clock()
            if not self._accept_session(session_id, received_at_sec):
                return False

            telemetry = self._to_sim_time(telemetry)
            self._telemetry = telemetry
            self._telemetry_received_at_sec = received_at_sec
            self._ai_mode = ai_mode
            # 호출자가 나중에 dict 를 고쳐도 저장본이 바뀌지 않게 복사한다
            for event in events:
                stored = self._event_to_sim_time(dict(event))
                if stored.get("kind") == HEART_SHOT_EVENT:
                    self._heart_shots.append(stored)
                else:
                    self._events.append(stored)

            # 운용자 링크가 끊긴 POST 는 heartbeat 로 치지 않는다 — 브리지 연결은 위에서 그대로 갱신된다 (D-01)
            if operator_link:
                self._operator_heartbeat_at_sec = received_at_sec
                self._operator_heartbeat_time_sec = telemetry.time_sec

            return True

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

    def put_frame(self, frame: BrowserFrame, session_id: str | None = None) -> bool:
        """활성 세션 프레임이면 슬롯에 넣고 True. 비활성 세션이거나 닫혔으면 버리고 False."""
        with self._condition:
            if self._closed:
                return False
            if not self._accept_session(session_id, self._clock()):
                return False

            telemetry = self._to_sim_time(frame.telemetry)
            frame = dataclasses.replace(frame, timestamp_sec=telemetry.time_sec, telemetry=telemetry)
            self._frame = frame
            self._frame_received_times.append(frame.received_at_sec)
            self._condition.notify_all()
            return True

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

    # --- 단일 세션·sim 시계 보정 (호출자가 _condition 을 잡고 부른다) ---

    def _accept_session(self, session_id: str | None, now_sec: float) -> bool:
        if self._has_active_session and session_id == self._active_session_id:
            self._active_seen_at_sec = now_sec
            return True

        is_new_session = session_id not in self._retired_session_ids
        is_active_silent = self._active_seen_at_sec is None or now_sec - self._active_seen_at_sec > LINK_TIMEOUT_SEC
        if self._has_active_session and not is_new_session and not is_active_silent:
            return False

        self._activate_session(session_id, now_sec)
        return True

    def _activate_session(self, session_id: str | None, now_sec: float) -> None:
        if self._has_active_session:
            self._retired_session_ids[self._active_session_id] = None
            while len(self._retired_session_ids) > RETIRED_SESSION_LIMIT:
                self._retired_session_ids.popitem(last=False)
            self._pending_commands = []  # 옛 탭에 보내려던 명령 — 새 탭에 배달하지 않는다
            self._needs_clock_anchor = True

        self._retired_session_ids.pop(session_id, None)
        self._has_active_session = True
        self._active_session_id = session_id
        self._active_seen_at_sec = now_sec

    def _to_sim_time(self, telemetry: Telemetry) -> Telemetry:
        """페이지 시각을 보정 시계로 — 세션 교체·시계 재시작이면 마지막 보정 시각에서 이어지게 오프셋을 다시 잡는다."""
        raw_sec = telemetry.time_sec
        is_rewound = self._last_raw_time_sec is not None and raw_sec < self._last_raw_time_sec - CLOCK_REWIND_TOLERANCE_SEC
        if (self._needs_clock_anchor or is_rewound) and self._last_sim_time_sec is not None:
            self._clock_offset_sec = self._last_sim_time_sec - raw_sec
        self._needs_clock_anchor = False

        sim_sec = raw_sec + self._clock_offset_sec
        self._last_raw_time_sec = raw_sec
        self._last_sim_time_sec = sim_sec if self._last_sim_time_sec is None else max(self._last_sim_time_sec, sim_sec)

        if self._clock_offset_sec == 0.0:
            return telemetry
        return dataclasses.replace(telemetry, time_sec=sim_sec)

    def _event_to_sim_time(self, event: dict) -> dict:
        if "time_sec" in event and self._clock_offset_sec != 0.0:
            event["time_sec"] = event["time_sec"] + self._clock_offset_sec
        return event

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
            self._detection_log.append((self._clock(), {**frame_dict, "seq": seq, "latency_ms": latency_ms}))

    def latest_detections(self) -> dict | None:
        with self._condition:
            return self._detections

    def latest_browser_detections(self) -> dict | None:
        """/sim/telemetry 응답용 — seq 가 있으면 탐지 dict 에 "seq" 를 붙인다. Frame 인터페이스는 그대로 둔다."""
        with self._condition:
            if self._detections is None or self._detections_seq is None:
                return self._detections
            return {**self._detections, "seq": self._detections_seq}

    def recent_detections(self) -> list[dict]:
        """TTL 안의 최근 YOLO 결과(오래된 것부터) — 페이지가 frame_id 로 중복을 걸러 프레임마다 로그를 남긴다."""
        with self._condition:
            oldest_sec = self._clock() - DETECTION_LOG_TTL_SEC
            return [dict(entry) for published_at_sec, entry in self._detection_log if published_at_sec >= oldest_sec]

    # --- Python → 브라우저: 파이프라인 state·report (운용자 콘솔 대신 페이지가 받는다) ---

    def publish_pipeline_message(self, kind: str, entry: dict) -> None:
        """kind 별 최신 한 칸만 둔다 — 페이지는 폴링마다 최신값만 본다."""
        if kind not in PIPELINE_MESSAGE_KINDS:
            raise ValueError(f"kind must be one of {PIPELINE_MESSAGE_KINDS}, got {kind!r}")
        if not isinstance(entry, dict):
            raise TypeError(f"entry must be a dict, got {type(entry).__name__}")

        with self._condition:
            self._pipeline_messages[kind] = (self._clock(), dict(entry))

    def pipeline_messages(self) -> dict[str, dict | None]:
        """kind 별 최신 항목. TTL 이 지났으면 None."""
        with self._condition:
            oldest_sec = self._clock() - PIPELINE_MESSAGE_TTL_SEC
            return {
                kind: None if stored is None or stored[0] < oldest_sec else dict(stored[1])
                for kind, stored in self._pipeline_messages.items()
            }

    def status(self) -> dict:
        with self._condition:
            window_start_sec = self._clock() - FRAME_FPS_WINDOW_SEC
            while self._frame_received_times and self._frame_received_times[0] < window_start_sec:
                self._frame_received_times.popleft()

            return {
                "frame_fps": len(self._frame_received_times) / FRAME_FPS_WINDOW_SEC,
                "yolo_latency_ms": self._yolo_latency_ms,
            }
