import dataclasses
import logging

import pytest

from modules.decision.interface import (
    EVENT_MODE_ENDED,
    FALLBACK_NONE,
    SOURCE_RULE,
    Latency,
    SelectionResult,
)
from modules.decision.interface.state import ModeResult
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.rule_selector import RuleSelector
from modules.drone.mission_loop import (
    END_LANDED,
    END_RETURN_FAILED,
    END_TAKEOFF_REJECTED,
    EVENT_RETURN_RETRY,
    SOURCE_FORCED,
    MissionConfig,
    MissionRunner,
    build_rule_mode_settings,
)
from modules.drone.run_mission import MOCK_DETECTION_RANGE_M, RangeLimitedTargetSource
from modules.flight_control.interface import CommandAck, FireHeart, TakeOff
from modules.mission.mission_log import (
    KIND_DETECTION,
    KIND_HEART,
    KIND_LINK,
    KIND_MISSION,
    KIND_MODE_TRANSITION,
    KIND_RETURN,
    KIND_SELECTION,
    MissionLog,
)
from modules.mission.mode_machine import MODE_AI, MODE_MANUAL, REASON_OPERATOR, ModeTransition
from modules.mission.operator import ScriptedOperator
from modules.mission.safety import REASON_MAX_FLIGHT_TIME
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.factory import SUPPORTED_RULE_MODES, create_rule_mode
from modules.rule_mode.interface import RuleMode, RuleModeSettings
from modules.rule_mode.return_home import ReturnHomeMode
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource

PARAMS_BY_MODE = {
    "search": {"timeout_sec": 120.0},
    "approach": {"target_track": "T1", "stop_distance_m": 5.0, "timeout_sec": 30.0},
    "hold": {"duration_sec": 5.0},
    "return_home": {"home_north_m": 0.0, "home_east_m": 0.0, "return_altitude_m": 10.0},
    "aim_and_fire": {
        "target_track": "T1",
        "center_tolerance_ratio": 0.05,
        "stable_frame_count": 5,
        "timeout_sec": 15.0,
    },
    "observe": {"target_track": "T1", "timeout_sec": 10.0},
}


# --- 모드 팩토리 ---


@pytest.mark.parametrize("rule_mode", SUPPORTED_RULE_MODES)
def test_factory_creates_supported_modes(rule_mode):
    mode = create_rule_mode(rule_mode, PARAMS_BY_MODE[rule_mode], RuleModeSettings())

    assert mode.rule_mode == rule_mode
    assert isinstance(mode, RuleMode)


def test_factory_rejects_unknown_name_with_value_error():
    with pytest.raises(ValueError):
        create_rule_mode("bogus", {}, RuleModeSettings())


# --- 임무 루프 도우미 ---

# 홈에서 60 m 라 탐지 거리(40 m) 밖 — search 가 다가가야 보인다
DEFAULT_PERSON = ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0)
RED_PERSON = ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0, upper_color="red")
SWAPPED_TRACK_ID = 7


def get_fire_hearts(flight) -> list[FireHeart]:
    return [command for _, command in flight.commands_received if isinstance(command, FireHeart)]


def build_red_settings():
    return dataclasses.replace(build_rule_mode_settings(ParamDefaults()), target_upper_color="red")


