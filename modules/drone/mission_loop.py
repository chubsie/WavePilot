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
| h    | AI 일 때 선택. 모드가 없으면 사건이 있을 때 고르고(재개 틱에 사건이 없으면 periodic_check 로 즉시), |
|      | 모드가 있으면 재판단(아래)                                                                       |
| i    | link 변화 로그, on_status, should_abort / 강제 정지 확인, advance(dt)                             |

재판단 ("다르면 전환", redecide_interval_sec 마다 — 기본 1 s, None 이면 끔):
- AI 이고 실행 중 모드가 있으면, 마지막 선택이 끝난 시각에서 간격이 지났거나 사건(new_target·periodic_check)이 난
  틱에 선택기를 다시 부른다. 간격은 select 완료 시각부터 잰다 — d1 이 오래 막혀도 다음 판단 전에 틱이 돈다.
- 결과 (rule_mode, target_track) 이 실행 중 모드와 같으면 그대로 계속한다(모드 step·명령 끊김 없음). 이 판단은
  selection 기록·MissionLog 에 남기지 않고 debug 로그와 on_selection payload("continued": true)만 낸다.
  대상은 '같은 표적'이면 같다고 본다 — 라벨이 같거나, 같은 클래스이고 지면 위치가 SAME_TARGET_RADIUS_M 안이다.
  트래커가 같은 사람에게 새 id 를 주고 approach·aim_and_fire 가 그 id 로 갈아탄 경우다.
  이때 비교 대상 라벨을 새 id 로 옮긴다.
- 대상 모드(approach·observe·aim_and_fire)를 대상 없는 결과(search 등)로 바꾸려 하면, 실행 중 대상이 살아 있는 동안은
  바꾸지 않는다(redecide_ignored reason=target_alive). 살아 있음 = 그 대상(또는 같은 표적)이 여전히 원하는 대상이고
  (클래스·hearted·색) 마지막으로 본 지 TARGET_KEEP_SEC 안. 트래커는 2 s 만 못 봐도 비활성으로 표시해 선택기 보기에서
  빠지지만, 모드는 자기 놓침 판정(유예)을 따로 갖는다 — 놓쳤다는 판단은 모드가 failure 로 끝내며 내리고, 그 mode_ended
  사건에 선택기가 새로 고른다. 선택기 종류(규칙·d1)와 무관하게 루프가 같은 규칙을 쓴다.
- 다르면 실행 중 모드를 버리고(mode_abandoned reason=redecide) 새 모드로 바꾼다 — 보통 선택처럼 기록한다.
- 끊지 않는 모드: aim_and_fire 연사 중(첫 FireHeart 수락 이후) · return_home(선택·강제 모두). 이때는 판단하지 않는다.
- 방금 실패한 (모드, track) 으로는 재판단으로도 바꾸지 않는다 — 선택기 보기 필터는 mode_ended 사건에서만 그 쌍을
  빼므로, 이어지는 periodic 재판단이 같은 실패를 곧바로 다시 고르지 않게 루프가 막는다.

하트 기록 (D-13, D-14, SC5):
- 모드에는 원본 FlightControl 대신 HeartRecordingControl 프록시를 넘긴다. 수락된 FireHeart 를 보면 그 track 을
  hearted 로 기록한다 — 연사 중 한 발이라도 수락되면 hearted 다. 모드는 프록시인지 모른다.
- 기록 순간 그 track 의 지면 위치를 저장하고, 매 틱 저장 위치에서 hearted_radius_m 안의 새 track(ID 바뀜)도
  hearted 로 넣는다. 판정은 이 모듈에만 있고 선택기·보기 필터는 상태의 hearted_tracks 만 본다.

구조화 로그 (LOG-01, D-11): MissionLog 에 mode_transition·selection·detection·heart·return·safety·link·mission 을
남긴다. 콘솔 접두사 selection / heart 는 MissionLog 가, heart_duplicate / mode_end / mode_abandoned 는 이 모듈
logger 가 낸다. MissionLog 는 만든 쪽(run_mission·테스트)이 닫는다.

