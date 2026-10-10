"""RM-06 — observe 모드: 중앙 맞춤 추적, 색 확정 success, 놓침·시간 초과·거절, 헤드리스 수렴."""

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import CommandAck, SetGimbal, SetYaw, TakeOff, Telemetry
from modules.perception.interface import DEFAULT_CAMERA, Track
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings
from modules.rule_mode.observe import ObserveMode
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource
from modules.yolo_detection.interface import Detection, Frame

TICK_SEC = 0.1
CRUISE_ALTITUDE_M = 15.0
WIDTH_PX = 1280
HEIGHT_PX = 720
OBSERVE_PARAMS = {"target_track": "T7", "timeout_sec": 10.0}


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


def _make_frame(frame_id: int, center_u: float | None = WIDTH_PX / 2 + 100.0, track_id: int = 7) -> Frame:
    detections = ()
    if center_u is not None:
        center_v = HEIGHT_PX / 2
        detections = (
            Detection(0, "person", 0.9, (center_u - 20.0, center_v - 40.0, center_u + 20.0, center_v + 40.0), track_id),
        )

    return Frame(frame_id, frame_id * TICK_SEC, WIDTH_PX, HEIGHT_PX, detections)


def _make_track(track_id: int = 7, is_active: bool = True, upper_color: str | None = None) -> Track:
    return Track(
        track_id=track_id,
        class_name="person",
        north_m=20.0,
        east_m=0.0,
        first_seen_sec=0.0,
        last_seen_sec=0.0,
        observation_count=5,
        mean_confidence=0.9,
        is_active=is_active,
        upper_color=upper_color,
        upper_color_ratio=None if upper_color is None else 0.8,
        upper_color_vote_count=0 if upper_color is None else 6,
    )


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
    def __init__(self, rejected_kinds=()):
        self.command_list = []
        self._rejected_kinds = set(rejected_kinds)

    def get_telemetry(self):
        return _make_telemetry()

    def send_command(self, command):
        self.command_list.append(command)
        if command.KIND in self._rejected_kinds:
            return CommandAck(False, "rejected for test")
        return CommandAck(True, "")


# --- params ---


def test_observe_is_a_rule_mode_and_validates_params():
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())

    assert isinstance(mode, RuleMode)
    assert mode.rule_mode == "observe"
    ObserveMode({"target_track": 7}, RuleModeSettings())

    for bad_params in (
        {**OBSERVE_PARAMS, "target_track": "X7"},
        {**OBSERVE_PARAMS, "target_track": -1},
        {**OBSERVE_PARAMS, "timeout_sec": -1},
    ):
        with pytest.raises(ValueError):
            ObserveMode(bad_params, RuleModeSettings())


# --- 합성 tick ---


def test_observe_tracks_with_yaw_and_gimbal_only_until_color_is_known():
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())
    control = _RecordingControl()

    for index in range(5):
        assert mode.step(_make_tick(index * TICK_SEC, _make_frame(index)), control) is None

    tracks = (_make_track(upper_color="red"),)
    result = mode.step(_make_tick(0.5, _make_frame(5), tracks), control)

    assert result == ModeResult("observe", "success", "T7")
    assert len(control.command_list) == 12
    assert all(isinstance(command, (SetYaw, SetGimbal)) for command in control.command_list)
    assert mode.step(_make_tick(0.6, _make_frame(6)), control) is None


def test_observe_succeeds_for_other_color_too():
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())

    result = mode.step(_make_tick(0.0, _make_frame(0), (_make_track(upper_color="other"),)), _RecordingControl())

    assert result == ModeResult("observe", "success", "T7")


def test_observe_skips_command_for_repeated_frame_and_frameless_tick():
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())
    control = _RecordingControl()

    mode.step(_make_tick(0.0, _make_frame(0)), control)
    mode.step(_make_tick(0.1, _make_frame(0)), control)
    mode.step(_make_tick(0.2, None), control)

    assert len(control.command_list) == 2


