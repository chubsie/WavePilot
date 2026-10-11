"""재판단 안정성 — 실측(run6, 규칙 선택기)에서 본 세 문제의 회귀 테스트.

1. 트래커가 2 s 못 본 대상을 비활성으로 표시하면 규칙 선택기는 periodic 재판단에 search 를 고른다. 모드는 아직 자기
   놓침 판정을 내리지 않았는데 루프가 시작 1~2 s 된 observe/approach 를 버렸다 → 대상이 살아 있는 동안은 유지.
2. approach 가 같은 표적의 새 id 로 갈아탄 뒤 선택기가 새 id 를 고르면 '다른 결정'으로 봤다 → 같은 표적이면 같은 결정.
3. aim_and_fire 연사 중 sim 시계가 튀어(벽시계 0.3 s 에 1.4 s) 한두 step 놓친 것이 유예를 넘은 failure 가 됐다.
"""

import logging
import math

import pytest

from modules.decision.interface import FALLBACK_NONE, SOURCE_RULE, Latency, SelectionResult
from modules.decision.interface.state import ModeResult
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.drone.mission_loop import (
    SOURCE_FORCED,
    TARGET_KEEP_SEC,
    MissionConfig,
    MissionRunner,
    build_rule_mode_settings,
)
from modules.flight_control.interface import CommandAck, FireHeart, SetVelocity, Telemetry
from modules.perception.interface import Track
from modules.perception.tracking import LOST_AFTER_SEC
from modules.rule_mode.aim_and_fire import AimAndFireMode
from modules.rule_mode.factory import create_rule_mode
from modules.rule_mode.interface import ModeTick, RuleModeSettings
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTargetSource
from modules.yolo_detection.interface import Detection, Frame

TICK_SEC = 0.1
TOLERANCE_SEC = TICK_SEC + 1e-6
TARGET_NORTH_M = 10.0  # 규칙 선택기가 approach 가 아니라 observe 를 고르는 거리(15 m) 안
MISSION_COLOR = "red"  # 색을 모르는 T1 은 확정 track 이 아니라 aim_and_fire·approach(2줄) 대신 observe 가 된다


# --- 루프 도우미 ---


class EndlessMode:
    """끝나지 않는 가짜 모드 — 모드 자신의 놓침 판정 없이 루프의 재판단만 본다."""

    def __init__(self, rule_mode: str, target_track: str | None) -> None:
        self.rule_mode = rule_mode
        self.target_track = target_track
        self.step_times: list[float] = []

    def step(self, tick, control):
        self.step_times.append(tick.now_sec)
        control.send_command(SetVelocity(0.0, 0.0, 0.0, TICK_SEC))
        return None


class ModeFactorySpy:
    def __init__(self) -> None:
        self.created: list = []

    def __call__(self, rule_mode, params, settings):
        if rule_mode == "return_home":
            mode = create_rule_mode(rule_mode, params, settings)
        else:
            mode = EndlessMode(rule_mode, params.get("target_track"))
        self.created.append(mode)

        return mode

    def get_choices(self) -> list[tuple[str, str | None]]:
        return [(mode.rule_mode, getattr(mode, "target_track", None)) for mode in self.created]


class ScriptedTracks:
    """TrackAggregator 자리 — 프레임은 무시하고 tracks_at(now) 가 돌려주는 track 을 스냅샷으로 낸다."""

    def __init__(self, tracks_at) -> None:
        self._tracks_at = tracks_at

    def update(self, frame, telemetry) -> None:
        return None

    def snapshot(self, now_sec: float) -> list[Track]:
        return list(self._tracks_at(now_sec))


class SelectorSpy:
    """선택기를 감싸 부른 시각과 결과 (rule_mode, target_track) 를 기록한다."""

    def __init__(self, inner, clock) -> None:
        self._inner = inner
        self._clock = clock
        self.calls: list[tuple[float, str, str | None]] = []

    def select(self, state, event):
        result = self._inner.select(state, event)
        self.calls.append((self._clock(), result.rule_mode, result.params.get("target_track")))

        return result

    @property
    def first_call_sec(self) -> float | None:
        return self.calls[0][0] if self.calls else None


