"""선택기 주도 임무 루프 — 이륙 -> (선택기가 고른 rule_mode)* -> return_home 착륙 (RM-07, D-14, D-16).

구성에 무관하다. FlightControl / MissionFrameSource / DecisionModel Protocol 에만 의존하고
mock·browser 클래스를 모른다. 시간은 clock() 으로 읽고 advance(dt) 로 한 틱씩 흘려보낸다.

틱 순서 (tick_hz 마다 한 번):
| 단계 | 하는 일                                                                                  |
|------|------------------------------------------------------------------------------------------|
| a    | now = clock(), 최신 텔레메트리                                                            |
| b    | 새 프레임이 있으면 TrackAggregator 에 넣고 track 스냅샷, 발사 지점 반경 안 새 track 을 hearted 로 |
| c    | 실행 중인 모드를 step(기록 프록시 경유). 결과가 나오면 mode_results 에 담고 모드를 비운다    |
| d    | 이번 틱의 모드 결과를 trigger.poll 에 넘긴다                                              |
| e    | return_home 결과 처리: success -> 착륙 종료, failure -> 비정상 종료, timeout -> 한 번 재시도 |
| f    | max_mission_sec 에 이르면 실행 중 모드를 버리고 선택기 없이 return_home 강제 시작           |
| g    | 실행 중 모드가 없고 사건이 있으면 DecisionModel.select -> create_rule_mode                  |
| h    | 모드 실행 중 사건(new_target / periodic_check)은 모드를 끊지 않는다                         |
| i    | should_abort / 강제 정지 확인, advance(dt)                                                 |

하트 기록 (D-13, D-14, SC5):
- 모드에는 원본 FlightControl 대신 HeartRecordingControl 프록시를 넘긴다. 수락된 FireHeart 를 보면 그 track 을
  hearted 로 기록한다 — 연사 중 한 발이라도 수락되면 hearted 다. 모드는 프록시인지 모른다.
- 기록 순간 그 track 의 지면 위치를 저장하고, 매 틱 저장 위치에서 hearted_radius_m 안의 새 track(ID 바뀜)도
  hearted 로 넣는다. 판정은 이 모듈에만 있고 선택기·보기 필터는 상태의 hearted_tracks 만 본다.

종료 사유:
| end_reason            | 뜻                                                                  |
|-----------------------|---------------------------------------------------------------------|
| landed                | return_home success (착지)                                          |
| takeoff_rejected      | TakeOff 명령 거절                                                   |
| takeoff_timeout       | takeoff_timeout_sec 안에 이륙 완료 못 함                            |
| return_failed         | return_home failure, 또는 재시도한 return_home 도 success 가 아님     |
| hard_stop             | max_mission_sec + return_grace_sec 초과 (끝나지 않는 루프 방지)      |
| aborted               | should_abort 훅이 참 (예: 브라우저 연결 끊김)                        |
"""

import json
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

EVENT_MAX_MISSION_TIME = "max_mission_time"
EVENT_RETURN_RETRY = "return_home_retry"
SOURCE_FORCED = "forced"

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
    max_mission_sec: float = 300.0
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
    event_kind: str
    rule_mode: str
    params: dict
    source: str
    fallback_reason: str
    mode_result: ModeResult | None  # 이 선택을 부른 mode_ended 사건의 결과


