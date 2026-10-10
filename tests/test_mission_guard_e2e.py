"""헤드리스 안전 감시 E2E — mock 세계(빨간 옷 1명 + 다른 색 1명)에서 비상·운용자 개입을 주입하고 임무 전체를 돈다.

증명하는 것:
- SC3 / SC4 (GUARD-01..03): 링크 두절·GPS 끊김·GPS 점프·배터리·구역 이탈·최대 비행 시간 6가지 사유 각각에서
  AI→RETURN(사유) 이 되고, 선택기 없이 강제 return_home 이 돌아 홈 근처에 착륙한다.
- SC6 (D-06): 안전 감시는 선택기 종류를 모른다 — 규칙 선택기와 "늘 search 만 고르는 가짜 d1형 선택기" 모두
  같은 주입에서 같은 사유로 RETURN 되고, RETURN 뒤에는 어느 선택기도 불리지 않는다.
- SC2 (MIS-02, MIS-03, D-07, D-08): AI 중 운용자 개입은 MANUAL 일시정지(명령 0개)가 되고, 재개하면 그 틱에
  선택기가 다시 고른다. 하트 기록이 유지되어 이미 하트를 보낸 사람에게 다시 쏘지 않는다.
- D-04: RETURN 중 링크가 다시 붙어도 복귀를 계속한다(RETURN→AI 없음).
- D-09 / D-10: RETURN 중 개입하면 RETURN→MANUAL 로 복귀가 멈추고, 재개 틱에 안전 감시가 먼저 돌아 비상이 남아
  있으면 곧바로 RETURN, 해소됐으면 선택기가 다시 고른다.

주입 수단은 D-19 헤드리스 경로뿐이다 — kinematic 주입 속성(gps_lost, gps_offset_m, battery_offset_pct),
ScriptedOperator 공개 속성, mode_factory 래퍼. advance 를 감싼 FaultSchedule 이 매 틱 조건을 보고 한 번씩 주입한다.
"""

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from modules.decision.interface import FALLBACK_NONE, SOURCE_D1, Latency, SelectionResult
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.rule_selector import RuleSelector
from modules.drone.mission_loop import END_LANDED, SOURCE_FORCED, MissionConfig, MissionRunner, build_rule_mode_settings
from modules.drone.run_mission import DEFAULT_MOCK_TARGETS, build_mock_world
from modules.flight_control.interface import FireHeart, GoTo
from modules.mission.mission_log import (
    KIND_LINK,
    KIND_MODE_TRANSITION,
    KIND_RETURN,
    KIND_SAFETY,
    KIND_SELECTION,
    MissionLog,
)
from modules.mission.mode_machine import MODE_AI, MODE_MANUAL, MODE_RETURN, REASON_OPERATOR
from modules.mission.operator import ScriptedOperator
from modules.mission.safety import (
    REASON_BATTERY,
    REASON_GEOFENCE,
    REASON_GPS_JUMP,
    REASON_GPS_LOST,
    REASON_LINK_LOST,
    REASON_MAX_FLIGHT_TIME,
    SAFETY_REASONS,
    SafetyConfig,
    SafetyMonitor,
)
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.factory import create_rule_mode
from modules.rule_mode.interface import ModeTick, track_label

MAX_MISSION_SEC = 600.0
INJECT_SEC = 40.0  # 이륙·첫 선택 뒤, 임무 진행 중
HEART_BURST_COUNT = 10
MANUAL_HOLD_SEC = 10.0
RUNAWAY_NORTH_M = 400.0  # 구역(north 20~120, 여유 20 m) 훨씬 밖


def get_target_track_id(upper_color: str) -> int:
    return next(target.track_id for target in DEFAULT_MOCK_TARGETS if target.upper_color == upper_color)


# --- 주입 일정 ---


