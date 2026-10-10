"""RM-02 / HRT-01 — aim_and_fire 모드: 안정 카운트, 10발 연사, 거절·놓침·시간 초과, 헤드리스 수렴, factory."""

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import (
    CommandAck,
    FireHeart,
    GoTo,
    SetGimbal,
    SetVelocity,
    SetYaw,
    TakeOff,
    Telemetry,
)
from modules.perception.interface import DEFAULT_CAMERA, Track
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.aim_and_fire import AimAndFireMode
from modules.rule_mode.factory import SUPPORTED_RULE_MODES, create_rule_mode
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings
from modules.rule_mode.observe import ObserveMode
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource
from modules.yolo_detection.interface import Detection, Frame

TICK_SEC = 0.1
CRUISE_ALTITUDE_M = 15.0
WIDTH_PX = 1280
HEIGHT_PX = 720
CENTER_U = WIDTH_PX / 2
OFF_CENTER_U = WIDTH_PX / 2 + 200.0
AIM_PARAMS = {"target_track": "T3", "center_tolerance_ratio": 0.05, "stable_frame_count": 5, "timeout_sec": 15.0}
SUCCESS = ModeResult("aim_and_fire", "success", "T3")
FAILURE = ModeResult("aim_and_fire", "failure", "T3")


# --- 합성 입력 helper ---


def _make_telemetry(time_sec: float = 0.0) -> Telemetry:
    return Telemetry(
        time_sec=time_sec,
        north_m=0.0,
        east_m=0.0,
        down_m=-CRUISE_ALTITUDE_M,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=0.0,
        gimbal_pitch_deg=-30.0,
        gimbal_yaw_deg=0.0,
        battery_pct=100.0,
        flight_mode="hover",
    )


def _make_frame(frame_id: int, center_u: float = CENTER_U, center_v: float = HEIGHT_PX / 2, track_id: int = 3) -> Frame:
    box = (center_u - 20.0, center_v - 40.0, center_u + 20.0, center_v + 40.0)

    return Frame(frame_id, frame_id * TICK_SEC, WIDTH_PX, HEIGHT_PX, (Detection(0, "person", 0.9, box, track_id),))


def _make_empty_frame(frame_id: int) -> Frame:
    return Frame(frame_id, frame_id * TICK_SEC, WIDTH_PX, HEIGHT_PX, ())


def _make_track(track_id: int = 3, is_active: bool = True) -> Track:
    return Track(track_id, "person", 20.0, 0.0, 0.0, 0.0, 5, 0.9, is_active)


def _make_tick(now_sec: float, frame: Frame | None, tracks=None) -> ModeTick:
    return ModeTick(
        now_sec=now_sec,
        telemetry=_make_telemetry(now_sec),
        tracks=tuple(tracks) if tracks is not None else (_make_track(),),
        frame=frame,
        frame_telemetry=_make_telemetry(now_sec) if frame is not None else None,
        hearted_track_ids=frozenset(),
    )


class _RecordingControl:
    """명령을 기록한다. rejected_kinds 의 명령, reject_fire_from 번째(1부터) 이후의 FireHeart 를 거절한다."""

    def __init__(self, rejected_kinds=(), reject_fire_from: int | None = None):
        self.command_list = []
        self._rejected_kinds = set(rejected_kinds)
        self._reject_fire_from = reject_fire_from
        self._fire_count = 0

    @property
    def fire_list(self):
        return [command for command in self.command_list if isinstance(command, FireHeart)]

    def get_telemetry(self):
        return _make_telemetry()

    def send_command(self, command):
        self.command_list.append(command)
        if isinstance(command, FireHeart):
            self._fire_count += 1
            if self._reject_fire_from is not None and self._fire_count >= self._reject_fire_from:
                return CommandAck(False, "fire rejected for test")
        if command.KIND in self._rejected_kinds:
            return CommandAck(False, "rejected for test")
        return CommandAck(True, "")


class _TickDriver:
    """now_sec 와 frame_id 를 0.1 s / 1 씩 올리며 step 을 부른다."""

    def __init__(self, mode, control):
        self.mode = mode
        self.control = control
        self.frame_id = 0
        self.now_sec = 0.0

    def step(self, center_u: float | None = CENTER_U, frame_id: int | None = None, tracks=None):
        used_frame_id = self.frame_id if frame_id is None else frame_id
        frame = _make_empty_frame(used_frame_id) if center_u is None else _make_frame(used_frame_id, center_u)
        result = self.mode.step(_make_tick(self.now_sec, frame, tracks), self.control)
        self.frame_id += 1
        self.now_sec = round(self.now_sec + TICK_SEC, 6)

        return result

    def step_many(self, count: int, center_u: float | None = CENTER_U, tracks=None):
        result_list = [self.step(center_u, tracks=tracks) for _ in range(count)]

        return [result for result in result_list if result is not None]