class ScriptedChoiceSelector:
    """choose(call_index) -> (rule_mode, target_track)."""

    def __init__(self, choose) -> None:
        self._choose = choose
        self._call_count = 0

    def select(self, state, event):
        rule_mode, target_track = self._choose(self._call_count)
        self._call_count += 1

        return SelectionResult(
            rule_mode=rule_mode,
            params={"target_track": target_track},
            confidence=1.0,
            probabilities={rule_mode: 1.0},
            source=SOURCE_RULE,
            fallback_reason=FALLBACK_NONE,
            options=(rule_mode,),
            latency=Latency(0.0, None, None),
            input_tokens=None,
        )


def make_track(
    track_id: int,
    now_sec: float,
    last_seen_sec: float,
    north_m: float = TARGET_NORTH_M,
    east_m: float = 0.0,
    upper_color: str | None = None,
) -> Track:
    seen_sec = min(now_sec, last_seen_sec)
    return Track(
        track_id=track_id,
        class_name="person",
        north_m=north_m,
        east_m=east_m,
        first_seen_sec=0.0,
        last_seen_sec=seen_sec,
        observation_count=5,
        mean_confidence=0.9,
        is_active=now_sec - seen_sec <= LOST_AFTER_SEC,
        upper_color=upper_color,
    )


def build_runner(decision_model_for, tracks_at, max_mission_sec=30.0, target_upper_color=None, **overrides):
    flight = KinematicFlightControl()
    defaults = ParamDefaults()
    factory = ModeFactorySpy()
    decision_model = decision_model_for(flight)
    runner = MissionRunner(
        flight_control=flight,
        frame_source=ScriptedTargetSource(flight, []),
        aggregator=ScriptedTracks(tracks_at),
        decision_model=decision_model,
        param_defaults=defaults,
        settings=build_rule_mode_settings(defaults, target_upper_color=target_upper_color),
        clock=lambda: flight.now_sec,
        advance=flight.advance,
        config=MissionConfig(max_mission_sec=max_mission_sec),
        mode_factory=factory,
        **overrides,
    )

    return runner, decision_model, factory


def run_rule_selector_with_target_seen_until(seen_for_sec: float, upper_color_after=None):
    """규칙 선택기로 임무를 돌린다. 대상 T1 은 첫 선택 뒤 seen_for_sec 까지 보이고 그 뒤로는 못 본다."""
    spy_holder: dict = {}

    def decision_model_for(flight):
        spy_holder["spy"] = SelectorSpy(RuleSelector(ParamDefaults(), allow_return_home=False), lambda: flight.now_sec)
        return spy_holder["spy"]

    def get_seen_until_sec() -> float:
        first_call_sec = spy_holder["spy"].first_call_sec
        return math.inf if first_call_sec is None else first_call_sec + seen_for_sec

    def tracks_at(now_sec):
        first_call_sec = spy_holder["spy"].first_call_sec
        upper_color = None
        if upper_color_after is not None and first_call_sec is not None and now_sec >= first_call_sec + 1.0:
            upper_color = upper_color_after
        return (make_track(1, now_sec, get_seen_until_sec(), upper_color=upper_color),)

    runner, spy, factory = build_runner(decision_model_for, tracks_at, target_upper_color=MISSION_COLOR)
    result = runner.run()

    return result, spy, factory, get_seen_until_sec()


def get_switch_sec(result, rule_mode: str) -> float:
    return next(record.time_sec for record in result.selections if record.rule_mode == rule_mode)


# --- 1. 실행 중 대상이 살아 있으면 search 로 되돌리지 않는다 ---


def test_observe_is_kept_while_inactive_target_was_seen_recently(caplog):
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    result, spy, factory, seen_until_sec = run_rule_selector_with_target_seen_until(seen_for_sec=0.5)

    search_answers = [call_sec for call_sec, rule_mode, _ in spy.calls if rule_mode == "search"]
    first_search_answer_sec = search_answers[0]
    switch_sec = get_switch_sec(result, "search")
    assert factory.get_choices()[:2] == [("observe", "T1"), ("search", None)]
    # 규칙 선택기는 트래커가 비활성으로 표시하자마자(2 s) search 를 답하지만 루프는 그때 바꾸지 않는다
    assert first_search_answer_sec == pytest.approx(seen_until_sec + LOST_AFTER_SEC, abs=1.0 + TOLERANCE_SEC)
    assert switch_sec > first_search_answer_sec
    assert "reason=target_alive" in caplog.text
    assert "redecide t=" not in caplog.text.split("reason=target_alive")[0]  # 유지 전에 전환이 없다


