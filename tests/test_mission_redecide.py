"""1 s 재판단("다르면 전환") — mission_loop MissionRunner.redecide_interval_sec.

실행 중 모드가 있어도 간격마다 선택기를 다시 부르고, 같은 (rule_mode, target_track) 이면 계속, 다르면 버리고 바꾼다.
aim_and_fire 연사 중과 return_home 은 끊지 않는다. 같은 결정은 continued payload 로만 알린다.
"""

import logging
import math
from itertools import pairwise

import pytest

from modules.decision.interface import FALLBACK_NONE, SOURCE_RULE, Latency, SelectionResult
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_PERIODIC_CHECK,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionState,
)
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.trigger import SelectionTrigger
from modules.drone import run_mission
from modules.drone.mission_loop import (
    DEFAULT_REDECIDE_INTERVAL_SEC,
    END_LANDED,
    SOURCE_FORCED,
    MissionConfig,
    MissionRunner,
    build_rule_mode_settings,
)
from modules.flight_control.interface import FireHeart, SetVelocity
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.factory import create_rule_mode
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTargetSource

TICK_SEC = 0.1
TIME_TOLERANCE_SEC = TICK_SEC + 1e-6
FAKE_MODE_TIMEOUT_TICKS = 10_000  # 가짜 모드가 스스로 끝나지 않을 만큼


# --- 도우미 ---


class FakeMode:
    """명령 하나를 매 step 보내는 가짜 모드. fire_from_step 부터 burst_count 발을 쏘고 success, fail_on_step 이면 failure."""

    def __init__(
        self,
        rule_mode: str,
        target_track: str | None,
        fire_from_step: int | None = None,
        burst_count: int = 0,
        fail_on_step: int | None = None,
    ) -> None:
        self.rule_mode = rule_mode
        self.target_track = target_track
        self.step_times: list[float] = []
        self._fire_from_step = fire_from_step
        self._burst_count = burst_count
        self._fail_on_step = fail_on_step
        self._fired_count = 0

    def step(self, tick, control):
        self.step_times.append(tick.now_sec)
        step_index = len(self.step_times)
        if self._fail_on_step is not None and step_index >= self._fail_on_step:
            return ModeResult(self.rule_mode, "failure", self.target_track)

        is_fire_due = self._fire_from_step is not None and step_index >= self._fire_from_step
        if is_fire_due and self._fired_count < self._burst_count:
            control.send_command(FireHeart(1))
            self._fired_count += 1
            if self._fired_count == self._burst_count:
                return ModeResult(self.rule_mode, "success", self.target_track)
            return None

        control.send_command(SetVelocity(0.0, 0.0, 0.0, TICK_SEC))
        if step_index >= FAKE_MODE_TIMEOUT_TICKS:
            return ModeResult(self.rule_mode, "timeout", self.target_track)
        return None


class ModeFactorySpy:
    """return_home 은 진짜 모드(착륙까지), 나머지는 FakeMode. fake_options[rule_mode] 로 가짜 모드 동작을 정한다."""

    def __init__(self, fake_options: dict | None = None) -> None:
        self._fake_options = fake_options or {}
        self.created: list = []

    def __call__(self, rule_mode, params, settings):
        if rule_mode == "return_home":
            mode = create_rule_mode(rule_mode, params, settings)
        else:
            mode = FakeMode(rule_mode, params.get("target_track"), **self._fake_options.get(rule_mode, {}))
        self.created.append(mode)

        return mode

    def get_created_modes(self) -> list[str]:
        return [mode.rule_mode for mode in self.created]


class ScriptedChoiceSelector:
    """choose(call_index, state, event) -> (rule_mode, target_track). 부른 시각(clock)과 사건을 기록한다."""

    def __init__(self, choose, clock, on_select=None) -> None:
        self._choose = choose
        self._clock = clock
        self._on_select = on_select
        self.call_times: list[float] = []
        self.events: list = []

    def select(self, state, event):
        self.call_times.append(self._clock())
        self.events.append(event)
        rule_mode, target_track = self._choose(len(self.call_times) - 1, state, event)
        if self._on_select is not None:
            self._on_select()

        if rule_mode == "return_home":
            params = fill_params("return_home", state, event, ParamDefaults())
        else:
            params = {"target_track": target_track}

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