class FaultSchedule:
    """(이름, 조건, 동작) 항목을 매 틱 확인해 조건이 처음 참인 틱에 동작을 한 번만 실행하고 그 시각을 남긴다."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, Callable, Callable]] = []
        self.fired_sec: dict[str, float] = {}

    def add(self, name: str, condition: Callable, action: Callable) -> None:
        self._entries.append((name, condition, action))

    def at(self, name: str, time_sec: float, action: Callable) -> None:
        self.add(name, lambda flight, now_sec: now_sec >= time_sec, action)

    def after(self, name: str, previous_name: str, delay_sec: float, action: Callable) -> None:
        def is_due(flight, now_sec: float) -> bool:
            previous_sec = self.fired_sec.get(previous_name)
            return previous_sec is not None and now_sec >= previous_sec + delay_sec

        self.add(name, is_due, action)

    def apply(self, flight, now_sec: float) -> None:
        for name, condition, action in self._entries:
            if name in self.fired_sec or not condition(flight, now_sec):
                continue
            action(flight)
            self.fired_sec[name] = now_sec


def run_guarded_mission(
    selector,
    schedule: FaultSchedule,
    operator: ScriptedOperator | None = None,
    safety: SafetyMonitor | None = None,
    mode_factory=create_rule_mode,
    mission_log: MissionLog | None = None,
):
    defaults = ParamDefaults()
    settings = build_rule_mode_settings(defaults, target_upper_color="red")
    flight, frame_source = build_mock_world()
    mission_log = mission_log or MissionLog()

    def advance(dt_sec: float) -> None:
        flight.advance(dt_sec)
        schedule.apply(flight, flight.now_sec)

    runner = MissionRunner(
        flight_control=flight,
        frame_source=frame_source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=selector,
        param_defaults=defaults,
        settings=settings,
        clock=lambda: flight.now_sec,
        advance=advance,
        config=MissionConfig(max_mission_sec=MAX_MISSION_SEC),
        mode_factory=mode_factory,
        operator=operator or ScriptedOperator(),
        safety=safety,
        mission_log=mission_log,
    )

    return runner.run(), flight, mission_log, settings


# --- 선택기·모드 대역 ---


class AlwaysSearchD1Selector:
    """늘 search 만 고르는 가짜 d1형 선택기 — 비상에도 임무를 계속하려는 원격 선택기 흉내 (SC6)."""

    def __init__(self, defaults: ParamDefaults | None = None) -> None:
        self._defaults = defaults or ParamDefaults()

    def select(self, state, event) -> SelectionResult:
        return SelectionResult(
            rule_mode="search",
            params=fill_params("search", state, event, self._defaults),
            confidence=0.9,
            probabilities={"search": 1.0},
            source=SOURCE_D1,
            fallback_reason=FALLBACK_NONE,
            options=("search",),
            latency=Latency(0.0, None, None),
            input_tokens=None,
        )


def build_selector(selector_kind: str):
    if selector_kind == "rule":
        return RuleSelector(ParamDefaults())

    return AlwaysSearchD1Selector()


class RunawayMode:
    """search 자리를 차지해 구역 북쪽 멀리 GoTo 를 한 번 보내고 끝나지 않는 모드 (구역 이탈 주입)."""

    rule_mode = "search"

    def __init__(self, altitude_m: float) -> None:
        self._altitude_m = altitude_m
        self._is_started = False

    def step(self, tick: ModeTick, control) -> None:
        if not self._is_started:
            control.send_command(GoTo(RUNAWAY_NORTH_M, 0.0, self._altitude_m))
            self._is_started = True

        return None


def make_runaway_factory():
    def factory(rule_mode, params, settings):
        if rule_mode == "search":
            return RunawayMode(settings.search_altitude_m)

        return create_rule_mode(rule_mode, params, settings)

    return factory


# --- 결과 조회 도우미 ---


def count_hearts_by_track(flight) -> dict[int, int]:
    return dict(Counter(command.track_id for _, command in flight.commands_received if isinstance(command, FireHeart)))


def get_commands_between(flight, start_sec: float, end_sec: float) -> list:
    return [command for time_sec, command in flight.commands_received if start_sec <= time_sec < end_sec]


def get_transitions(result, to_mode: str | None = None) -> list:
    return [transition for transition in result.transitions if to_mode is None or transition.to_mode == to_mode]


def get_transition_pairs(result) -> list[tuple[str, str, str]]:
    return [(transition.from_mode, transition.to_mode, transition.reason) for transition in result.transitions]


def get_selector_records_after(result, time_sec: float) -> list:
    return [record for record in result.selections if record.source != SOURCE_FORCED and record.time_sec >= time_sec]


def describe(result) -> str:
    return f"end_reason={result.end_reason} return_reason={result.return_reason} transitions={get_transition_pairs(result)}"


def get_log_records(mission_log: MissionLog, kind: str) -> list[dict]:
    return [record for record in mission_log.records if record["kind"] == kind]


def assert_landed_near_home(result, flight, settings) -> None:
    telemetry = flight.get_telemetry()
    assert result.end_reason == END_LANDED, describe(result)
    assert result.final_flight_mode == "grounded"
    assert math.hypot(telemetry.north_m, telemetry.east_m) <= settings.arrival_tolerance_m + 1.0


# --- 사유 6종 × 선택기 2종 (SC3, SC4, SC6) ---


def set_battery_drop(flight) -> None:
    flight.battery_offset_pct = 80.0


def set_gps_lost(flight) -> None:
    flight.gps_lost = True


def set_gps_jump(flight) -> None:
    flight.gps_offset_m = (500.0, 0.0)


@dataclass
class FaultCase:
    """한 임무의 주입 설정. 기본값은 주입 없음."""

    schedule: FaultSchedule = field(default_factory=FaultSchedule)
    operator: ScriptedOperator = field(default_factory=ScriptedOperator)
    safety: SafetyMonitor | None = None
    mode_factory: Callable = create_rule_mode


def inject_link_lost(case: FaultCase) -> None:
    case.schedule.at("fault", INJECT_SEC, lambda flight: setattr(case.operator, "link_connected", False))


def inject_gps_lost(case: FaultCase) -> None:
    case.schedule.at("fault", INJECT_SEC, set_gps_lost)


def inject_gps_jump(case: FaultCase) -> None:
    case.schedule.at("fault", INJECT_SEC, set_gps_jump)


def inject_battery_drop(case: FaultCase) -> None:
    case.schedule.at("fault", INJECT_SEC, set_battery_drop)


def inject_geofence_runaway(case: FaultCase) -> None:
    case.mode_factory = make_runaway_factory()  # 첫 search 부터 구역 북쪽 멀리 날아간다


def inject_short_max_flight(case: FaultCase) -> None:
    search_area = build_rule_mode_settings(ParamDefaults()).search_area
    case.safety = SafetyMonitor(SafetyConfig(search_area=search_area, max_flight_sec=INJECT_SEC))


FAULT_CASES = {  # 사유 → 주입 설정
    REASON_LINK_LOST: inject_link_lost,
    REASON_GPS_LOST: inject_gps_lost,
    REASON_GPS_JUMP: inject_gps_jump,
    REASON_BATTERY: inject_battery_drop,
    REASON_GEOFENCE: inject_geofence_runaway,
    REASON_MAX_FLIGHT_TIME: inject_short_max_flight,
}
INJECTED_AT_INJECT_SEC = (REASON_BATTERY, REASON_GPS_LOST, REASON_GPS_JUMP, REASON_LINK_LOST, REASON_MAX_FLIGHT_TIME)


@pytest.mark.parametrize("selector_kind", ["rule", "d1"])
@pytest.mark.parametrize("reason", SAFETY_REASONS)
def test_each_safety_reason_forces_return_and_landing_for_any_selector(reason, selector_kind):
    case = FaultCase()
    FAULT_CASES[reason](case)

    result, flight, mission_log, settings = run_guarded_mission(
        build_selector(selector_kind), case.schedule,
        operator=case.operator, safety=case.safety, mode_factory=case.mode_factory,
    )

    assert_landed_near_home(result, flight, settings)

    returns = get_transitions(result, MODE_RETURN)
    assert [(transition.from_mode, transition.reason) for transition in returns] == [(MODE_AI, reason)], describe(result)
    return_sec = returns[0].time_sec
    if reason in INJECTED_AT_INJECT_SEC:
        assert return_sec >= INJECT_SEC  # 주입 시각에 임무가 아직 진행 중이었다
    assert not any(
        transition.from_mode == MODE_RETURN and transition.to_mode == MODE_AI for transition in result.transitions
    )

    assert result.return_reason == reason
    assert result.forced_return
    forced = [record for record in result.selections if record.source == SOURCE_FORCED]
    assert any(record.event_kind == reason and record.confidence is None for record in forced)
    assert get_selector_records_after(result, return_sec) == []

    assert any(record["reason"] == reason for record in get_log_records(mission_log, KIND_SAFETY))
    assert any(record["to"] == MODE_RETURN for record in get_log_records(mission_log, KIND_MODE_TRANSITION))
    assert any(record["reason"] == reason for record in get_log_records(mission_log, KIND_RETURN))


# --- MANUAL 개입·재개 (SC2, D-07, D-08) ---


def assert_manual_takeover_and_resume(result, flight, schedule: FaultSchedule) -> None:
    red_track_id = get_target_track_id("red")
    manual_sec = schedule.fired_sec["manual"]
    resume_sec = schedule.fired_sec["resume"]

    assert result.end_reason == END_LANDED, describe(result)
    assert result.return_reason is None
    assert count_hearts_by_track(flight) == {red_track_id: HEART_BURST_COUNT}
    assert get_commands_between(flight, manual_sec, resume_sec) == []
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
    ]
    assert result.transitions[2].time_sec == resume_sec
    assert any(record.time_sec == resume_sec for record in get_selector_records_after(result, resume_sec))


def test_manual_takeover_after_burst_resumes_without_firing_again():
    red_track_id = get_target_track_id("red")
    operator = ScriptedOperator()
    schedule = FaultSchedule()

    def has_burst_finished(flight, now_sec: float) -> bool:
        return count_hearts_by_track(flight).get(red_track_id, 0) == HEART_BURST_COUNT

    schedule.add("manual", has_burst_finished, lambda flight: setattr(operator, "ai_requested", False))
    schedule.after("resume", "manual", MANUAL_HOLD_SEC, lambda flight: setattr(operator, "ai_requested", True))

    result, flight, _, _ = run_guarded_mission(RuleSelector(ParamDefaults()), schedule, operator=operator)

    assert_manual_takeover_and_resume(result, flight, schedule)


def test_manual_takeover_during_approach_resumes_and_fires_once():
    operator = ScriptedOperator()
    mission_log = MissionLog()
    schedule = FaultSchedule()

    def has_selected_approach(flight, now_sec: float) -> bool:
        # runner 내부가 아니라 MissionLog 의 selection 기록으로 판단한다
        return any(record["mode"] == "approach" for record in get_log_records(mission_log, KIND_SELECTION))

    schedule.add("manual", has_selected_approach, lambda flight: setattr(operator, "ai_requested", False))
    schedule.after("resume", "manual", MANUAL_HOLD_SEC, lambda flight: setattr(operator, "ai_requested", True))

    result, flight, _, _ = run_guarded_mission(
        RuleSelector(ParamDefaults()), schedule, operator=operator, mission_log=mission_log
    )

    commands_before_manual = get_commands_between(flight, 0.0, schedule.fired_sec["manual"])
    assert_manual_takeover_and_resume(result, flight, schedule)
    assert not any(isinstance(command, FireHeart) for command in commands_before_manual)  # 발사 전에 개입했다


# --- RETURN 중 링크 복구·운용자 개입 (D-04, D-09, D-10) ---


def test_link_restored_during_return_keeps_returning():
    operator = ScriptedOperator()
    schedule = FaultSchedule()
    schedule.at("cut", INJECT_SEC, lambda flight: setattr(operator, "link_connected", False))
    schedule.after("restore", "cut", 5.0, lambda flight: setattr(operator, "link_connected", True))

    result, flight, mission_log, settings = run_guarded_mission(
        RuleSelector(ParamDefaults()), schedule, operator=operator
    )

    assert_landed_near_home(result, flight, settings)
    assert result.return_reason == REASON_LINK_LOST
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_RETURN, REASON_LINK_LOST),
    ]
    assert get_selector_records_after(result, schedule.fired_sec["cut"]) == []
    assert [record["connected"] for record in get_log_records(mission_log, KIND_LINK)] == [True, False, True]


def test_manual_during_return_then_resume_with_fault_remaining_returns_again():
    operator = ScriptedOperator()
    schedule = FaultSchedule()
    schedule.at("fault", INJECT_SEC, set_battery_drop)
    schedule.after("manual", "fault", 3.0, lambda flight: setattr(operator, "ai_requested", False))
    schedule.after("resume", "manual", 5.0, lambda flight: setattr(operator, "ai_requested", True))

    result, flight, _, settings = run_guarded_mission(RuleSelector(ParamDefaults()), schedule, operator=operator)

    resume_sec = schedule.fired_sec["resume"]
    assert_landed_near_home(result, flight, settings)
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_RETURN, REASON_BATTERY),
        (MODE_RETURN, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_RETURN, REASON_BATTERY),
    ]
    assert result.transitions[3].time_sec == result.transitions[4].time_sec == resume_sec
    assert get_commands_between(flight, schedule.fired_sec["manual"], resume_sec) == []
    assert get_selector_records_after(result, schedule.fired_sec["fault"]) == []
    forced_battery = [record for record in result.selections if record.event_kind == REASON_BATTERY]
    assert [record.time_sec for record in forced_battery][-1] == resume_sec
    assert len(forced_battery) == 2
    assert result.return_reason == REASON_BATTERY


def test_manual_during_return_then_resume_after_fault_cleared_selects_again():
    operator = ScriptedOperator()
    schedule = FaultSchedule()
    schedule.at("cut", INJECT_SEC, lambda flight: setattr(operator, "link_connected", False))
    schedule.after("manual", "cut", 3.0, lambda flight: setattr(operator, "ai_requested", False))
    schedule.after("restore", "manual", 2.0, lambda flight: setattr(operator, "link_connected", True))
    schedule.after("resume", "manual", 5.0, lambda flight: setattr(operator, "ai_requested", True))

    result, flight, _, settings = run_guarded_mission(RuleSelector(ParamDefaults()), schedule, operator=operator)

    resume_sec = schedule.fired_sec["resume"]
    assert_landed_near_home(result, flight, settings)
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_RETURN, REASON_LINK_LOST),
        (MODE_RETURN, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
    ]
    resumed = get_selector_records_after(result, resume_sec)
    assert resumed and resumed[0].time_sec == resume_sec
    assert resumed[0].source != SOURCE_FORCED
    assert result.return_reason == REASON_LINK_LOST