def _make_driver(params=AIM_PARAMS, control=None):
    return _TickDriver(AimAndFireMode(params, RuleModeSettings()), control or _RecordingControl())


# --- params ---


def test_aim_and_fire_is_a_rule_mode_and_validates_params():
    mode = AimAndFireMode(AIM_PARAMS, RuleModeSettings())

    assert isinstance(mode, RuleMode)
    assert mode.rule_mode == "aim_and_fire"
    AimAndFireMode({**AIM_PARAMS, "target_track": 3}, RuleModeSettings())

    for bad_params in (
        {**AIM_PARAMS, "target_track": "X3"},
        {**AIM_PARAMS, "center_tolerance_ratio": 0},
        {**AIM_PARAMS, "center_tolerance_ratio": 0.6},
        {**AIM_PARAMS, "center_tolerance_ratio": -0.1},
        {**AIM_PARAMS, "stable_frame_count": 0},
        {**AIM_PARAMS, "stable_frame_count": 1.5},
        {**AIM_PARAMS, "stable_frame_count": True},
        {**AIM_PARAMS, "timeout_sec": 0},
    ):
        with pytest.raises(ValueError):
            AimAndFireMode(bad_params, RuleModeSettings())


def test_missing_params_use_param_defaults():
    driver = _make_driver(params={"target_track": "T3"})

    assert driver.step_many(4) == []
    assert driver.control.fire_list == []
    driver.step()
    assert driver.control.fire_list == [FireHeart(3)]


# --- 안정 카운트 (D-09, D-10) ---


def test_off_center_frame_resets_stable_count():
    driver = _make_driver()

    driver.step_many(4)
    driver.step(OFF_CENTER_U)
    driver.step_many(4)
    assert driver.control.fire_list == []

    driver.step()
    assert driver.control.fire_list == [FireHeart(3)]


def test_repeated_frame_id_does_not_count_as_stable_frame():
    driver = _make_driver()

    for _ in range(4):
        driver.step()
    for _ in range(3):
        driver.step(frame_id=3)
    assert driver.control.fire_list == []

    driver.step()
    assert driver.control.fire_list == [FireHeart(3)]


# --- 연사 (D-12, D-14) ---


def test_burst_fires_ten_hearts_at_interval_then_succeeds():
    driver = _make_driver()
    fire_tick_list = []

    result = None
    for _ in range(40):
        before_count = len(driver.control.fire_list)
        tick_sec = driver.now_sec
        result = driver.step()
        if len(driver.control.fire_list) > before_count:
            fire_tick_list.append(tick_sec)
        if result is not None:
            break

    assert result == SUCCESS
    assert driver.control.fire_list == [FireHeart(3)] * 10
    assert [round(b - a, 6) for a, b in zip(fire_tick_list, fire_tick_list[1:])] == [0.2] * 9
    assert driver.step() is None


def test_burst_keeps_aiming_and_firing_when_target_drifts_off_center():
    driver = _make_driver()

    driver.step_many(5)
    assert len(driver.control.fire_list) == 1

    yaw_count_before = sum(isinstance(command, SetYaw) for command in driver.control.command_list)
    result_list = driver.step_many(18, OFF_CENTER_U)
    yaw_count_after = sum(isinstance(command, SetYaw) for command in driver.control.command_list)

    assert result_list == [SUCCESS]
    assert len(driver.control.fire_list) == 10
    assert yaw_count_after - yaw_count_before == 18


def test_first_heart_rejected_fails_immediately():
    driver = _make_driver(control=_RecordingControl(reject_fire_from=1))

    result_list = driver.step_many(10)

    assert result_list == [FAILURE]
    assert len(driver.control.fire_list) == 1


def test_fourth_heart_rejected_fails_and_stops_firing():
    driver = _make_driver(control=_RecordingControl(reject_fire_from=4))

    result_list = driver.step_many(30)

    assert result_list == [FAILURE]
    assert len(driver.control.fire_list) == 4


@pytest.mark.parametrize("rejected_kind", [SetYaw.KIND, SetGimbal.KIND])
def test_rejected_centering_command_fails(rejected_kind):
    driver = _make_driver(control=_RecordingControl({rejected_kind}))

    assert driver.step() == FAILURE


# --- 놓침 (D-11) · 시간 초과 ---


def test_short_loss_resets_stable_count_without_commands():
    driver = _make_driver()

    driver.step_many(4)
    command_count = len(driver.control.command_list)
    assert driver.step_many(10, center_u=None, tracks=()) == []
    assert len(driver.control.command_list) == command_count

    driver.step_many(4)
    assert driver.control.fire_list == []
    driver.step()
    assert driver.control.fire_list == [FireHeart(3)]