선택·모드 종료 콜백 (mission_demo LLM Decisions 패널): on_selection / on_mode_end 를 주입하면 선택마다(강제 복귀
포함) build_selection_payload, 모드가 끝날 때마다 build_mode_end_payload 의 JSON 직렬화 가능한 dict 를 넘긴다.
None 이면 아무것도 하지 않는다 — mock 경로는 그대로다. run_mission --sim browser 가 브리지 pipeline 으로 잇는다.

종료 사유:
| end_reason            | 뜻                                                                         |
|-----------------------|----------------------------------------------------------------------------|
| landed                | return_home success (착지) — 선택기가 골랐든 강제 복귀든 같다                |
| takeoff_rejected      | TakeOff 명령 거절                                                          |
| takeoff_timeout       | run 시작부터 takeoff_timeout_sec 안에 이륙 완료 못 함 (MANUAL 대기 포함)     |
| return_failed         | return_home failure, 또는 재시도한 return_home 도 success 가 아님            |
| hard_stop             | max_mission_sec + return_grace_sec 초과 (끝나지 않는 루프 방지). 보통은 안전 |
|                       | 감시 max_flight_time 이 먼저 복귀시킨다                                     |
| aborted               | should_abort 훅이 참 (외부 중단 요청). 브라우저 링크 끊김은 aborted 가 아니다 |

시뮬레이터 링크 일시정지 (is_link_up 훅): 틱 첫머리에 링크가 끊겨 있으면 재연결까지 기다린다 — 명령·선택·안전 감시·
모드 step 을 모두 멈추고, 실행 중 모드와 운용 모드는 그대로 둔다(Hold 를 보내지 않는다: 끊긴 동안 넣은 명령은
재연결 때 늦게 배달될 뿐이라, 아무것도 보내지 않는 쪽이 가장 단순하고 안전하다). 끊김으로는 임무를 끝내지 않고
무기한 기다리며 LINK_WAIT_LOG_INTERVAL_SEC 마다 한 줄 남긴다. 임무 시계는 sim 시각이라 끊긴 동안 흐르지 않는다 —
max_flight_sec·takeoff_timeout·hard_stop 도 그동안 멈춘다(별도 보정 없이 시계 정의만으로 일시정지된다).

시계는 단조 비감소로 읽는다(_now): clock() 이 뒤로 가면 오프셋을 다시 잡아 마지막 값에서 이어 간다 — 안전 감시 창·
경과 시간이 역행하지 않게. 브라우저 경로는 브리지가 이미 보정하므로 이것은 다른 시계 구현을 위한 방어선이다.