@dataclass(frozen=True, slots=True)
class MissionRunResult:
    end_reason: str
    selections: tuple[SelectionRecord, ...]
    mode_results: tuple[ModeResult, ...]
    final_flight_mode: str
    elapsed_sec: float
    forced_return: bool

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

        self._trigger = SelectionTrigger(config.periodic_interval_sec)
        self._tick_sec = 1.0 / config.tick_hz
        self._start_sec = 0.0
        self._running_mode: RuleMode | None = None
        self._heart_control = HeartRecordingControl(flight_control, self._record_heart)
        self._latest_tracks: tuple[Track, ...] = ()
        self._hearted_track_ids: list[int] = []  # 기록 순서 유지
        self._hearted_positions: list[tuple[float, float]] = []  # 발사 순간 (north_m, east_m)
        self._selections: list[SelectionRecord] = []
        self._mode_results: list[ModeResult] = []
        self._forced_return = False
        self._has_retried_return = False

    def run(self) -> MissionRunResult:
        self._start_sec = self._clock()

        end_reason = self._take_off()
        if end_reason is None:
            end_reason = self._fly_mission()

        return self._finish(end_reason)

    # --- 이륙 ---

    def _take_off(self) -> str | None:
        ack = self._flight_control.send_command(TakeOff(self._settings.search_altitude_m))
        if not ack.accepted:
            logger.warning("takeoff rejected: %s", ack.reason)
            return END_TAKEOFF_REJECTED

        while True:
            now_sec = self._clock()
            telemetry = self._flight_control.get_telemetry()
            self._observe(now_sec, telemetry)

            if self._is_airborne(telemetry):
                return None
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
            frame, frame_telemetry, tracks = self._observe(now_sec, telemetry)

            mode_result = self._step_running_mode(now_sec, telemetry, tracks, frame, frame_telemetry)

            state = build_selection_state(
                now_sec, telemetry, tracks, self._settings.target_class, self._get_hearted_labels(),
                target_upper_color=self._settings.target_upper_color,
            )
            event = self._trigger.poll(now_sec, state, mode_result)

            end_reason = self._handle_return_home_result(now_sec, state, mode_result)
            if end_reason is not None:
                return end_reason

            self._force_return_if_overdue(now_sec, state)
            self._select_next_mode(now_sec, state, event)

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

        return frame, frame_telemetry, self._latest_tracks

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

    def _force_return_if_overdue(self, now_sec: float, state: SelectionState) -> None:
        is_overdue = now_sec - self._start_sec >= self._config.max_mission_sec
        is_returning = self._running_mode is not None and self._running_mode.rule_mode == "return_home"
        if not is_overdue or self._forced_return or is_returning:
            return

        if self._running_mode is not None:
            logger.info("mode_abandoned t=%.1f mode=%s reason=max_mission_time", now_sec, self._running_mode.rule_mode)
            self._running_mode = None
        self._forced_return = True
        self._start_forced_return(now_sec, state, EVENT_MAX_MISSION_TIME)

    def _start_forced_return(self, now_sec: float, state: SelectionState, event_kind: str) -> None:
        params = fill_params("return_home", state, SelectionEvent(EVENT_PERIODIC_CHECK, None), self._param_defaults)
        self._running_mode = self._mode_factory("return_home", params, self._settings)
        record = SelectionRecord(now_sec, event_kind, "return_home", params, SOURCE_FORCED, "none", None)
        self._record_selection(record)

    def _select_next_mode(self, now_sec: float, state: SelectionState, event: SelectionEvent | None) -> None:
        if event is None:
            return
        if self._running_mode is not None:
            logger.debug("event %s ignored while %s is running", event.kind, self._running_mode.rule_mode)
            return

        result = self._decision_model.select(state, event)
        record = SelectionRecord(
            now_sec, event.kind, result.rule_mode, result.params, result.source, result.fallback_reason, event.mode_result
        )
        self._record_selection(record)
        self._running_mode = self._mode_factory(result.rule_mode, result.params, self._settings)

    # --- 하트 기록 (D-13, D-14) ---

    def _record_heart(self, track_id: int) -> None:
        if track_id in self._hearted_track_ids:
            return  # 연사의 두 번째 발부터는 이미 기록돼 있다

        self._hearted_track_ids.append(track_id)
        track = next((track for track in self._latest_tracks if track.track_id == track_id), None)
        if track is None:
            position_text = "north=none east=none"  # 스냅샷에 없으면 위치 없이 id 만 기록한다
        else:
            self._hearted_positions.append((track.north_m, track.east_m))
            position_text = f"north={track.north_m:.1f} east={track.east_m:.1f}"

        logger.info("%s t=%.1f target=%s %s", HEART_LOG_PREFIX, self._clock(), track_label(track_id), position_text)

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

    def _record_selection(self, record: SelectionRecord) -> None:
        self._selections.append(record)
        logger.info(
            "%s t=%.1f event=%s mode=%s params=%s source=%s fallback=%s",
            SELECTION_LOG_PREFIX, record.time_sec, record.event_kind, record.rule_mode,
            json.dumps(record.params, separators=(",", ":")), record.source, record.fallback_reason,
        )

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
        )
        logger.info(
            "mission finished: end_reason=%s elapsed=%.1f selections=%d mode_results=%d final_flight_mode=%s forced_return=%s",
            result.end_reason, result.elapsed_sec, len(result.selections), len(result.mode_results),
            result.final_flight_mode, result.forced_return,
        )

        return result