@pytest.mark.parametrize(
    ("missing_sec", "expected"),
    [(0.9, None), (1.1, ModeResult("observe", "failure", "T7"))],
)
def test_observe_target_loss_grace(missing_sec, expected):
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())
    control = _RecordingControl()

    mode.step(_make_tick(0.0, _make_frame(0)), control)
    mode.step(_make_tick(0.1, _make_frame(1, center_u=None), tracks=()), control)
    result = mode.step(_make_tick(0.1 + missing_sec, _make_frame(2, center_u=None), tracks=()), control)

    assert result == expected


def test_observe_counts_inactive_track_as_missing():
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())
    inactive = (_make_track(is_active=False),)

    mode.step(_make_tick(0.0, None, inactive), _RecordingControl())
    result = mode.step(_make_tick(1.2, None, inactive), _RecordingControl())

    assert result == ModeResult("observe", "failure", "T7")


def test_observe_times_out_without_extra_commands():
    mode = ObserveMode({**OBSERVE_PARAMS, "timeout_sec": 1.0}, RuleModeSettings())
    control = _RecordingControl()

    mode.step(_make_tick(0.0, _make_frame(0)), control)
    result = mode.step(_make_tick(1.0, _make_frame(1)), control)

    assert result == ModeResult("observe", "timeout", "T7")
    assert len(control.command_list) == 2


@pytest.mark.parametrize("rejected_kind", [SetYaw.KIND, SetGimbal.KIND])
def test_observe_fails_when_centering_command_is_rejected(rejected_kind):
    mode = ObserveMode(OBSERVE_PARAMS, RuleModeSettings())

    result = mode.step(_make_tick(0.0, _make_frame(0)), _RecordingControl({rejected_kind}))

    assert result == ModeResult("observe", "failure", "T7")


# --- 헤드리스 (운동 모델 + 스크립트 대상 + 실제 TrackAggregator) ---


def _run_observe(targets, params=OBSERVE_PARAMS, max_sec=30.0):
    """observe 를 10 Hz 로 돌린다. (결과, 경과 초, control, 마지막 프레임) 를 돌려준다."""
    control = KinematicFlightControl()
    control.send_command(TakeOff(CRUISE_ALTITUDE_M))
    while control.get_telemetry().flight_mode != "hover":
        control.advance(TICK_SEC)

    control.send_command(SetGimbal(-30.0, 0.0))
    control.advance(1.0)

    source = ScriptedTargetSource(control, targets)
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    mode = ObserveMode(params, RuleModeSettings())
    start_sec = control.now_sec
    frame = None

    while control.now_sec - start_sec < max_sec:
        elapsed_sec = control.now_sec - start_sec
        frame = source.read_frame()
        frame_telemetry = source.telemetry_for(frame.timestamp_sec)
        aggregator.update(frame, frame_telemetry)
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
            return result, elapsed_sec, control, frame

        control.advance(TICK_SEC)

    return None, max_sec, control, frame


def _box_center_error_px(frame: Frame, track_id: int) -> tuple[float, float]:
    detection = next(detection for detection in frame.detections if detection.track_id == track_id)
    x1, y1, x2, y2 = detection.xyxy

    return (x1 + x2) / 2.0 - frame.image_width / 2.0, (y1 + y2) / 2.0 - frame.image_height / 2.0


def test_observe_headless_centers_offset_target_and_times_out_without_color():
    params = {**OBSERVE_PARAMS, "timeout_sec": 5.0}

    result, end_sec, control, frame = _run_observe([ScriptedTarget(7, "person", 20.0, 6.0)], params=params)

    assert result == ModeResult("observe", "timeout", "T7")
    assert end_sec == pytest.approx(5.0, abs=0.15)
    error_x_px, error_y_px = _box_center_error_px(frame, 7)
    assert abs(error_x_px) <= 0.05 * WIDTH_PX
    assert abs(error_y_px) <= 0.05 * HEIGHT_PX
    assert all(isinstance(command, (TakeOff, SetYaw, SetGimbal)) for _, command in control.commands_received)


def test_observe_headless_succeeds_once_aggregator_confirms_color():
    target = ScriptedTarget(7, "person", 20.0, 6.0, upper_color="red")

    result, end_sec, _, _ = _run_observe([target])

    assert result == ModeResult("observe", "success", "T7")
    assert end_sec < 3.0