위험 기록: GPS 끊김·점프 중 복귀 항법은 텔레메트리 NED(north_m/east_m)를 쓴다. sim 에서는 NED 가 참값이라
그대로 홈에 돌아오지만, 실기에서는 GPS 없이 관성·기압 추정으로 NED 를 유지해야 한다(Phase 5 범위 밖).
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from modules.decision.interface import DecisionModel, SelectionResult
from modules.decision.interface.state import (
    EVENT_PERIODIC_CHECK,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.options import FAILED_OUTCOMES, is_wanted_track
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
from modules.perception.tracking import LOST_AFTER_SEC as TRACK_LOST_AFTER_SEC
from modules.perception.tracking import TrackAggregator
from modules.perception.interface import Track
from modules.rule_mode.approach import LOST_AFTER_SEC as APPROACH_LOST_AFTER_SEC
from modules.rule_mode.factory import create_rule_mode
from modules.rule_mode.interface import (
    SAME_TARGET_RADIUS_M,
    ModeParams,
    ModeTick,
    RuleMode,
    RuleModeSettings,
    SearchArea,
    track_label,
)
from modules.yolo_detection.interface import Frame

logger = logging.getLogger("modules.drone.mission_loop")

SELECTION_LOG_PREFIX = "selection"
MODE_END_LOG_PREFIX = "mode_end"
HEART_LOG_PREFIX = "heart"
HEART_DUPLICATE_LOG_PREFIX = "heart_duplicate"

AIRBORNE_TOLERANCE_M = 1.0
DEFAULT_REDECIDE_INTERVAL_SEC = 1.0  # 실행 중 모드 재판단 간격 (select 완료 시각 기준)
REDECIDE_EPSILON_SEC = 1e-6  # 틱 시각 누적 오차로 간격을 한 틱 늦게 보지 않게
# 재판단이 대상 모드를 대상 없는 결과로 끊지 않는 시간(마지막으로 본 뒤) — 트래커 비활성(2 s) + approach 놓침 유예(3 s).
# 모드의 놓침 판정이 먼저 끝나도록 가장 긴 유예에 맞춘다. 그 전에 모드가 failure 로 끝나면 mode_ended 로 새로 고른다
TARGET_KEEP_SEC = TRACK_LOST_AFTER_SEC + APPROACH_LOST_AFTER_SEC
LINK_WAIT_LOG_INTERVAL_SEC = 10.0  # 링크 대기 중 'waiting for simulator reconnect' 줄 간격(틱 수로 잰 대략값)

EVENT_RETURN_RETRY = "return_home_retry"
SOURCE_FORCED = "forced"
RETURN_REASON_SELECTED = "selector"  # 선택기가 return_home 을 골랐을 때의 return 로그 사유
ABANDON_REASON_REDECIDE = "redecide"

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


def build_selection_payload(record: SelectionRecord, result: SelectionResult | None, continued: bool = False) -> dict:
    """selection pipeline payload. result 가 None 이면 강제 복귀 — 확률·보기·지연이 없다.

    continued 는 재판단 결과가 실행 중 모드와 같아 그대로 계속한다는 뜻 — 페이지가 새 결정 카드로 덮지 않게.
    """
    return {
        "time_sec": record.time_sec,
        "event_kind": record.event_kind,
        "rule_mode": record.rule_mode,
        "params": _to_json_safe(record.params),
        "source": record.source,
        "confidence": record.confidence,
        "probabilities": {} if result is None else dict(result.probabilities),
        "fallback_reason": record.fallback_reason,
        "options": [] if result is None else list(result.options),
        "latency_ms": None if result is None else result.latency.total_ms,
        "mode_result": _mode_result_to_dict(record.mode_result),
        "continued": continued,
    }


def build_mode_end_payload(now_sec: float, result: ModeResult) -> dict:
    return {"time_sec": now_sec, **_mode_result_to_dict(result)}


def _mode_result_to_dict(result: ModeResult | None) -> dict | None:
    if result is None:
        return None
    return {"rule_mode": result.rule_mode, "outcome": result.outcome, "target_track": result.target_track}


def _to_json_safe(value):
    # 튜플은 JSON 배열로 — 페이지가 그대로 읽는다
    if isinstance(value, dict):
        return {key: _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(item) for item in value]
    return value


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
        on_selection: Callable[[dict], None] | None = None,
        on_mode_end: Callable[[dict], None] | None = None,
        is_link_up: Callable[[], bool] | None = None,
        redecide_interval_sec: float | None = DEFAULT_REDECIDE_INTERVAL_SEC,
    ) -> None:
        _validate_redecide_interval(redecide_interval_sec)

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
        self._is_link_up = is_link_up
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
        self._on_selection = on_selection
        self._on_mode_end = on_mode_end

        self._trigger = SelectionTrigger(config.periodic_interval_sec)
        self._tick_sec = 1.0 / config.tick_hz
        self._start_sec = 0.0
        self._last_now_sec: float | None = None
        self._clock_offset_sec = 0.0
        self._takeoff_sec: float | None = None  # 첫 TakeOff 수락 시각 — 비행 경과 기준 (MANUAL 포함)
        self._running_mode: RuleMode | None = None
        self._running_target: str | None = None  # 실행 중 모드를 고른 params 의 target_track — 재판단 비교용
        self._is_burst_started = False  # 실행 중 모드에서 FireHeart 가 한 발이라도 수락됐다
        self._redecide_interval_sec = redecide_interval_sec
        self._last_decide_sec: float | None = None  # 마지막 select 가 끝난 시각
        self._last_failed_choice: tuple[str, str | None] | None = None  # 직전 모드가 실패했으면 그 (모드, track)
        self._heart_control = HeartRecordingControl(flight_control, self._handle_heart_accepted)
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
        self._start_sec = self._now()

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
            if not self._wait_for_link():
                return END_ABORTED

            now_sec = self._now()
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

    # --- 시계·링크 일시정지 ---

    def _now(self) -> float:
        raw_sec = self._clock()
        now_sec = raw_sec + self._clock_offset_sec
        if self._last_now_sec is not None and now_sec < self._last_now_sec:
            # 역행하면 마지막 값에서 이어지게 오프셋을 다시 잡는다 — 값에 묶어 두면 시간이 멈춘 뒤 한꺼번에 흘러
            # 안전 감시가 가짜 gps_jump 를 본다
            self._clock_offset_sec = self._last_now_sec - raw_sec
            now_sec = self._last_now_sec
        self._last_now_sec = now_sec
        return now_sec

    def _wait_for_link(self) -> bool:
        """링크가 끊겨 있으면 다시 붙을 때까지 틱만 흘려보낸다. should_abort 가 참이면 False (끊김 자체로는 끝내지 않는다)."""
        if self._is_link_up is None or self._is_link_up():
            return True

        paused_at_sec = self._now()
        self._mission_log.record(KIND_MISSION, paused_at_sec, event="paused", reason="simulator_link_lost")
        logger.warning("simulator link lost t=%.1f: pausing mission (no commands) until reconnect", paused_at_sec)

        log_every_ticks = max(1, round(LINK_WAIT_LOG_INTERVAL_SEC / self._tick_sec))
        waited_ticks = 0
        while not self._is_link_up():
            if self._should_abort is not None and self._should_abort():
                return False
            self._advance(self._tick_sec)
            waited_ticks += 1
            if waited_ticks % log_every_ticks == 0:
                logger.info("waiting for simulator reconnect (%.0f s)", waited_ticks * self._tick_sec)

        resumed_at_sec = self._now()
        self._mission_log.record(
            KIND_MISSION, resumed_at_sec, event="resumed", waited=round(waited_ticks * self._tick_sec, 1)
        )
        return True

    # --- 임무 틱 ---

    def _fly_mission(self) -> str:
        while True:
            if not self._wait_for_link():
                return END_ABORTED

            now_sec = self._now()
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
                self._decide(now_sec, state, event)

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

    # --- 선택·재판단 ---

    def _decide(self, now_sec: float, state: SelectionState, event: SelectionEvent | None) -> None:
        """AI 틱의 선택 단계. 모드가 없으면 사건에 고르고, 있으면 간격·사건마다 재판단한다("다르면 전환")."""
        if self._running_mode is None:
            if event is not None:
                self._start_selected_mode(now_sec, event, self._select(state, event))
            return

        if not self._is_redecidable():
            if event is not None:
                logger.debug("event %s ignored while %s is running", event.kind, self._running_mode.rule_mode)
            return
        if event is None and not self._is_redecide_due(now_sec):
            return

        redecide_event = event if event is not None else SelectionEvent(EVENT_PERIODIC_CHECK, None)
        if event is None:
            self._trigger.reset_periodic(now_sec)  # 이 재판단이 주기 확인을 겸한다 — 곧이어 periodic_check 가 겹치지 않게
        self._redecide(now_sec, state, redecide_event, self._select(state, redecide_event))

    def _select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        result = self._decision_model.select(state, event)
        self._last_decide_sec = self._now()  # 간격은 select 가 끝난 시각부터 — 느린 선택기가 틱을 굶기지 않게

        return result

    def _is_redecidable(self) -> bool:
        """실행 중 모드를 재판단으로 끊어도 되는지. 연사 중 aim_and_fire 와 return_home 은 끊지 않는다."""
        if self._redecide_interval_sec is None or self._running_mode is None:
            return False

        rule_mode = self._running_mode.rule_mode
        if rule_mode == "return_home":
            return False
        return not (rule_mode == "aim_and_fire" and self._is_burst_started)

    def _is_redecide_due(self, now_sec: float) -> bool:
        if self._redecide_interval_sec is None:
            return False
        if self._last_decide_sec is None:
            return True

        return now_sec - self._last_decide_sec >= self._redecide_interval_sec - REDECIDE_EPSILON_SEC

    def _redecide(self, now_sec: float, state: SelectionState, event: SelectionEvent, result: SelectionResult) -> None:
        running_mode = self._running_mode
        assert running_mode is not None  # _decide 가 확인했다

        choice = (result.rule_mode, _get_target_track(result.params))
        if choice[0] == running_mode.rule_mode and self._is_same_target(state, choice[1]):
            self._follow_target(now_sec, choice[1])
            self._report_continued(now_sec, event, result)
            return
        if choice == self._last_failed_choice:
            logger.info(
                "redecide_ignored t=%.1f mode=%s target=%s reason=just_failed (keeping %s)",
                now_sec, choice[0], choice[1], running_mode.rule_mode,
            )
            return
        if choice[1] is None and self._is_running_target_alive(now_sec, state):
            logger.info(
                "redecide_ignored t=%.1f mode=%s reason=target_alive (keeping %s/%s)",
                now_sec, choice[0], running_mode.rule_mode, self._running_target,
            )
            return

        logger.info(
            "redecide t=%.1f from=%s/%s to=%s/%s",
            now_sec, running_mode.rule_mode, self._running_target, choice[0], choice[1],
        )
        self._abandon_running_mode(now_sec, ABANDON_REASON_REDECIDE)
        self._last_failed_choice = None  # 실패 직후 고른 모드를 떠났다 — 막는 것은 그 모드가 도는 동안뿐
        self._start_selected_mode(now_sec, event, result)

    def _is_same_target(self, state: SelectionState, target_track: str | None) -> bool:
        """target_track 이 실행 중 대상과 같은 표적인가 — 같은 라벨, 또는 같은 클래스이고 SAME_TARGET_RADIUS_M 안."""
        if target_track == self._running_target:
            return True
        if target_track is None or self._running_target is None:
            return False

        running_track = _find_track_state(state, self._running_target)
        chosen_track = _find_track_state(state, target_track)
        if running_track is None or chosen_track is None:
            return False

        return _is_near_same_class(running_track, chosen_track)

    def _follow_target(self, now_sec: float, target_track: str | None) -> None:
        """같은 표적의 새 id 로 비교 라벨을 옮긴다 — 모드가 이미 그 id 로 갈아탔거나 곧 갈아탄다."""
        if target_track == self._running_target:
            return

        logger.debug(
            "redecide t=%.1f target %s continues as %s (same target)", now_sec, self._running_target, target_track
        )
        self._running_target = target_track

    def _is_running_target_alive(self, now_sec: float, state: SelectionState) -> bool:
        """실행 중 대상(또는 같은 표적의 다른 id)이 아직 원하는 대상이고 TARGET_KEEP_SEC 안에 보였는가."""
        if self._running_target is None:
            return False

        running_track = _find_track_state(state, self._running_target)
        if running_track is None:
            return False

        hearted_labels = frozenset(state.mission.hearted_tracks)
        return any(
            (track is running_track or _is_near_same_class(running_track, track))
            and is_wanted_track(track, state.mission, hearted_labels)
            and now_sec - track.last_seen_sec <= TARGET_KEEP_SEC
            for track in state.tracks
        )

    def _start_selected_mode(self, now_sec: float, event: SelectionEvent, result: SelectionResult) -> None:
        record = SelectionRecord(
            now_sec, event.kind, result.rule_mode, result.params, result.source, result.fallback_reason,
            event.mode_result, confidence=result.confidence,
        )
        self._record_selection(record, result)
        self._start_mode(result.rule_mode, result.params)

        if result.rule_mode == "return_home":
            self._record_return(now_sec, RETURN_REASON_SELECTED, result.params)

    def _start_mode(self, rule_mode: str, params: ModeParams) -> None:
        self._running_mode = self._mode_factory(rule_mode, params, self._settings)
        self._running_target = _get_target_track(params)
        self._is_burst_started = False

    def _report_continued(self, now_sec: float, event: SelectionEvent, result: SelectionResult) -> None:
        """같은 결정 — 모드를 그대로 두고 기록 없이 debug 로그·continued payload 만 낸다."""
        logger.debug(
            "%s t=%.1f event=%s mode=%s target=%s continued",
            SELECTION_LOG_PREFIX, now_sec, event.kind, result.rule_mode, self._running_target,
        )
        if self._on_selection is None:
            return

        record = SelectionRecord(
            now_sec, event.kind, result.rule_mode, result.params, result.source, result.fallback_reason,
            event.mode_result, confidence=result.confidence,
        )
        self._on_selection(build_selection_payload(record, result, continued=True))

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
        self._start_mode("return_home", params)

        record = SelectionRecord(now_sec, event_kind, "return_home", params, SOURCE_FORCED, "none", None, confidence=None)
        self._record_selection(record, None)
        self._record_return(now_sec, event_kind, params)

    def _abandon_running_mode(self, now_sec: float, reason: str) -> None:
        if self._running_mode is None:
            return

        logger.info("mode_abandoned t=%.1f mode=%s reason=%s", now_sec, self._running_mode.rule_mode, reason)
        self._running_mode = None

    # --- 하트 기록 (D-13, D-14) ---

    def _handle_heart_accepted(self, track_id: int) -> None:
        self._is_burst_started = True  # 연사가 시작됐다 — 이 모드는 끝까지 재판단으로 끊지 않는다
        self._record_heart(track_id)

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

        self._mission_log.record(KIND_HEART, self._now(), target=track_label(track_id), north=north_m, east=east_m)

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

    def _record_selection(self, record: SelectionRecord, result: SelectionResult | None) -> None:
        self._selections.append(record)
        self._mission_log.record(
            KIND_SELECTION, record.time_sec,
            event=record.event_kind, mode=record.rule_mode, params=record.params, source=record.source,
            confidence=record.confidence, fallback=record.fallback_reason,
        )

        if self._on_selection is not None:
            self._on_selection(build_selection_payload(record, result))

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
        is_failed = result.outcome in FAILED_OUTCOMES
        self._last_failed_choice = (result.rule_mode, result.target_track) if is_failed else None
        logger.info(
            "%s t=%.1f mode=%s outcome=%s target=%s",
            MODE_END_LOG_PREFIX, now_sec, result.rule_mode, result.outcome, result.target_track,
        )

        if self._on_mode_end is not None:
            self._on_mode_end(build_mode_end_payload(now_sec, result))

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
            elapsed_sec=self._now() - self._start_sec,
            forced_return=self._forced_return,
            return_reason=self._return_reason,
            transitions=tuple(self._transitions),
        )
        self._mission_log.record(
            KIND_MISSION, self._now(),
            event="end", end_reason=result.end_reason, elapsed=round(result.elapsed_sec, 1),
            return_reason=result.return_reason, forced_return=result.forced_return,
        )
        logger.info(
            "mission finished: end_reason=%s elapsed=%.1f selections=%d mode_results=%d final_flight_mode=%s forced_return=%s",
            result.end_reason, result.elapsed_sec, len(result.selections), len(result.mode_results),
            result.final_flight_mode, result.forced_return,
        )

        return result


def _get_target_track(params: ModeParams) -> str | None:
    target_track = params.get("target_track")
    return target_track if isinstance(target_track, str) else None


def _find_track_state(state: SelectionState, label: str) -> TrackState | None:
    for track in state.tracks:
        if track.track_id == label:
            return track

    return None


def _is_near_same_class(track_a: TrackState, track_b: TrackState) -> bool:
    distance_m = math.hypot(track_a.north_m - track_b.north_m, track_a.east_m - track_b.east_m)
    return track_a.class_name == track_b.class_name and distance_m <= SAME_TARGET_RADIUS_M


def _validate_redecide_interval(redecide_interval_sec: float | None) -> None:
    if redecide_interval_sec is None:
        return

    value = redecide_interval_sec
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"redecide_interval_sec must be None or a finite number > 0, got {value!r}")