def test_lost_target_allows_switch_to_search():
    result, _, factory, seen_until_sec = run_rule_selector_with_target_seen_until(seen_for_sec=0.5)

    switch_sec = get_switch_sec(result, "search")
    observe_mode = factory.created[0]
    assert switch_sec >= seen_until_sec + TARGET_KEEP_SEC - TOLERANCE_SEC
    assert switch_sec <= seen_until_sec + TARGET_KEEP_SEC + 1.0 + TOLERANCE_SEC  # 다음 재판단(1 s) 안에 바꾼다
    assert observe_mode.step_times[-1] == pytest.approx(switch_sec)


def test_target_that_stays_visible_is_never_abandoned():
    result, spy, factory, _ = run_rule_selector_with_target_seen_until(seen_for_sec=math.inf)

    selector_records = [record for record in result.selections if record.source != SOURCE_FORCED]
    assert [record.rule_mode for record in selector_records] == ["observe"]
    assert len(spy.calls) > 10  # 재판단은 계속 불렸고 모두 같은 결정이었다
    assert {(rule_mode, target) for _, rule_mode, target in spy.calls} == {("observe", "T1")}


def test_disqualified_target_allows_switch_while_still_visible():
    """색이 임무 색과 어긋나게 확정되면 보이고 있어도 대상이 아니다 — 바로 search 로 바꿔도 된다."""
    result, _, factory, _ = run_rule_selector_with_target_seen_until(
        seen_for_sec=math.inf, upper_color_after="blue"
    )

    first_selection_sec = result.selections[0].time_sec
    switch_sec = get_switch_sec(result, "search")
    assert factory.get_choices()[:2] == [("observe", "T1"), ("search", None)]
    assert switch_sec <= first_selection_sec + 1.0 + 1.0 + TOLERANCE_SEC  # 색이 바뀐 뒤 첫 재판단


# --- 2. 같은 표적의 id 이어받기는 같은 결정 ---


def build_scripted_runner(choose, payloads):
    def tracks_at(now_sec):
        return (
            make_track(56, now_sec, math.inf, north_m=10.0),
            make_track(57, now_sec, math.inf, north_m=11.7),  # T56 에서 1.7 m — 같은 표적
            make_track(80, now_sec, math.inf, north_m=20.0),  # T56 에서 10 m — 다른 사람
        )

    return build_runner(lambda flight: ScriptedChoiceSelector(choose), tracks_at, on_selection=payloads.append)


def test_same_target_new_id_is_the_same_decision():
    def choose(call_index):
        return ("approach", "T56") if call_index == 0 else ("approach", "T57")

    payloads: list[dict] = []
    runner, _, factory = build_scripted_runner(choose, payloads)

    result = runner.run()

    selector_records = [record for record in result.selections if record.source != SOURCE_FORCED]
    continued_payloads = [payload for payload in payloads if payload.get("continued")]
    assert factory.get_choices()[0] == ("approach", "T56")
    assert [record.rule_mode for record in selector_records] == ["approach"]  # 새 모드를 만들지 않았다
    assert len(continued_payloads) > 5


def test_followed_id_becomes_the_comparison_target(caplog):
    """T56→T57 로 이어받은 뒤에는 T57 이 기준 — 다시 T57 을 골라도 같고, 먼 T80 은 다른 결정이다."""
    caplog.set_level(logging.INFO, logger="modules.drone.mission_loop")

    def choose(call_index):
        if call_index == 0:
            return ("approach", "T56")
        return ("approach", "T57") if call_index < 5 else ("approach", "T80")

    runner, _, factory = build_scripted_runner(choose, [])

    runner.run()

    assert factory.get_choices()[:2] == [("approach", "T56"), ("approach", "T80")]
    assert "redecide t=" in caplog.text and "from=approach/T57 to=approach/T80" in caplog.text