def build_runner(choose, max_mission_sec=20.0, fake_options=None, on_select=None, **overrides):
    flight = KinematicFlightControl()
    defaults = ParamDefaults()
    factory = ModeFactorySpy(fake_options)
    selector = ScriptedChoiceSelector(choose, lambda: flight.now_sec, on_select=on_select)
    runner = MissionRunner(
        flight_control=flight,
        frame_source=ScriptedTargetSource(flight, []),
        aggregator=TrackAggregator(DEFAULT_CAMERA),
        decision_model=selector,
        param_defaults=defaults,
        settings=build_rule_mode_settings(defaults),
        clock=lambda: flight.now_sec,
        advance=flight.advance,
        config=MissionConfig(max_mission_sec=max_mission_sec),
        mode_factory=factory,
        **overrides,
    )

    return runner, flight, selector, factory


def always(rule_mode, target_track=None):
    return lambda call_index, state, event: (rule_mode, target_track)


def get_gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in pairwise(times)]


def get_fire_hearts(flight) -> list[FireHeart]:
    return [command for _, command in flight.commands_received if isinstance(command, FireHeart)]


# --- 간격 ---


def test_default_interval_is_one_second():
    assert DEFAULT_REDECIDE_INTERVAL_SEC == 1.0


def test_selector_is_called_every_second_while_mode_runs():
    runner, _, selector, _ = build_runner(always("hold"))

    result = runner.run()

    gaps = get_gaps(selector.call_times)
    assert result.forced_return  # hold 가 끝나지 않아 max_flight_time 강제 복귀로 끝난다
    assert len(selector.call_times) >= 10
    assert all(abs(gap - 1.0) <= TIME_TOLERANCE_SEC for gap in gaps)


def test_interval_is_configurable():
    runner, _, selector, _ = build_runner(always("hold"), redecide_interval_sec=2.5)

    runner.run()

    gaps = get_gaps(selector.call_times)
    assert len(gaps) >= 3
    assert all(abs(gap - 2.5) <= TIME_TOLERANCE_SEC for gap in gaps)


def test_none_interval_restores_event_only_selection():
    runner, _, selector, _ = build_runner(always("hold"), redecide_interval_sec=None)

    runner.run()

    assert len(selector.call_times) == 1  # 첫 선택 뒤로는 사건이 없다(periodic 10 s 전에 강제 복귀도 아님)


@pytest.mark.parametrize("bad_interval", [0.0, -1.0, math.nan, math.inf, True])
def test_invalid_interval_is_rejected(bad_interval):
    with pytest.raises(ValueError, match="redecide_interval_sec"):
        build_runner(always("hold"), redecide_interval_sec=bad_interval)


def test_interval_counts_from_select_completion():
    """select 가 0.8 s 막히는 동안 시계가 흘러도, 다음 판단은 끝난 뒤 1 s 가 지나야 한다 — 틱이 굶지 않는다."""
    flight_holder = {}

    def block_for_point_eight_seconds():
        flight_holder["flight"].advance(0.8)

    runner, flight, selector, factory = build_runner(always("hold"), on_select=block_for_point_eight_seconds)
    flight_holder["flight"] = flight

    runner.run()

    gaps = get_gaps(selector.call_times)
    hold_mode = factory.created[0]
    assert len(gaps) >= 5
    assert all(abs(gap - 1.8) <= TIME_TOLERANCE_SEC for gap in gaps)
    assert len(hold_mode.step_times) >= 8 * len(gaps)  # 판단 사이마다 약 1 s(10 틱) 동안 모드가 돈다


# --- 같은 결정: 계속 ---


def test_same_decision_keeps_running_mode_without_command_gap():
    runner, flight, selector, factory = build_runner(always("hold"))

    result = runner.run()

    hold_mode = factory.created[0]
    selector_records = [record for record in result.selections if record.source != SOURCE_FORCED]
    assert factory.get_created_modes() == ["hold", "return_home"]
    assert len(selector_records) == 1  # 같은 결정은 selection 기록에 넣지 않는다
    assert all(abs(gap - TICK_SEC) <= 1e-6 for gap in get_gaps(hold_mode.step_times))  # 매 틱 step, 끊김 없음
    assert len(selector.call_times) > 1


def test_same_mode_with_different_target_switches():
    def choose(call_index, state, event):
        return ("observe", "T1") if call_index < 3 else ("observe", "T2")

    runner, _, _, factory = build_runner(choose)

    runner.run()

    observe_targets = [mode.target_track for mode in factory.created if mode.rule_mode == "observe"]
    assert observe_targets == ["T1", "T2"]