def test_loss_over_grace_fails():
    driver = _make_driver()

    driver.step_many(3)
    result_list = driver.step_many(12, center_u=None, tracks=())

    assert result_list == [FAILURE]


def test_loss_during_burst_fails_after_grace():
    driver = _make_driver()

    driver.step_many(6)
    fire_count = len(driver.control.fire_list)
    result_list = driver.step_many(12, center_u=None, tracks=(_make_track(is_active=False),))

    assert fire_count == 1
    assert result_list == [FAILURE]
    assert len(driver.control.fire_list) < 10


def test_timeout_before_firing():
    driver = _make_driver(params={**AIM_PARAMS, "timeout_sec": 1.0})

    result_list = driver.step_many(15, OFF_CENTER_U)

    assert result_list == [ModeResult("aim_and_fire", "timeout", "T3")]
    assert driver.control.fire_list == []


def test_never_sends_position_commands():
    driver = _make_driver()

    driver.step_many(4)
    driver.step(OFF_CENTER_U)
    driver.step_many(30)

    assert not any(isinstance(command, (GoTo, SetVelocity)) for command in driver.control.command_list)


# --- 헤드리스 (운동 모델 + 스크립트 대상 + 실제 TrackAggregator) ---


def _run_aim(targets, params=AIM_PARAMS, max_sec=30.0):
    """aim_and_fire 를 10 Hz 로 돌린다. (결과, control, {tick 시각: 프레임}) 을 돌려준다."""
    control = KinematicFlightControl()
    control.send_command(TakeOff(CRUISE_ALTITUDE_M))
    while control.get_telemetry().flight_mode != "hover":
        control.advance(TICK_SEC)

    control.send_command(SetGimbal(-30.0, 0.0))
    control.advance(1.0)

    source = ScriptedTargetSource(control, targets)
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    mode = AimAndFireMode(params, RuleModeSettings())
    start_sec = control.now_sec
    frame_by_sec = {}

    while control.now_sec - start_sec < max_sec:
        frame = source.read_frame()
        frame_telemetry = source.telemetry_for(frame.timestamp_sec)
        aggregator.update(frame, frame_telemetry)
        frame_by_sec[control.now_sec] = frame
        tick = ModeTick(
            now_sec=control.now_sec,
            telemetry=control.get_telemetry(),
            tracks=tuple(aggregator.snapshot(control.now_sec)),
            frame=frame,
            frame_telemetry=frame_telemetry,
            hearted_track_ids=frozenset(),
        )

        result = mode.step(tick, control)
        if result is not None:
            return result, control, frame_by_sec

        control.advance(TICK_SEC)

    return None, control, frame_by_sec


def test_aim_and_fire_headless_centers_offset_target_and_fires_ten_hearts():
    result, control, frame_by_sec = _run_aim([ScriptedTarget(3, "person", 20.0, 6.0)])

    assert result == SUCCESS

    fire_list = [
        (sent_sec, command) for sent_sec, command in control.commands_received if isinstance(command, FireHeart)
    ]
    assert [command for _, command in fire_list] == [FireHeart(3)] * 10
    for (previous_sec, _), (next_sec, _) in zip(fire_list, fire_list[1:]):
        assert next_sec - previous_sec == pytest.approx(0.2, abs=0.05)

    first_fire_frame = frame_by_sec[fire_list[0][0]]
    detection = next(detection for detection in first_fire_frame.detections if detection.track_id == 3)
    x1, y1, x2, y2 = detection.xyxy
    assert abs((x1 + x2) / 2.0 - WIDTH_PX / 2) <= 0.05 * WIDTH_PX
    assert abs((y1 + y2) / 2.0 - HEIGHT_PX / 2) <= 0.05 * HEIGHT_PX

    assert not any(isinstance(command, (GoTo, SetVelocity)) for _, command in control.commands_received)


# --- factory (C6) ---


def test_factory_creates_aim_and_fire_and_observe():
    settings = RuleModeSettings()

    assert isinstance(create_rule_mode("aim_and_fire", AIM_PARAMS, settings), AimAndFireMode)
    assert isinstance(create_rule_mode("observe", {"target_track": "T3", "timeout_sec": 10.0}, settings), ObserveMode)
    assert SUPPORTED_RULE_MODES == ("search", "approach", "hold", "return_home", "aim_and_fire", "observe")


def test_factory_rejects_unknown_name_with_value_error():
    with pytest.raises(ValueError):
        create_rule_mode("bogus", {}, RuleModeSettings())