class RecordingSelector:
    """실제 선택기를 감싸 select 에 들어온 (state, event) 를 기록한다."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = []

    def select(self, state, event):
        self.calls.append((state, event))

        return self._inner.select(state, event)


class ScriptedSelector:
    """정해 둔 rule_mode 순서를 돌려주는 가짜 선택기. 순서가 끝나면 마지막 것을 반복한다."""

    def __init__(self, rule_modes, defaults: ParamDefaults | None = None) -> None:
        self._rule_modes = list(rule_modes)
        self._defaults = defaults or ParamDefaults()
        self.calls = []

    def select(self, state, event):
        self.calls.append((state, event))
        rule_mode = self._rule_modes[min(len(self.calls), len(self._rule_modes)) - 1]
        if rule_mode == "hold":
            params = {"duration_sec": 1000.0}
        elif rule_mode == "aim_and_fire":
            params = {
                "target_track": self._get_first_person_label(state),
                "center_tolerance_ratio": self._defaults.aim_center_tolerance_ratio,
                "stable_frame_count": self._defaults.aim_stable_frame_count,
                "timeout_sec": self._defaults.aim_timeout_sec,
            }
        elif rule_mode == "observe":
            params = {"target_track": self._get_first_person_label(state), "timeout_sec": self._defaults.observe_timeout_sec}
        else:
            params = fill_params(rule_mode, state, event, self._defaults)

        return SelectionResult(
            rule_mode=rule_mode,
            params=params,
            confidence=1.0,
            probabilities={rule_mode: 1.0},
            source=SOURCE_RULE,
            fallback_reason=FALLBACK_NONE,
            options=(rule_mode,),
            latency=Latency(0.0, None, None),
            input_tokens=None,
        )

    @staticmethod
    def _get_first_person_label(state):
        return next((track.track_id for track in state.tracks if track.class_name == "person"), None)


class HeartRejectingControl:
    """KinematicFlightControl 을 감싸 FireHeart 만 accepted_heart_count 발 뒤부터 거절한다 (D-14 시험)."""

    def __init__(self, inner: KinematicFlightControl, accepted_heart_count: int) -> None:
        self._inner = inner
        self._accepted_heart_count = accepted_heart_count
        self._heart_count = 0

    @property
    def now_sec(self) -> float:
        return self._inner.now_sec

    @property
    def commands_received(self):
        return self._inner.commands_received

    def advance(self, dt_sec: float) -> None:
        self._inner.advance(dt_sec)

    def get_telemetry(self):
        return self._inner.get_telemetry()

    def send_command(self, command):
        if isinstance(command, FireHeart):
            self._heart_count += 1
            if self._heart_count > self._accepted_heart_count:
                return CommandAck(accepted=False, reason="heart rejected for test")

        return self._inner.send_command(command)


class TrackIdSwappingSource:
    """FireHeart(1) 10발 전에는 track 1 만, 뒤에는 SWAPPED_TRACK_ID 만 내보낸다 (ByteTrack ID 교체 흉내).

    두 track 은 서로 다른 ScriptedTarget 이라 교체 뒤 위치를 따로 둘 수 있다(대조군).
    """

    def __init__(self, inner, flight: KinematicFlightControl) -> None:
        self._inner = inner
        self._flight = flight

    def read_frame(self):
        frame = self._inner.read_frame()
        if frame is None:
            return frame

        hidden_track_id = 1 if self._has_burst_finished() else SWAPPED_TRACK_ID
        detections = tuple(detection for detection in frame.detections if detection.track_id != hidden_track_id)

        return dataclasses.replace(frame, detections=detections)

    def telemetry_for(self, timestamp_sec: float):
        return self._inner.telemetry_for(timestamp_sec)

    def _has_burst_finished(self) -> bool:
        return sum(1 for heart in get_fire_hearts(self._flight) if heart.track_id == 1) >= 10


def build_runner(
    decision_model,
    config=None,
    targets=(DEFAULT_PERSON,),
    settings=None,
    source_wrapper=None,
    control_wrapper=None,
    on_advance=None,
    **overrides,
):
    """on_advance(flight) 는 advance 직후 불린다 — 시각 기준으로 ScriptedOperator·flight 속성을 바꿔 주입한다."""
    flight = KinematicFlightControl()
    defaults = ParamDefaults()
    source = RangeLimitedTargetSource(ScriptedTargetSource(flight, list(targets)), flight, MOCK_DETECTION_RANGE_M)
    if source_wrapper is not None:
        source = source_wrapper(source, flight)
    if control_wrapper is not None:
        flight = control_wrapper(flight)

    def advance(dt_sec: float) -> None:
        flight.advance(dt_sec)
        if on_advance is not None:
            on_advance(flight)

    runner = MissionRunner(
        flight_control=flight,
        frame_source=source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=decision_model,
        param_defaults=defaults,
        settings=settings or build_rule_mode_settings(defaults),
        clock=lambda: flight.now_sec,
        advance=advance,
        config=config or MissionConfig(max_mission_sec=120.0),
        **overrides,
    )

    return runner, flight


# --- 임무 루프: 규칙 선택기 E2E (SC4) ---


def test_rule_selector_mission_flows_search_approach_fire_search_return():
    selector = RecordingSelector(RuleSelector(ParamDefaults()))
    runner, flight = build_runner(selector, config=MissionConfig(max_mission_sec=300.0))

    result = runner.run()

    modes = [mode_result.rule_mode for mode_result in result.mode_results]
    assert result.is_success
    assert result.end_reason == END_LANDED
    assert result.final_flight_mode == "grounded"
    assert not result.forced_return
    # 1 s 재판단: new_target 에서 search 를 버리고 approach 로 바꾼다 — search 는 결과 없이 끝난다
    assert modes == ["approach", "aim_and_fire", "search", "return_home"]
    assert result.mode_results[0] == ModeResult("approach", "success", "T1")
    assert result.mode_results[1] == ModeResult("aim_and_fire", "success", "T1")
    assert result.mode_results[2].outcome == "failure"
    assert result.mode_results[-1] == ModeResult("return_home", "success", None)
    assert [record.rule_mode for record in result.selections] == ["search", "approach", "aim_and_fire", "search", "return_home"]
    assert result.selections[1].params["target_track"] == "T1"
    assert result.elapsed_sec < 300.0
    assert flight.commands_received[0][1] == TakeOff(15.0)
    assert [heart.track_id for heart in get_fire_hearts(flight)] == [1] * 10


def test_every_mode_result_reaches_selector_as_mode_ended_event():
    selector = RecordingSelector(RuleSelector(ParamDefaults()))
    runner, _ = build_runner(selector, config=MissionConfig(max_mission_sec=300.0))

    result = runner.run()

    ended_results = [event.mode_result for _, event in selector.calls if event.kind == EVENT_MODE_ENDED]
    assert ended_results[0] == result.mode_results[0]
    assert ended_results[1] == result.mode_results[1]
    assert all(ended in result.mode_results for ended in ended_results)
    assert ended_results == list(result.mode_results[:-1])  # return_home 결과로는 선택하지 않는다


def test_hearted_label_reaches_next_selection_state():
    selector = RecordingSelector(RuleSelector(ParamDefaults()))
    runner, _ = build_runner(selector, config=MissionConfig(max_mission_sec=300.0))

    runner.run()

    hearted_after_fire = [state.mission.hearted_tracks for state, _ in selector.calls]
    assert hearted_after_fire[0] == ()
    assert hearted_after_fire[-1] == ("T1",)


# --- 임무 루프: 실제 aim_and_fire / observe (RM-02, RM-06, HRT-01) ---


def test_aim_and_fire_fires_burst_and_marks_hearted(caplog):
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")
    selector = ScriptedSelector(["search", "aim_and_fire", "return_home"])
    runner, flight = build_runner(selector, redecide_interval_sec=None)  # 순서 고정 선택기 — 사건 때만 고른다

    result = runner.run()

    assert result.is_success
    assert [mode_result.rule_mode for mode_result in result.mode_results] == ["search", "aim_and_fire", "return_home"]
    assert result.mode_results[1] == ModeResult("aim_and_fire", "success", "T1")
    assert [heart.track_id for heart in get_fire_hearts(flight)] == [1] * 10
    assert selector.calls[2][0].mission.hearted_tracks == ("T1",)
    assert selector.calls[2][1].mode_result == ModeResult("aim_and_fire", "success", "T1")
    assert "heart t=" in caplog.text
    assert "not implemented until Phase 4" not in caplog.text


def test_observe_succeeds_once_color_is_confirmed():
    selector = ScriptedSelector(["search", "observe", "return_home"])
    runner, flight = build_runner(
        selector, targets=(RED_PERSON,), settings=build_red_settings(), redecide_interval_sec=None
    )

    result = runner.run()

    assert result.is_success
    assert result.mode_results[1] == ModeResult("observe", "success", "T1")
    assert selector.calls[2][1].mode_result == ModeResult("observe", "success", "T1")
    assert selector.calls[2][0].mission.hearted_tracks == ()
    assert get_fire_hearts(flight) == []


# --- 임무 루프: 발사 기록 (D-14) ---


def test_burst_rejected_midway_fails_but_marks_hearted():
    selector = ScriptedSelector(["search", "aim_and_fire", "return_home"])
    runner, flight = build_runner(
        selector, control_wrapper=lambda inner: HeartRejectingControl(inner, accepted_heart_count=3),
        redecide_interval_sec=None,
    )

    result = runner.run()

    assert result.is_success
    assert result.mode_results[1].rule_mode == "aim_and_fire"
    assert result.mode_results[1].outcome == "failure"
    assert len(get_fire_hearts(flight)) == 3
    assert "T1" in selector.calls[2][0].mission.hearted_tracks


def test_burst_rejected_from_first_shot_is_not_hearted():
    selector = ScriptedSelector(["search", "aim_and_fire", "return_home"])
    runner, flight = build_runner(
        selector, control_wrapper=lambda inner: HeartRejectingControl(inner, accepted_heart_count=0),
        redecide_interval_sec=None,
    )

    result = runner.run()

    assert result.is_success
    assert result.mode_results[1].outcome == "failure"
    assert get_fire_hearts(flight) == []
    assert "T1" not in selector.calls[2][0].mission.hearted_tracks


# --- 임무 루프: 위치 기반 중복 제외 (HRT-02, D-13) ---


def run_track_id_swap_mission(config, swapped_east_offset_m=0.0):
    swapped_person = dataclasses.replace(
        RED_PERSON, track_id=SWAPPED_TRACK_ID, east_m=RED_PERSON.east_m + swapped_east_offset_m
    )
    selector = RecordingSelector(RuleSelector(ParamDefaults()))
    runner, flight = build_runner(
        selector, config=config, targets=(RED_PERSON, swapped_person), settings=build_red_settings(),
        source_wrapper=TrackIdSwappingSource,
    )

    return runner.run(), flight, selector


def test_new_track_id_at_hearted_position_is_not_fired_again():
    result, flight, selector = run_track_id_swap_mission(MissionConfig(max_mission_sec=300.0))

    hearts = get_fire_hearts(flight)
    assert result.end_reason == END_LANDED
    assert len(hearts) == 10
    assert all(heart.track_id == 1 for heart in hearts)
    assert any("T7" in state.mission.hearted_tracks for state, _ in selector.calls)
    assert any(any(track.track_id == "T7" for track in state.tracks) for state, _ in selector.calls)


def test_new_track_id_within_radius_is_not_fired_again():
    result, flight, _ = run_track_id_swap_mission(MissionConfig(max_mission_sec=300.0), swapped_east_offset_m=1.0)

    assert result.end_reason == END_LANDED
    assert {heart.track_id for heart in get_fire_hearts(flight)} == {1}


def test_hearted_radius_smaller_than_offset_lets_swapped_track_be_fired():
    # 대조군 — 위치 판정이 실제로 재발사를 막는다는 증거. mock 위치 추정은 정확하므로 1 m 떨어뜨리고 반경을 0.5 m 로 둔다
    config = MissionConfig(max_mission_sec=300.0, hearted_radius_m=0.5)
    result, flight, _ = run_track_id_swap_mission(config, swapped_east_offset_m=1.0)

    hearts = get_fire_hearts(flight)
    assert result.end_reason == END_LANDED
    assert any(heart.track_id == SWAPPED_TRACK_ID for heart in hearts)


@pytest.mark.parametrize("radius_m", [0.0, -1.0])
def test_non_positive_hearted_radius_is_rejected(radius_m):
    with pytest.raises(ValueError):
        MissionConfig(hearted_radius_m=radius_m)


def test_loop_accepts_any_decision_model():
    selector = ScriptedSelector(["return_home"])
    runner, _ = build_runner(selector)

    result = runner.run()

    assert result.is_success
    assert [record.rule_mode for record in result.selections] == ["return_home"]


# --- 임무 루프: 최대 임무 시간 / 복귀 재시도 / 이륙 거절 ---


def test_max_flight_time_forces_return_home_without_selector():
    selector = ScriptedSelector(["hold"])
    runner, _ = build_runner(selector, config=MissionConfig(max_mission_sec=20.0), redecide_interval_sec=None)

    result = runner.run()

    assert result.is_success
    assert result.forced_return
    assert result.return_reason == REASON_MAX_FLIGHT_TIME
    assert result.final_flight_mode == "grounded"
    assert len(selector.calls) == 1
    forced = result.selections[-1]
    assert forced.event_kind == REASON_MAX_FLIGHT_TIME
    assert forced.source == SOURCE_FORCED
    assert forced.confidence is None
    assert forced.rule_mode == "return_home"
    assert result.mode_results[-1].rule_mode == "return_home"
    last_transition = result.transitions[-1]
    assert (last_transition.from_mode, last_transition.to_mode, last_transition.reason) == ("AI", "RETURN", REASON_MAX_FLIGHT_TIME)


def _make_short_timeout_factory(short_count: int):
    created = []

    def factory(rule_mode, params, settings):
        if rule_mode != "return_home":
            return create_rule_mode(rule_mode, params, settings)
        created.append(rule_mode)
        timeout_sec = 1.0 if len(created) <= short_count else 120.0

        return ReturnHomeMode(params, settings, timeout_sec=timeout_sec)

    return factory


def test_return_home_timeout_retries_once_then_lands():
    selector = ScriptedSelector(["return_home"])
    runner, _ = build_runner(selector, mode_factory=_make_short_timeout_factory(1))

    result = runner.run()

    assert result.is_success
    assert [mode_result.outcome for mode_result in result.mode_results] == ["timeout", "success"]
    assert result.selections[-1].event_kind == EVENT_RETURN_RETRY
    assert len(selector.calls) == 1


def test_return_home_timeout_twice_ends_abnormally():
    selector = ScriptedSelector(["return_home"])
    runner, _ = build_runner(selector, mode_factory=_make_short_timeout_factory(2))

    result = runner.run()

    assert not result.is_success
    assert result.end_reason == END_RETURN_FAILED
    assert result.final_flight_mode != "grounded"
    assert [mode_result.outcome for mode_result in result.mode_results] == ["timeout", "timeout"]


def test_takeoff_rejected_ends_without_selection():
    selector = ScriptedSelector(["search"])
    runner, flight = build_runner(selector)
    flight.reject_commands = True

    result = runner.run()

    assert result.end_reason == END_TAKEOFF_REJECTED
    assert not result.is_success
    assert selector.calls == []


# --- 임무 루프: 운용 모드 MANUAL/AI (MIS-02, MIS-03, D-07, D-08) ---


def get_commands_between(flight, start_sec: float, end_sec: float) -> list:
    return [command for time_sec, command in flight.commands_received if start_sec <= time_sec < end_sec]


def get_transition_pairs(result) -> list[tuple[str, str, str]]:
    return [(transition.from_mode, transition.to_mode, transition.reason) for transition in result.transitions]


def test_default_operator_switches_manual_to_ai_on_first_tick_and_takes_off():
    selector = ScriptedSelector(["return_home"])
    runner, flight = build_runner(selector)

    result = runner.run()

    assert result.is_success
    assert result.transitions == (ModeTransition(0.0, MODE_MANUAL, MODE_AI, REASON_OPERATOR),)
    assert flight.commands_received[0][1] == TakeOff(15.0)
    assert result.return_reason is None
    assert result.selections[0].confidence == 1.0


def test_mission_waits_in_manual_until_operator_requests_ai():
    operator = ScriptedOperator(ai_requested=False)

    def request_ai_after_three_seconds(flight):
        if flight.now_sec >= 3.0:
            operator.ai_requested = True

    selector = ScriptedSelector(["return_home"])
    runner, flight = build_runner(selector, operator=operator, on_advance=request_ai_after_three_seconds)

    result = runner.run()

    first_time_sec, first_command = flight.commands_received[0]
    assert result.end_reason == END_LANDED
    assert first_command == TakeOff(15.0)
    assert first_time_sec >= 3.0
    assert result.transitions[0].time_sec == first_time_sec


def test_manual_during_takeoff_pauses_and_resends_takeoff():
    operator = ScriptedOperator()

    def take_over_between_one_and_four_seconds(flight):
        operator.ai_requested = not 1.0 <= flight.now_sec < 4.0

    selector = ScriptedSelector(["return_home"])
    runner, flight = build_runner(selector, operator=operator, on_advance=take_over_between_one_and_four_seconds)

    result = runner.run()

    takeoffs = [command for _, command in flight.commands_received if isinstance(command, TakeOff)]
    assert result.end_reason == END_LANDED
    assert len(takeoffs) == 2
    assert get_commands_between(flight, 1.0, 4.0) == []
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
    ]


def test_ai_to_manual_stops_commands_and_resume_selects_immediately():
    operator = ScriptedOperator()

    def take_over_between_thirty_and_forty_seconds(flight):
        operator.ai_requested = not 30.0 <= flight.now_sec < 40.0

    selector = ScriptedSelector(["search", "return_home"])
    runner, flight = build_runner(
        selector, targets=(), operator=operator, on_advance=take_over_between_thirty_and_forty_seconds,
        redecide_interval_sec=None,
    )

    result = runner.run()

    resume = result.transitions[-1]
    assert result.end_reason == END_LANDED
    assert get_commands_between(flight, 30.0, 40.0) == []
    assert get_transition_pairs(result) == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
    ]
    assert resume.time_sec >= 40.0
    assert len(selector.calls) == 2
    assert result.selections[1].time_sec == resume.time_sec
    assert result.selections[1].event_kind == "periodic_check"
    assert result.selections[1].rule_mode == "return_home"


def test_resume_after_manual_keeps_hearted_tracks():
    operator = ScriptedOperator()
    takeover = {}

    def take_over_after_burst(flight):
        hearts = get_fire_hearts(flight)
        if len(hearts) >= 10 and "start_sec" not in takeover:
            takeover["start_sec"] = flight.now_sec
            operator.ai_requested = False
        if "start_sec" in takeover and flight.now_sec >= takeover["start_sec"] + 5.0:
            operator.ai_requested = True

    selector = ScriptedSelector(["search", "aim_and_fire", "hold"])
    runner, flight = build_runner(
        selector, operator=operator, on_advance=take_over_after_burst, redecide_interval_sec=None
    )

    result = runner.run()

    resume = result.transitions[2]
    selector_records = [record for record in result.selections if record.source != SOURCE_FORCED]
    resume_index = next(index for index, record in enumerate(selector_records) if record.time_sec == resume.time_sec)
    resume_state, _ = selector.calls[resume_index]
    assert result.end_reason == END_LANDED
    assert (resume.from_mode, resume.to_mode) == (MODE_MANUAL, MODE_AI)
    assert resume_state.mission.hearted_tracks == ("T1",)
    assert [heart.track_id for heart in get_fire_hearts(flight)] == [1] * 10


# --- 임무 루프: 상태 알림·구조화 로그 (LOG-01, GUARD-03) ---


def test_on_status_reports_mode_link_and_running_mode_every_tick():
    statuses = []
    selector = ScriptedSelector(["return_home"])
    runner, _ = build_runner(selector, on_status=statuses.append)

    result = runner.run()

    assert result.is_success
    assert len(statuses) > 10
    assert all(
        set(status) == {"mode", "reason", "link_connected", "link_last_heartbeat_sec", "running_mode", "t"}
        for status in statuses
    )
    assert all(status["mode"] == MODE_AI for status in statuses)
    assert statuses[0]["reason"] == REASON_OPERATOR
    assert statuses[-1]["running_mode"] == "return_home"
    assert statuses[-1]["link_connected"] is True
    assert statuses[-1]["link_last_heartbeat_sec"] == statuses[-1]["t"]


def test_mission_log_records_structured_kinds():
    mission_log = MissionLog()
    selector = RuleSelector(ParamDefaults())
    runner, _ = build_runner(selector, config=MissionConfig(max_mission_sec=300.0), mission_log=mission_log)

    result = runner.run()

    records = mission_log.records
    kinds = {record["kind"] for record in records}
    selection_records = [record for record in records if record["kind"] == KIND_SELECTION]
    mission_records = [record for record in records if record["kind"] == KIND_MISSION]
    detection_records = [record for record in records if record["kind"] == KIND_DETECTION]
    assert result.is_success
    assert {KIND_MODE_TRANSITION, KIND_SELECTION, KIND_DETECTION, KIND_HEART, KIND_RETURN, KIND_LINK, KIND_MISSION} <= kinds
    assert all({"source", "confidence", "fallback", "params", "mode"} <= set(record) for record in selection_records)
    assert len(mission_records) == 1
    assert mission_records[0]["event"] == "end"
    assert mission_records[0]["end_reason"] == END_LANDED
    assert 0 < len(detection_records) < 10