# --- 다른 결정: 전환 ---


def test_different_decision_abandons_and_switches(caplog):
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    def choose(call_index, state, event):
        return ("hold", None) if call_index < 3 else ("search", None)

    runner, _, selector, factory = build_runner(choose)

    result = runner.run()

    hold_mode, search_mode = factory.created[0], factory.created[1]
    switch_sec = selector.call_times[3]
    selector_records = [record for record in result.selections if record.source != SOURCE_FORCED]
    assert factory.get_created_modes() == ["hold", "search", "return_home"]
    assert [record.rule_mode for record in selector_records] == ["hold", "search"]
    assert selector_records[1].event_kind == "periodic_check"
    assert selector_records[1].time_sec == pytest.approx(switch_sec)
    assert "mode_abandoned" in caplog.text and "mode=hold reason=redecide" in caplog.text
    assert hold_mode.step_times[-1] == pytest.approx(switch_sec)  # 전환 틱까지만 step — 버린 뒤로는 부르지 않는다
    assert search_mode.step_times[0] == pytest.approx(switch_sec + TICK_SEC)


def test_switch_to_just_failed_choice_is_ignored(caplog):
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    def choose(call_index, state, event):
        if call_index == 0:
            return "approach", "T1"
        if event.kind == EVENT_MODE_ENDED:
            return "hold", None
        return "approach", "T1"  # 선택기 보기 필터가 없는 periodic 재판단이 방금 실패를 다시 고른다

    runner, _, selector, factory = build_runner(choose, fake_options={"approach": {"fail_on_step": 3}})

    runner.run()

    assert factory.get_created_modes() == ["approach", "hold", "return_home"]
    assert len(selector.call_times) > 3
    assert "redecide_ignored" in caplog.text and "reason=just_failed" in caplog.text


# --- 끊지 않는 모드 ---


def test_aim_and_fire_burst_is_not_interrupted():
    def choose(call_index, state, event):
        return ("aim_and_fire", "T1") if call_index == 0 else ("hold", None)

    burst = {"fire_from_step": 1, "burst_count": 30}  # 30 틱 = 3 s 연사 — 재판단 간격보다 길다
    runner, flight, selector, factory = build_runner(choose, fake_options={"aim_and_fire": burst})

    result = runner.run()

    aim_mode = factory.created[0]
    assert result.mode_results[0] == ModeResult("aim_and_fire", "success", "T1")
    assert len(get_fire_hearts(flight)) == 30
    assert len(aim_mode.step_times) == 30
    assert selector.call_times[1] >= aim_mode.step_times[-1]  # 연사가 끝나기 전에는 다시 판단하지 않는다
    assert selector.events[1].kind == EVENT_MODE_ENDED


def test_aim_and_fire_before_first_heart_can_be_switched():
    def choose(call_index, state, event):
        return ("aim_and_fire", "T1") if call_index == 0 else ("hold", None)

    aiming_long = {"fire_from_step": 50, "burst_count": 10}  # 5 s 조준 뒤에야 첫 발
    runner, flight, _, factory = build_runner(choose, fake_options={"aim_and_fire": aiming_long})

    result = runner.run()

    assert factory.get_created_modes()[:2] == ["aim_and_fire", "hold"]
    assert get_fire_hearts(flight) == []
    assert all(mode_result.rule_mode != "aim_and_fire" for mode_result in result.mode_results)


def test_selected_return_home_is_not_interrupted():
    def choose(call_index, state, event):
        return ("return_home", None) if call_index == 0 else ("hold", None)

    runner, _, selector, factory = build_runner(choose, max_mission_sec=120.0)

    result = runner.run()

    assert result.end_reason == END_LANDED
    assert not result.forced_return
    assert factory.get_created_modes() == ["return_home"]
    assert len(selector.call_times) == 1


def test_forced_return_is_not_interrupted():
    runner, _, selector, factory = build_runner(always("hold"))

    result = runner.run()

    forced = result.selections[-1]
    assert result.end_reason == END_LANDED
    assert forced.source == SOURCE_FORCED
    assert factory.get_created_modes() == ["hold", "return_home"]
    assert max(selector.call_times) < forced.time_sec


# --- continued payload ---


