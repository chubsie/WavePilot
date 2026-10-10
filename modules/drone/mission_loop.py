"""선택기 주도 임무 루프 — 이륙 -> (선택기가 고른 rule_mode)* -> return_home 착륙 (RM-07, D-14, D-16).

구성에 무관하다. FlightControl / MissionFrameSource / DecisionModel / OperatorInput Protocol 에만 의존하고
mock·browser 클래스를 모른다. 시간은 clock() 으로 읽고 advance(dt) 로 한 틱씩 흘려보낸다.

운용 모드 (Phase 5, MIS-02, MIS-03 — ModeStateMachine):
- 시작은 MANUAL 이다. 운용자가 AI 를 요청한 틱에 MANUAL→AI(operator) 로 바뀌고 이륙한다.
- 이륙 중 MANUAL 이면 일시정지(명령 없음)하고, AI 로 돌아오면 TakeOff 를 다시 보낸다.
- AI 또는 RETURN 중 운용자가 요청을 끊으면 그 틱에 MANUAL 이 되고 실행 중 모드를 버린다 — MANUAL 동안은
  어떤 FlightControl 명령도 보내지 않는다(D-07, D-09). 하트 기록·위치·경과 시간은 그대로 둔다.
- MANUAL→AI 재개 틱에는 안전 감시가 먼저 돈다. 비상이 남아 있으면 선택 없이 곧바로 RETURN, 아니면 같은 틱에
  선택기가 새로 고른다(D-08, D-10). 하트 기록이 남아 있으므로 이미 하트를 보낸 사람에게 다시 쏘지 않는다.
- 안전 감시(SafetyMonitor)는 선택기 종류를 모른다. AI 중 SAFETY_REASONS(link_lost, gps_lost, gps_jump, battery,
  geofence, max_flight_time) 중 하나가 나오면 AI→RETURN 이 되고, 선택기 없이 return_home 을 강제로 돌린다(D-05, D-06).
  RETURN 중에는 선택기를 부르지 않고, 링크가 다시 붙어도 복귀를 계속한다(D-04).
- 선택기가 스스로 고른 return_home 은 AI 상태 그대로다(정상 임무 종료). RETURN 은 강제 복귀 전용이다.

틱 순서 (tick_hz 마다 한 번):
| 단계 | 하는 일                                                                                        |
|------|------------------------------------------------------------------------------------------------|
| a    | now = clock(), 최신 텔레메트리, 운용자 링크 상태(틱마다 한 번만 읽는다)                          |
| b    | 새 프레임이 있으면 TrackAggregator 에 넣고 track 스냅샷, 발사 지점 반경 안 새 track 을 hearted 로.  |
|      | 새 활성 track·색 변화가 있으면 detection 로그                                                    |
| c    | 운용자 전환. MANUAL 로 가면 실행 중 모드를 버린다(명령 중단)                                      |
| d    | AI/RETURN 일 때만 실행 중 모드를 step(기록 프록시 경유). 결과가 나오면 모드를 비운다              |
| e    | AI 일 때만 이번 틱의 모드 결과를 trigger.poll 에 넘긴다                                           |
| f    | return_home 결과 처리: success -> 착륙 종료, failure -> 비정상 종료, timeout -> 한 번 재시도       |
| g    | 안전 감시 check (매 틱, 모드와 무관하게 이력 갱신). AI 이고 사유가 있으면 AI→RETURN, 강제 복귀     |
| h    | AI 이고 실행 중 모드가 없을 때만 선택. 재개 틱에 사건이 없으면 periodic_check 로 즉시 고른다       |
| i    | link 변화 로그, on_status, should_abort / 강제 정지 확인, advance(dt)                             |

하트 기록 (D-13, D-14, SC5):
- 모드에는 원본 FlightControl 대신 HeartRecordingControl 프록시를 넘긴다. 수락된 FireHeart 를 보면 그 track 을
  hearted 로 기록한다 — 연사 중 한 발이라도 수락되면 hearted 다. 모드는 프록시인지 모른다.
- 기록 순간 그 track 의 지면 위치를 저장하고, 매 틱 저장 위치에서 hearted_radius_m 안의 새 track(ID 바뀜)도
  hearted 로 넣는다. 판정은 이 모듈에만 있고 선택기·보기 필터는 상태의 hearted_tracks 만 본다.

구조화 로그 (LOG-01, D-11): MissionLog 에 mode_transition·selection·detection·heart·return·safety·link·mission 을
남긴다. 콘솔 접두사 selection / heart 는 MissionLog 가, heart_duplicate / mode_end / mode_abandoned 는 이 모듈
logger 가 낸다. MissionLog 는 만든 쪽(run_mission·테스트)이 닫는다.

종료 사유:
| end_reason            | 뜻                                                                         |
|-----------------------|----------------------------------------------------------------------------|
| landed                | return_home success (착지) — 선택기가 골랐든 강제 복귀든 같다                |
| takeoff_rejected      | TakeOff 명령 거절                                                          |
| takeoff_timeout       | run 시작부터 takeoff_timeout_sec 안에 이륙 완료 못 함 (MANUAL 대기 포함)     |
| return_failed         | return_home failure, 또는 재시도한 return_home 도 success 가 아님            |
| hard_stop             | max_mission_sec + return_grace_sec 초과 (끝나지 않는 루프 방지). 보통은 안전 |
|                       | 감시 max_flight_time 이 먼저 복귀시킨다                                     |
| aborted               | should_abort 훅이 참 (예: 브라우저 연결 끊김)                               |

위험 기록: GPS 끊김·점프 중 복귀 항법은 텔레메트리 NED(north_m/east_m)를 쓴다. sim 에서는 NED 가 참값이라
그대로 홈에 돌아오지만, 실기에서는 GPS 없이 관성·기압 추정으로 NED 를 유지해야 한다(Phase 5 범위 밖).
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from modules.decision.interface import DecisionModel
from modules.decision.interface.state import EVENT_PERIODIC_CHECK, ModeResult, SelectionEvent, SelectionState
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.trigger import PERIODIC_CHECK_INTERVAL_SEC, SelectionTrigger
from modules.drone.selection_state import build_selection_state
from modules.flight_control.interface import Command, CommandAck, FireHeart, FlightControl, TakeOff, Telemetry
from modules.mission.mission_log import (
    KIND_DETECTION,
    KIND_HEART,
    KIND_LINK,
    KIND_MISSION,
    KIND_MODE_TRANSITION,
    KIND_RETURN,
    KIND_SAFETY,
    KIND_SELECTION,
    MissionLog,
)
from modules.mission.mode_machine import MODE_AI, MODE_MANUAL, MODE_RETURN, ModeStateMachine, ModeTransition
from modules.mission.operator import LinkStatus, OperatorInput, ScriptedOperator
from modules.mission.safety import SafetyConfig, SafetyMonitor
from modules.perception.tracking import TrackAggregator
from modules.perception.interface import Track
from modules.rule_mode.factory import create_rule_mode
from modules.rule_mode.interface import ModeParams, ModeTick, RuleMode, RuleModeSettings, SearchArea, track_label
from modules.yolo_detection.interface import Frame

logger = logging.getLogger("modules.drone.mission_loop")

SELECTION_LOG_PREFIX = "selection"
MODE_END_LOG_PREFIX = "mode_end"
HEART_LOG_PREFIX = "heart"
HEART_DUPLICATE_LOG_PREFIX = "heart_duplicate"

AIRBORNE_TOLERANCE_M = 1.0

EVENT_RETURN_RETRY = "return_home_retry"
SOURCE_FORCED = "forced"
RETURN_REASON_SELECTED = "selector"  # 선택기가 return_home 을 골랐을 때의 return 로그 사유

END_LANDED = "landed"
END_TAKEOFF_REJECTED = "takeoff_rejected"
END_TAKEOFF_TIMEOUT = "takeoff_timeout"
END_RETURN_FAILED = "return_failed"
END_HARD_STOP = "hard_stop"
END_ABORTED = "aborted"


class MissionFrameSource(Protocol):
    def read_frame(self) -> Frame | None:
        """이번 틱에 새 프레임이 없으면 None. 오래 막으면 안 된다."""
        ...

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None: ...


@dataclass(frozen=True, slots=True)
class MissionConfig:
    max_mission_sec: float = 300.0  # 기본 SafetyConfig.max_flight_sec 와 hard_stop 기준
    tick_hz: float = 10.0
    takeoff_timeout_sec: float = 30.0
    return_grace_sec: float = 300.0  # 강제 정지 = max_mission_sec + return_grace_sec
    periodic_interval_sec: float = PERIODIC_CHECK_INTERVAL_SEC
    hearted_radius_m: float = 3.0  # 발사 지점에서 이 거리 안의 새 track 은 같은 사람으로 본다 (D-13)

    def __post_init__(self) -> None:
        for name in (
            "max_mission_sec", "tick_hz", "takeoff_timeout_sec", "return_grace_sec", "periodic_interval_sec",
            "hearted_radius_m",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"field '{name}' must be a finite number > 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    time_sec: float
    event_kind: str  # 선택 사건 종류, 강제 복귀면 안전 사유 또는 return_home_retry
    rule_mode: str
    params: dict
    source: str
    fallback_reason: str
    mode_result: ModeResult | None  # 이 선택을 부른 mode_ended 사건의 결과
    confidence: float | None = None  # 선택기 확신도, 강제 복귀면 None


@dataclass(frozen=True, slots=True)
class MissionRunResult:
    end_reason: str
    selections: tuple[SelectionRecord, ...]
    mode_results: tuple[ModeResult, ...]
    final_flight_mode: str
    elapsed_sec: float
    forced_return: bool
    return_reason: str | None = None  # 첫 강제 RETURN 사유 (SAFETY_REASONS)
    transitions: tuple[ModeTransition, ...] = ()

    @property
    def is_success(self) -> bool:
        return self.end_reason == END_LANDED


def build_rule_mode_settings(
    defaults: ParamDefaults, target_class: str = "person", target_upper_color: str | None = None
) -> RuleModeSettings:
    return RuleModeSettings(
        target_class=target_class,
        target_upper_color=target_upper_color,
        search_area=SearchArea(
            north_min_m=defaults.search_area_north_min_m,
            north_max_m=defaults.search_area_north_max_m,
            east_min_m=defaults.search_area_east_min_m,
            east_max_m=defaults.search_area_east_max_m,
        ),
        search_altitude_m=defaults.search_altitude_m,
        search_lane_spacing_m=defaults.search_lane_spacing_m,
    )


class HeartRecordingControl:
    """FlightControl 을 그대로 감싸고, 수락된 FireHeart 의 track_id 를 on_heart_accepted 로 알린다 (D-14, SC5)."""

    def __init__(self, inner: FlightControl, on_heart_accepted: Callable[[int], None]) -> None:
        self._inner = inner
        self._on_heart_accepted = on_heart_accepted

    def get_telemetry(self) -> Telemetry:
        return self._inner.get_telemetry()

    def send_command(self, command: Command) -> CommandAck:
        ack = self._inner.send_command(command)
        if isinstance(command, FireHeart) and ack.accepted:
            self._on_heart_accepted(command.track_id)

        return ack


class MissionRunner:
    def __init__(
        self,
        flight_control: FlightControl,
        frame_source: MissionFrameSource,
        aggregator: TrackAggregator,
        decision_model: DecisionModel,
        param_defaults: ParamDefaults,
        settings: RuleModeSettings,
        clock: Callable[[], float],
        advance: Callable[[float], None],
        config: MissionConfig = MissionConfig(),
        mode_factory: Callable[[str, ModeParams, RuleModeSettings], RuleMode] = create_rule_mode,
        should_abort: Callable[[], bool] | None = None,
        operator: OperatorInput | None = None,
        safety: SafetyMonitor | None = None,
        mission_log: MissionLog | None = None,
        on_status: Callable[[dict], None] | None = None,
    ) -> None:
        self._flight_control = flight_control
        self._frame_source = frame_source
        self._aggregator = aggregator
        self._decision_model = decision_model
        self._param_defaults = param_defaults
        self._settings = settings
        self._clock = clock
        self._advance = advance
        self._config = config
        self._mode_factory = mode_factory
        self._should_abort = should_abort
        self._operator = operator if operator is not None else ScriptedOperator()
        self._safety = safety if safety is not None else SafetyMonitor(
            SafetyConfig(
                search_area=settings.search_area,
                max_flight_sec=config.max_mission_sec,
                home_north_m=param_defaults.home_north_m,
                home_east_m=param_defaults.home_east_m,
            )
        )
        self._mission_log = mission_log if mission_log is not None else MissionLog(path=None, console=logger)
        self._on_status = on_status

        self._trigger = SelectionTrigger(config.periodic_interval_sec)
        self._tick_sec = 1.0 / config.tick_hz
        self._start_sec = 0.0
        self._takeoff_sec: float | None = None  # 첫 TakeOff 수락 시각 — 비행 경과 기준 (MANUAL 포함)
        self._running_mode: RuleMode | None = None
        self._heart_control = HeartRecordingControl(flight_control, self._record_heart)
        self._latest_tracks: tuple[Track, ...] = ()
        self._hearted_track_ids: list[int] = []  # 기록 순서 유지
        self._hearted_positions: list[tuple[float, float]] = []  # 발사 순간 (north_m, east_m)
        self._selections: list[SelectionRecord] = []
        self._mode_results: list[ModeResult] = []
        self._forced_return = False
        self._has_retried_return = False

        self._modes = ModeStateMachine()
        self._transitions: list[ModeTransition] = []
        self._return_reason: str | None = None

        self._last_link_connected: bool | None = None
        self._last_safety_reason: str | None = None
        self._logged_track_colors: dict[int, str | None] = {}

    def run(self) -> MissionRunResult:
        self._start_sec = self._clock()

        end_reason = self._take_off()
        if end_reason is None:
            end_reason = self._fly_mission()

        return self._finish(end_reason)

    # --- 이륙 ---

    def _take_off(self) -> str | None:
        """AI 인 동안 TakeOff 를 보내고 airborne 을 기다린다. MANUAL 이면 일시정지하고 재개 때 다시 보낸다.

        takeoff_timeout_sec 는 run 시작부터 센다 — MANUAL 대기도 포함해 끝나지 않는 대기를 막는다.
        이륙 중 안전 감시가 RETURN 을 걸면 강제 return_home 을 시작한 채 임무 루프로 넘긴다(None).
        """
        is_takeoff_sent = False

        while True:
            now_sec = self._clock()
            telemetry = self._flight_control.get_telemetry()
            link = self._operator.link_status(now_sec)

            _, _, tracks = self._observe(now_sec, telemetry)

            self._apply_operator(now_sec)
            if self._modes.mode == MODE_MANUAL:
                is_takeoff_sent = False  # 재개 때 TakeOff 를 다시 보낸다
            elif not is_takeoff_sent:
                ack = self._flight_control.send_command(TakeOff(self._settings.search_altitude_m))
                if not ack.accepted:
                    logger.warning("takeoff rejected: %s", ack.reason)
                    return END_TAKEOFF_REJECTED
                is_takeoff_sent = True
                if self._takeoff_sec is None:
                    self._takeoff_sec = now_sec

            state = self._build_state(now_sec, telemetry, tracks)
            if self._check_safety(now_sec, telemetry, link, state):
                return None

            if self._modes.mode == MODE_AI and self._is_airborne(telemetry):
                return None

            self._record_link(now_sec, link)
            self._publish_status(now_sec, link)
            if now_sec - self._start_sec >= self._config.takeoff_timeout_sec:
                return END_TAKEOFF_TIMEOUT
            if self._should_abort is not None and self._should_abort():
                return END_ABORTED

            self._advance(self._tick_sec)

    def _is_airborne(self, telemetry: Telemetry) -> bool:
        reached_altitude = telemetry.altitude_m >= self._settings.search_altitude_m - AIRBORNE_TOLERANCE_M

        return telemetry.flight_mode == "hover" and reached_altitude

    # --- 임무 틱 ---

    def _fly_mission(self) -> str:
        while True:
            now_sec = self._clock()
            telemetry = self._flight_control.get_telemetry()
            link = self._operator.link_status(now_sec)

            frame, frame_telemetry, tracks = self._observe(now_sec, telemetry)

            transition = self._apply_operator(now_sec)
            is_resumed = transition is not None and transition.to_mode == MODE_AI

            mode_result = None
            if self._modes.mode in (MODE_AI, MODE_RETURN):
                mode_result = self._step_running_mode(now_sec, telemetry, tracks, frame, frame_telemetry)
            state = self._build_state(now_sec, telemetry, tracks)

            event = None
            if self._modes.mode == MODE_AI:
                event = self._trigger.poll(now_sec, state, mode_result)

            end_reason = self._handle_return_home_result(now_sec, state, mode_result)
            if end_reason is not None:
                return end_reason

            self._check_safety(now_sec, telemetry, link, state)

            if self._modes.mode == MODE_AI:
                if is_resumed and event is None and self._running_mode is None:
                    event = SelectionEvent(EVENT_PERIODIC_CHECK, None)  # 재개 즉시 새로 고른다 (D-08)
                self._select_next_mode(now_sec, state, event)

            self._record_link(now_sec, link)
            self._publish_status(now_sec, link)
            if self._should_abort is not None and self._should_abort():
                return END_ABORTED
            if now_sec - self._start_sec >= self._config.max_mission_sec + self._config.return_grace_sec:
                return END_HARD_STOP

            self._advance(self._tick_sec)

    def _observe(
        self, now_sec: float, telemetry: Telemetry
    ) -> tuple[Frame | None, Telemetry | None, tuple]:
        frame = self._frame_source.read_frame()
        frame_telemetry = None
        if frame is not None:
            frame_telemetry = self._frame_source.telemetry_for(frame.timestamp_sec) or telemetry
            self._aggregator.update(frame, frame_telemetry)

        self._latest_tracks = tuple(self._aggregator.snapshot(now_sec))
        self._mark_hearted_by_position(now_sec, self._latest_tracks)
        self._record_detections(now_sec, self._latest_tracks)

        return frame, frame_telemetry, self._latest_tracks

    def _build_state(self, now_sec: float, telemetry: Telemetry, tracks: tuple) -> SelectionState:
        return build_selection_state(
            now_sec, telemetry, tracks, self._settings.target_class, self._get_hearted_labels(),
            target_upper_color=self._settings.target_upper_color,
        )

    def _step_running_mode(
        self,
        now_sec: float,
        telemetry: Telemetry,
        tracks: tuple,
        frame: Frame | None,
        frame_telemetry: Telemetry | None,
    ) -> ModeResult | None:
        if self._running_mode is None:
            return None

        tick = ModeTick(
            now_sec=now_sec,
            telemetry=telemetry,
            tracks=tracks,
            frame=frame,
            frame_telemetry=frame_telemetry,
            hearted_track_ids=self._get_hearted_track_ids(),
        )
        result = self._running_mode.step(tick, self._heart_control)
        if result is not None:
            self._running_mode = None  # 끝난 모드는 다시 step 하지 않는다
            self._record_mode_result(now_sec, result)

        return result

    def _handle_return_home_result(self, now_sec: float, state: SelectionState, mode_result: ModeResult | None) -> str | None:
        if mode_result is None or mode_result.rule_mode != "return_home":
            return None
        if mode_result.outcome == "success":
            return END_LANDED
        if mode_result.outcome == "failure" or self._has_retried_return:
            return END_RETURN_FAILED

        self._has_retried_return = True
        self._start_forced_return(now_sec, state, EVENT_RETURN_RETRY)

        return None

    def _select_next_mode(self, now_sec: float, state: SelectionState, event: SelectionEvent | None) -> None:
        if event is None:
            return
        if self._running_mode is not None:
            logger.debug("event %s ignored while %s is running", event.kind, self._running_mode.rule_mode)
            return

        result = self._decision_model.select(state, event)
        record = SelectionRecord(
            now_sec, event.kind, result.rule_mode, result.params, result.source, result.fallback_reason,
            event.mode_result, confidence=result.confidence,
        )
        self._record_selection(record)
        self._running_mode = self._mode_factory(result.rule_mode, result.params, self._settings)

        if result.rule_mode == "return_home":
            self._record_return(now_sec, RETURN_REASON_SELECTED, result.params)

    # --- 운용 모드·안전 감시 (MIS-02, MIS-03, GUARD-01..03) ---

    def _apply_operator(self, now_sec: float) -> ModeTransition | None:
        transition = self._modes.on_operator(now_sec, self._operator.is_ai_requested())
        if transition is None:
            return None

        self._record_transition(transition)
        if transition.to_mode == MODE_MANUAL:
            self._abandon_running_mode(now_sec, transition.reason)  # 명령은 보내지 않는다 (D-07, D-09)

        return transition

    def _check_safety(self, now_sec: float, telemetry: Telemetry, link: LinkStatus, state: SelectionState) -> bool:
        """매 틱 안전 감시를 돌리고, AI 중 사유가 나오면 RETURN 으로 바꿔 강제 복귀를 시작한다. 선택기 종류는 보지 않는다(SC6)."""
        flight_elapsed_sec = 0.0 if self._takeoff_sec is None else now_sec - self._takeoff_sec
        verdict = self._safety.check(now_sec, telemetry, flight_elapsed_sec, link)

        verdict_reason = None if verdict is None else verdict.reason
        if verdict is not None and verdict_reason != self._last_safety_reason:
            self._mission_log.record(KIND_SAFETY, now_sec, reason=verdict.reason, detail=verdict.detail)
        self._last_safety_reason = verdict_reason

        transition = self._modes.on_safety(now_sec, verdict)
        if transition is None:
            return False

        self._record_transition(transition)
        self._abandon_running_mode(now_sec, transition.reason)
        self._forced_return = True
        if self._return_reason is None:
            self._return_reason = transition.reason
        self._has_retried_return = False
        self._start_forced_return(now_sec, state, transition.reason)

        return True

    def _start_forced_return(self, now_sec: float, state: SelectionState, event_kind: str) -> None:
        params = fill_params("return_home", state, SelectionEvent(EVENT_PERIODIC_CHECK, None), self._param_defaults)
        self._running_mode = self._mode_factory("return_home", params, self._settings)

        record = SelectionRecord(now_sec, event_kind, "return_home", params, SOURCE_FORCED, "none", None, confidence=None)
        self._record_selection(record)
        self._record_return(now_sec, event_kind, params)

    def _abandon_running_mode(self, now_sec: float, reason: str) -> None:
        if self._running_mode is None:
            return

        logger.info("mode_abandoned t=%.1f mode=%s reason=%s", now_sec, self._running_mode.rule_mode, reason)
        self._running_mode = None

    # --- 하트 기록 (D-13, D-14) ---

    def _record_heart(self, track_id: int) -> None:
        if track_id in self._hearted_track_ids:
            return  # 연사의 두 번째 발부터는 이미 기록돼 있다

        self._hearted_track_ids.append(track_id)
        track = next((track for track in self._latest_tracks if track.track_id == track_id), None)
        north_m = None
        east_m = None
        if track is not None:  # 스냅샷에 없으면 위치 없이 id 만 기록한다
            self._hearted_positions.append((track.north_m, track.east_m))
            north_m = round(track.north_m, 1)
            east_m = round(track.east_m, 1)

        self._mission_log.record(KIND_HEART, self._clock(), target=track_label(track_id), north=north_m, east=east_m)

    def _mark_hearted_by_position(self, now_sec: float, tracks: tuple[Track, ...]) -> None:
        if not self._hearted_positions:
            return

        for track in tracks:
            if not track.is_active or track.track_id in self._hearted_track_ids:
                continue
            if self._is_near_hearted_position(track):
                self._hearted_track_ids.append(track.track_id)  # 위치는 원래 발사 지점만 기준으로 둔다
                logger.info(
                    "%s t=%.1f target=%s reason=near_hearted_position",
                    HEART_DUPLICATE_LOG_PREFIX, now_sec, track_label(track.track_id),
                )

    def _is_near_hearted_position(self, track: Track) -> bool:
        return any(
            math.hypot(track.north_m - north_m, track.east_m - east_m) <= self._config.hearted_radius_m
            for north_m, east_m in self._hearted_positions
        )

    # --- 기록 ---

    def _record_transition(self, transition: ModeTransition) -> None:
        self._transitions.append(transition)
        # from 은 파이썬 예약어라 dict 로 넘긴다 — 콘솔 "mode_transition t=.. from=AI to=RETURN reason=battery"
        self._mission_log.record(
            KIND_MODE_TRANSITION, transition.time_sec,
            **{"from": transition.from_mode, "to": transition.to_mode},
            reason=transition.reason, detail=transition.detail,
        )

    def _record_selection(self, record: SelectionRecord) -> None:
        self._selections.append(record)
        self._mission_log.record(
            KIND_SELECTION, record.time_sec,
            event=record.event_kind, mode=record.rule_mode, params=record.params, source=record.source,
            confidence=record.confidence, fallback=record.fallback_reason,
        )

    def _record_return(self, now_sec: float, reason: str, params: dict) -> None:
        self._mission_log.record(
            KIND_RETURN, now_sec, reason=reason, home_north_m=params["home_north_m"], home_east_m=params["home_east_m"]
        )

    def _record_detections(self, now_sec: float, tracks: tuple[Track, ...]) -> None:
        """새 활성 track 과 색이 바뀐 track 만 기록한다 — 매 프레임 기록하지 않는다."""
        for track in tracks:
            if not track.is_active:
                continue
            is_known = track.track_id in self._logged_track_colors
            if is_known and self._logged_track_colors[track.track_id] == track.upper_color:
                continue

            self._logged_track_colors[track.track_id] = track.upper_color
            self._mission_log.record(
                KIND_DETECTION, now_sec,
                track=track_label(track.track_id), class_name=track.class_name, upper_color=track.upper_color,
                north=round(track.north_m, 1), east=round(track.east_m, 1), confidence=round(track.mean_confidence, 3),
            )

    def _record_link(self, now_sec: float, link: LinkStatus) -> None:
        if link.connected == self._last_link_connected:
            return

        self._last_link_connected = link.connected
        self._mission_log.record(KIND_LINK, now_sec, connected=link.connected, last_heartbeat_sec=link.last_heartbeat_sec)

    def _publish_status(self, now_sec: float, link: LinkStatus) -> None:
        if self._on_status is None:
            return

        self._on_status({
            "mode": self._modes.mode,
            "reason": self._transitions[-1].reason if self._transitions else None,
            "link_connected": link.connected,
            "link_last_heartbeat_sec": link.last_heartbeat_sec,
            "running_mode": None if self._running_mode is None else self._running_mode.rule_mode,
            "t": now_sec,
        })

    def _record_mode_result(self, now_sec: float, result: ModeResult) -> None:
        self._mode_results.append(result)
        logger.info(
            "%s t=%.1f mode=%s outcome=%s target=%s",
            MODE_END_LOG_PREFIX, now_sec, result.rule_mode, result.outcome, result.target_track,
        )

    def _get_hearted_labels(self) -> tuple[str, ...]:
        return tuple(track_label(track_id) for track_id in self._hearted_track_ids)

    def _get_hearted_track_ids(self) -> frozenset[int]:
        return frozenset(self._hearted_track_ids)

    def _finish(self, end_reason: str) -> MissionRunResult:
        result = MissionRunResult(
            end_reason=end_reason,
            selections=tuple(self._selections),
            mode_results=tuple(self._mode_results),
            final_flight_mode=self._flight_control.get_telemetry().flight_mode,
            elapsed_sec=self._clock() - self._start_sec,
            forced_return=self._forced_return,
            return_reason=self._return_reason,
            transitions=tuple(self._transitions),
        )
        self._mission_log.record(
            KIND_MISSION, self._clock(),
            event="end", end_reason=result.end_reason, elapsed=round(result.elapsed_sec, 1),
            return_reason=result.return_reason, forced_return=result.forced_return,
        )
        logger.info(
            "mission finished: end_reason=%s elapsed=%.1f selections=%d mode_results=%d final_flight_mode=%s forced_return=%s",
            result.end_reason, result.elapsed_sec, len(result.selections), len(result.mode_results),
            result.final_flight_mode, result.forced_return,
        )

        return result