def test_same_target_with_different_mode_still_switches():
    def choose(call_index):
        return ("approach", "T56") if call_index < 2 else ("observe", "T57")

    runner, _, factory = build_scripted_runner(choose, [])

    runner.run()

    assert factory.get_choices()[:2] == [("approach", "T56"), ("observe", "T57")]


# --- 3. aim_and_fire: 시계가 튀어도 한두 step 놓침으로 연사를 끝내지 않는다 ---

WIDTH_PX = 1280
HEIGHT_PX = 720


def _make_telemetry(time_sec: float) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=0.0, east_m=0.0, down_m=-15.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0, battery_pct=100.0, flight_mode="hover",
    )


def _make_frame(frame_id: int, now_sec: float, has_target: bool) -> Frame:
    center_u, center_v = WIDTH_PX / 2, HEIGHT_PX / 2
    box = (center_u - 20.0, center_v - 40.0, center_u + 20.0, center_v + 60.0)  # 조준점(박스 위 40%)이 화면 가운데
    detections = (Detection(0, "person", 0.9, box, 10),) if has_target else ()

    return Frame(frame_id, now_sec, WIDTH_PX, HEIGHT_PX, detections)


class _AcceptAllControl:
    def __init__(self) -> None:
        self.fire_list: list[FireHeart] = []

    def get_telemetry(self):
        return _make_telemetry(0.0)

    def send_command(self, command):
        if isinstance(command, FireHeart):
            self.fire_list.append(command)
        return CommandAck(True, "")


def _step_aim(mode, control, frame_id: int, now_sec: float, has_target: bool) -> ModeResult | None:
    track = Track(10, "person", 20.0, 0.0, 0.0, now_sec, 5, 0.9, True, "red")
    tick = ModeTick(
        now_sec=now_sec,
        telemetry=_make_telemetry(now_sec),
        tracks=(track,),
        frame=_make_frame(frame_id, now_sec, has_target),
        frame_telemetry=_make_telemetry(now_sec),
        hearted_track_ids=frozenset(),
    )
    return mode.step(tick, control)


def _run_aim_with_clock_jump(jump_sec: float) -> tuple[ModeResult | None, int]:
    """5발을 쏜 뒤 한 step 놓치고, 시계가 jump_sec 뛴 다음 step 도 놓치고, 그 뒤로는 다시 보인다."""
    mode = AimAndFireMode({"target_track": "T10"}, RuleModeSettings(target_upper_color="red"))
    control = _AcceptAllControl()
    now_sec = 0.0
    frame_id = 0
    result = None

    while len(control.fire_list) < 5 and result is None:
        result = _step_aim(mode, control, frame_id, now_sec, has_target=True)
        frame_id, now_sec = frame_id + 1, round(now_sec + TICK_SEC, 6)

    for has_target, advance_sec in ((False, jump_sec), (False, TICK_SEC)):
        if result is None:
            result = _step_aim(mode, control, frame_id, now_sec, has_target)
            frame_id, now_sec = frame_id + 1, round(now_sec + advance_sec, 6)

    for _ in range(40):
        if result is not None:
            break
        result = _step_aim(mode, control, frame_id, now_sec, has_target=True)
        frame_id, now_sec = frame_id + 1, round(now_sec + TICK_SEC, 6)

    return result, len(control.fire_list)


def test_aim_and_fire_survives_clock_jump_with_brief_loss():
    """run6 T10: 5발 뒤 sim 시계가 68.6→70.0 으로 뛰어 failure. 이제는 연사를 끝까지 쏜다."""
    result, fire_count = _run_aim_with_clock_jump(jump_sec=1.3)

    assert result == ModeResult("aim_and_fire", "success", "T10")
    assert fire_count == 10


def test_aim_and_fire_still_fails_on_sustained_loss():
    mode = AimAndFireMode({"target_track": "T10"}, RuleModeSettings(target_upper_color="red"))
    control = _AcceptAllControl()
    results = [
        _step_aim(mode, control, frame_id, frame_id * TICK_SEC, has_target=frame_id < 4) for frame_id in range(20)
    ]

    finished = [result for result in results if result is not None]
    assert finished == [ModeResult("aim_and_fire", "failure", "T10")]
    assert results.index(finished[0]) * TICK_SEC - 0.4 > RuleModeSettings().target_lost_grace_sec  # 유예가 지난 뒤
