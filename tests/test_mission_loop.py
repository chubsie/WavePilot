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
    EVENT_MAX_MISSION_TIME,
    EVENT_RETURN_RETRY,
    SOURCE_FORCED,
    MissionConfig,
    MissionRunner,
    build_rule_mode_settings,
)
from modules.drone.run_mission import MOCK_DETECTION_RANGE_M, RangeLimitedTargetSource
from modules.flight_control.interface import TakeOff
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.factory import (
    SUPPORTED_RULE_MODES,
    UnsupportedRuleModeError,
    create_rule_mode,
)
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


def test_factory_rejects_unknown_name_with_plain_value_error():
    with pytest.raises(ValueError) as error:
        create_rule_mode("bogus", {}, RuleModeSettings())

    assert not isinstance(error.value, UnsupportedRuleModeError)


# --- 임무 루프 도우미 ---

# 홈에서 60 m 라 탐지 거리(40 m) 밖 — search 가 다가가야 보인다
DEFAULT_PERSON = ScriptedTarget(track_id=1, class_name="person", north_m=60.0, east_m=0.0)


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
        elif rule_mode in ("aim_and_fire", "observe"):
            params = {"target_track": state.tracks[0].track_id if state.tracks else None}
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


def build_runner(decision_model, config=None, targets=(DEFAULT_PERSON,), **overrides):
    flight = KinematicFlightControl()
    defaults = ParamDefaults()
    source = RangeLimitedTargetSource(ScriptedTargetSource(flight, list(targets)), flight, MOCK_DETECTION_RANGE_M)
    runner = MissionRunner(
        flight_control=flight,
        frame_source=source,
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=decision_model,
        param_defaults=defaults,
        settings=build_rule_mode_settings(defaults),
        clock=lambda: flight.now_sec,
        advance=flight.advance,
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
    assert modes == ["search", "approach", "aim_and_fire", "search", "return_home"]
    assert result.mode_results[0] == ModeResult("search", "success", "T1")
    assert result.mode_results[1] == ModeResult("approach", "success", "T1")
    assert result.mode_results[2] == ModeResult("aim_and_fire", "success", "T1")
    assert result.mode_results[3].outcome == "failure"
    assert result.mode_results[-1] == ModeResult("return_home", "success", None)
    assert [record.rule_mode for record in result.selections] == ["search", "approach", "aim_and_fire", "search", "return_home"]
    assert result.selections[1].params["target_track"] == "T1"
    assert result.elapsed_sec < 300.0
    assert flight.commands_received[0][1] == TakeOff(15.0)


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


# --- 임무 루프: Phase 4 자리표시 ---


def test_aim_and_fire_placeholder_succeeds_and_marks_hearted(caplog):
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")
    selector = ScriptedSelector(["search", "aim_and_fire", "return_home"])
    runner, _ = build_runner(selector)

    result = runner.run()

    assert result.is_success
    assert result.mode_results[0].rule_mode == "search"
    assert result.mode_results[1] == ModeResult("aim_and_fire", "success", "T1")
    assert selector.calls[2][0].mission.hearted_tracks == ("T1",)
    assert selector.calls[2][1].mode_result == ModeResult("aim_and_fire", "success", "T1")
    assert "not implemented until Phase 4" in caplog.text


def test_observe_placeholder_fails_immediately():
    selector = ScriptedSelector(["search", "observe", "return_home"])
    runner, flight = build_runner(selector)

    result = runner.run()

    assert result.is_success
    assert result.mode_results[1] == ModeResult("observe", "failure", "T1")
    assert selector.calls[2][1].mode_result == ModeResult("observe", "failure", "T1")
    assert selector.calls[2][0].mission.hearted_tracks == ()


def test_loop_accepts_any_decision_model():
    selector = ScriptedSelector(["return_home"])
    runner, _ = build_runner(selector)

    result = runner.run()

    assert result.is_success
    assert [record.rule_mode for record in result.selections] == ["return_home"]


# --- 임무 루프: 최대 임무 시간 / 복귀 재시도 / 이륙 거절 ---


def test_max_mission_time_forces_return_home_without_selector():
    selector = ScriptedSelector(["hold"])
    runner, _ = build_runner(selector, config=MissionConfig(max_mission_sec=20.0))

    result = runner.run()

    assert result.is_success
    assert result.forced_return
    assert result.final_flight_mode == "grounded"
    assert len(selector.calls) == 1
    forced = result.selections[-1]
    assert forced.event_kind == EVENT_MAX_MISSION_TIME
    assert forced.source == SOURCE_FORCED
    assert forced.rule_mode == "return_home"
    assert result.mode_results[-1].rule_mode == "return_home"


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