def test_continued_flag_marks_same_decisions_only():
    payloads: list[dict] = []

    def choose(call_index, state, event):
        return ("hold", None) if call_index < 3 else ("search", None)

    runner, _, selector, _ = build_runner(choose, on_selection=payloads.append)

    result = runner.run()

    selector_payloads = [payload for payload in payloads if payload["source"] != SOURCE_FORCED]
    new_decisions = [payload for payload in payloads if not payload["continued"]]
    assert len(selector_payloads) == len(selector.call_times)  # 매 판단이 payload 로 간다
    assert [payload["continued"] for payload in selector_payloads[:4]] == [False, True, True, False]
    assert all(payload["continued"] for payload in selector_payloads[4:])
    assert [payload["rule_mode"] for payload in new_decisions] == [record.rule_mode for record in result.selections]
    assert payloads[-1]["source"] == SOURCE_FORCED and payloads[-1]["continued"] is False


def test_continued_decision_is_debug_logged_not_recorded(caplog):
    caplog.set_level(logging.DEBUG, logger="modules.drone.mission_loop")
    runner, _, selector, _ = build_runner(always("hold"))

    result = runner.run()

    continued_records = [record for record in caplog.records if "continued" in record.getMessage()]
    assert len(continued_records) == len(selector.call_times) - 1
    assert all(record.levelno == logging.DEBUG for record in continued_records)
    assert len(result.selections) == 2  # hold 한 번 + 강제 복귀


# --- SelectionTrigger.reset_periodic ---


def make_empty_state(time_sec: float) -> SelectionState:
    drone = DroneState(
        north_m=0.0, east_m=0.0, down_m=-15.0, altitude_m=15.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-20.0, gimbal_yaw_deg=0.0, battery_pct=90.0, flight_mode="hover",
    )
    mission = MissionSummary(target_class="person", target_upper_color=None, hearted_tracks=())
    return SelectionState(time_sec=time_sec, drone=drone, tracks=(), mission=mission)


def test_trigger_reset_periodic_postpones_periodic_check():
    trigger = SelectionTrigger(10.0)
    trigger.poll(0.0, make_empty_state(0.0), None)

    trigger.reset_periodic(6.0)

    assert trigger.poll(10.0, make_empty_state(10.0), None) is None
    assert trigger.poll(16.0, make_empty_state(16.0), None).kind == EVENT_PERIODIC_CHECK


@pytest.mark.parametrize("bad_time", [math.nan, math.inf])
def test_trigger_reset_periodic_rejects_non_finite_time(bad_time):
    with pytest.raises(ValueError, match="now_sec must be finite"):
        SelectionTrigger().reset_periodic(bad_time)


def test_redecide_replaces_overlapping_periodic_check():
    runner, _, selector, _ = build_runner(always("hold"), max_mission_sec=30.0)

    runner.run()

    assert all(event.kind == EVENT_PERIODIC_CHECK for event in selector.events)
    assert all(abs(gap - 1.0) <= TIME_TOLERANCE_SEC for gap in get_gaps(selector.call_times))  # 10 s 주기가 끼지 않는다


# --- run_mission CLI ---


def test_cli_redecide_sec_defaults_to_one_second():
    args = run_mission.build_arg_parser().parse_args([])

    assert args.redecide_sec == DEFAULT_REDECIDE_INTERVAL_SEC


@pytest.mark.parametrize("bad_value", ["-1", "nan"])
def test_cli_rejects_invalid_redecide_sec(bad_value, capsys):
    with pytest.raises(SystemExit) as exc_info:
        run_mission.main(["--sim", "mock", "--redecide-sec", bad_value])

    assert exc_info.value.code == 2
    assert "--redecide-sec" in capsys.readouterr().err


@pytest.mark.parametrize(("flag_value", "expected"), [("2.5", 2.5), ("0", None)])
def test_cli_redecide_sec_reaches_runner(flag_value, expected, monkeypatch, tmp_path):
    received = {}

    class StopAfterInit(Exception):
        pass

    def capture_runner(*args, **kwargs):
        received.update(kwargs)
        raise StopAfterInit

    monkeypatch.setattr(run_mission, "DEFAULT_MISSION_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(run_mission, "MissionRunner", capture_runner)

    with pytest.raises(StopAfterInit):
        run_mission.main(["--sim", "mock", "--redecide-sec", flag_value])

    assert received["redecide_interval_sec"] == expected
