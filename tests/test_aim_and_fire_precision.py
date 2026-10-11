"""aim_and_fire 사격 속도·조준 정확도 — 브라우저 FPV 640x360(HFOV 90, VFOV 58.72°) 기준.

닫힌 루프 helper: 각도로 둔 표적을 현재 yaw·짐벌 pitch 로 640x360 핀홀에 투영해 박스를 만들고, 모드가 보낸
SetYaw·SetGimbal 을 다음 프레임 전에 그대로 반영한다(1 프레임 지연).
"""

import math
from itertools import pairwise

import pytest

from modules.decision.interface.state import ModeResult
from modules.decision.params import ParamDefaults
from modules.flight_control.interface import CommandAck, FireHeart, SetGimbal, SetYaw, Telemetry
from modules.perception.interface import Track
from modules.rule_mode.aim_and_fire import AIM_POINT_RATIO, AimAndFireMode
from modules.rule_mode.centering import PITCH_GAIN, YAW_GAIN, compute_centering
from modules.rule_mode.interface import ModeTick, RuleModeSettings
from modules.yolo_detection.interface import Detection, Frame

TICK_SEC = 0.1
WIDTH_PX = 640
HEIGHT_PX = 360
FOCAL_PX = 320.0  # HFOV 90 → (640/2)/tan(45°)
BOX_WIDTH_PX = 20.0
BOX_HEIGHT_PX = 44.0  # 8 m 고도·수평 8 m 의 사람
DEFAULTS = ParamDefaults()
DEFAULT_PARAMS = {
    "target_track": "T3",
    "center_tolerance_ratio": DEFAULTS.aim_center_tolerance_ratio,
    "stable_frame_count": DEFAULTS.aim_stable_frame_count,
    "timeout_sec": DEFAULTS.aim_timeout_sec,
}


# --- 합성 입력 helper ---


def _make_telemetry(now_sec: float, yaw_deg: float = 0.0, gimbal_pitch_deg: float = -45.0) -> Telemetry:
    return Telemetry(
        time_sec=now_sec,
        north_m=0.0,
        east_m=0.0,
        down_m=-8.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=yaw_deg,
        gimbal_pitch_deg=gimbal_pitch_deg,
        gimbal_yaw_deg=0.0,
        battery_pct=100.0,
        flight_mode="hover",
    )


def _make_box_at_aim_point(aim_u: float, aim_v: float) -> tuple[float, float, float, float]:
    top_v = aim_v - AIM_POINT_RATIO * BOX_HEIGHT_PX
    return (aim_u - BOX_WIDTH_PX / 2, top_v, aim_u + BOX_WIDTH_PX / 2, top_v + BOX_HEIGHT_PX)


def _make_frame(frame_id: int, boxes: dict[int, tuple[float, float, float, float]]) -> Frame:
    detections = tuple(Detection(0, "person", 0.9, box, track_id) for track_id, box in boxes.items())
    return Frame(frame_id, frame_id * TICK_SEC, WIDTH_PX, HEIGHT_PX, detections)


class _RecordingControl:
    def __init__(self) -> None:
        self.command_list: list = []

    @property
    def fire_list(self) -> list[FireHeart]:
        return [command for command in self.command_list if isinstance(command, FireHeart)]

    def get_telemetry(self) -> Telemetry:
        return _make_telemetry(0.0)

    def send_command(self, command) -> CommandAck:
        self.command_list.append(command)
        return CommandAck(True, "")

    def get_last(self, command_type):
        return next((c for c in reversed(self.command_list) if isinstance(c, command_type)), None)


class _ClosedLoop:
    """표적 방위·고각(도)을 640x360 에 투영하고, 모드의 SetYaw·SetGimbal 을 다음 프레임에 반영한다."""

    def __init__(self, params=None, yaw_deg: float = 0.0, gimbal_pitch_deg: float = -45.0) -> None:
        self.mode = AimAndFireMode(params or DEFAULT_PARAMS, RuleModeSettings())
        self.control = _RecordingControl()
        self.yaw_deg = yaw_deg
        self.gimbal_pitch_deg = gimbal_pitch_deg
        self.frame_id = 0
        self.now_sec = 0.0
        self.error_list: list[tuple[float, float]] = []

    def project(self, azimuth_deg: float, elevation_deg: float) -> tuple[float, float]:
        u = WIDTH_PX / 2 + FOCAL_PX * math.tan(math.radians(azimuth_deg - self.yaw_deg))
        v = HEIGHT_PX / 2 + FOCAL_PX * math.tan(math.radians(self.gimbal_pitch_deg - elevation_deg))
        return u, v

    def step(self, azimuth_deg: float, elevation_deg: float, track_id: int = 3) -> ModeResult | None:
        aim_u, aim_v = self.project(azimuth_deg, elevation_deg)
        self.error_list.append((aim_u - WIDTH_PX / 2, aim_v - HEIGHT_PX / 2))
        frame = _make_frame(self.frame_id, {track_id: _make_box_at_aim_point(aim_u, aim_v)})
        telemetry = _make_telemetry(self.now_sec, self.yaw_deg, self.gimbal_pitch_deg)
        tick = ModeTick(
            now_sec=self.now_sec,
            telemetry=telemetry,
            tracks=(Track(track_id, "person", 8.0, 0.0, 0.0, 0.0, 5, 0.9, True),),
            frame=frame,
            frame_telemetry=telemetry,
            hearted_track_ids=frozenset(),
        )
        result = self.mode.step(tick, self.control)

        last_yaw = self.control.get_last(SetYaw)
        last_gimbal = self.control.get_last(SetGimbal)
        if last_yaw is not None:
            self.yaw_deg = last_yaw.yaw_deg
        if last_gimbal is not None:
            self.gimbal_pitch_deg = last_gimbal.pitch_deg
        self.frame_id += 1
        self.now_sec = round(self.now_sec + TICK_SEC, 6)

        return result


def _first_fire_step(loop: _ClosedLoop, azimuth_deg: float, elevation_deg: float, max_steps: int = 50) -> int:
    for index in range(max_steps):
        loop.step(azimuth_deg, elevation_deg)
        if loop.control.fire_list:
            return index

    raise AssertionError("never fired")


# --- 빠른 첫 발 ---


def test_defaults_are_tuned_for_fast_first_heart():
    assert DEFAULTS.aim_stable_frame_count <= 3
    assert 0.08 <= DEFAULTS.aim_center_tolerance_ratio <= 0.1
    assert DEFAULTS.approach_stop_distance_m >= 8.0


def test_target_already_near_center_fires_on_second_frame_with_defaults():
    loop = _ClosedLoop()

    assert _first_fire_step(loop, azimuth_deg=1.0, elevation_deg=-46.0) == 1


def test_stable_frame_count_one_fires_on_first_centered_frame():
    loop = _ClosedLoop(params={**DEFAULT_PARAMS, "stable_frame_count": 1})

    assert _first_fire_step(loop, azimuth_deg=1.0, elevation_deg=-46.0) == 0


def test_first_heart_comes_within_stable_count_frames_after_convergence():
    loop = _ClosedLoop()
    tolerance_x_px = DEFAULTS.aim_center_tolerance_ratio * WIDTH_PX
    tolerance_y_px = DEFAULTS.aim_center_tolerance_ratio * HEIGHT_PX

    fire_step = _first_fire_step(loop, azimuth_deg=25.0, elevation_deg=-60.0)

    first_centered_step = next(
        index
        for index, (error_x_px, error_y_px) in enumerate(loop.error_list)
        if abs(error_x_px) <= tolerance_x_px and abs(error_y_px) <= tolerance_y_px
    )
    assert fire_step - first_centered_step == DEFAULTS.aim_stable_frame_count - 1
    # 25°·15° 오프셋도 0.6 이득이면 1 s 안에 첫 발
    assert fire_step * TICK_SEC <= 1.0


def test_offset_box_converges_inside_tolerance_and_keeps_shrinking():
    loop = _ClosedLoop()

    for _ in range(12):
        loop.step(azimuth_deg=-30.0, elevation_deg=-30.0)

    error_x_px, error_y_px = loop.error_list[-1]
    assert abs(error_x_px) < 2.0
    assert abs(error_y_px) < 2.0
    magnitude_list = [math.hypot(x, y) for x, y in loop.error_list]
    assert all(later <= earlier + 1e-9 for earlier, later in pairwise(magnitude_list))


# --- 640x360 부호·크기 ---


@pytest.mark.parametrize(("offset_u", "offset_v"), [(100.0, 0.0), (-100.0, 0.0), (0.0, 60.0), (0.0, -60.0)])
def test_centering_sign_and_magnitude_on_browser_fpv_frame(offset_u, offset_v):
    aim_u, aim_v = WIDTH_PX / 2 + offset_u, HEIGHT_PX / 2 + offset_v
    detection = Detection(0, "person", 0.9, _make_box_at_aim_point(aim_u, aim_v), 3)
    frame = _make_frame(0, {})

    command = compute_centering(
        detection, frame, _make_telemetry(0.0, 10.0, -45.0), RuleModeSettings().camera, 0.08, AIM_POINT_RATIO
    )

    assert command.error_x_px == pytest.approx(offset_u)
    assert command.error_y_px == pytest.approx(offset_v)
    assert command.yaw_deg == pytest.approx(10.0 + YAW_GAIN * math.degrees(math.atan(offset_u / FOCAL_PX)))
    assert command.gimbal_pitch_deg == pytest.approx(-45.0 - PITCH_GAIN * math.degrees(math.atan(offset_v / FOCAL_PX)))


def test_aim_point_is_upper_body_not_box_center():
    # 박스 중심이 화면 중앙이면 가슴(위에서 40%)은 중앙보다 위 → 짐벌을 조금 올린다
    box = (310.0, 180.0 - BOX_HEIGHT_PX / 2, 330.0, 180.0 + BOX_HEIGHT_PX / 2)
    detection = Detection(0, "person", 0.9, box, 3)

    command = compute_centering(
        detection, _make_frame(0, {}), _make_telemetry(0.0), RuleModeSettings().camera, 0.08, AIM_POINT_RATIO
    )

    assert command.error_y_px == pytest.approx(-0.1 * BOX_HEIGHT_PX)
    assert command.gimbal_pitch_deg > -45.0


# --- 발사 중 센터링 유지 ---


def test_moving_target_stays_centered_through_the_burst():
    loop = _ClosedLoop()
    azimuth_deg = 5.0
    tolerance_x_px = DEFAULTS.aim_center_tolerance_ratio * WIDTH_PX

    result = None
    for _ in range(60):
        result = loop.step(azimuth_deg, -45.0)
        azimuth_deg += 0.5  # 5°/s — 보행자 ~1 m/s 를 수평 10 m 에서 본 각속도
        if result is not None:
            break

    assert result == ModeResult("aim_and_fire", "success", "T3")
    assert len(loop.control.fire_list) == RuleModeSettings().heart_burst_count
    first_fire_index = next(
        index
        for index, command in enumerate(loop.control.command_list)
        if isinstance(command, FireHeart)
    )
    yaw_after_first_fire = [c for c in loop.control.command_list[first_fire_index:] if isinstance(c, SetYaw)]
    assert len(yaw_after_first_fire) >= 9
    # 이득 0.6 의 정상상태 지연은 각속도×(1-g)/g ≈ 0.33° ≈ 2 px — 발사 내내 허용 오차 안
    burst_errors = loop.error_list[-len(yaw_after_first_fire):]
    assert all(abs(error_x_px) <= tolerance_x_px for error_x_px, _ in burst_errors)


# --- 재획득 (트래커 id 교체) ---


def test_track_id_churn_hands_off_to_new_id_and_keeps_aiming_and_firing():
    loop = _ClosedLoop()
    azimuth_deg = 3.0

    result = None
    step_count = 0
    for index in range(40):
        # 실측처럼 같은 사람의 id 가 자주 바뀐다(여기선 0.4 s 마다): T3 → T4 → T5 ...
        result = loop.step(azimuth_deg, -46.0, track_id=3 + index // 4)
        azimuth_deg += 0.5
        step_count += 1
        if result is not None:
            break

    assert result is not None
    assert result.outcome == "success"
    assert len(loop.control.fire_list) == 10
    fired_id_set = {command.track_id for command in loop.control.fire_list}
    assert len(fired_id_set) > 1
    assert result.target_track == f"T{loop.control.fire_list[-1].track_id}"
    # id 가 바뀐 프레임에도 조준 명령이 끊기지 않는다
    assert sum(isinstance(command, SetYaw) for command in loop.control.command_list) == step_count


def test_far_away_person_is_not_adopted_and_loss_fails_after_grace():
    mode = AimAndFireMode(DEFAULT_PARAMS, RuleModeSettings())
    control = _RecordingControl()
    center_box = _make_box_at_aim_point(WIDTH_PX / 2, HEIGHT_PX / 2)
    far_box = _make_box_at_aim_point(WIDTH_PX / 2 + 200.0, HEIGHT_PX / 2)

    result_list = []
    for frame_id in range(15):
        boxes = {3: center_box} if frame_id == 0 else {9: far_box}
        telemetry = _make_telemetry(frame_id * TICK_SEC)
        tick = ModeTick(
            now_sec=frame_id * TICK_SEC,
            telemetry=telemetry,
            tracks=(),
            frame=_make_frame(frame_id, boxes),
            frame_telemetry=telemetry,
            hearted_track_ids=frozenset(),
        )
        result = mode.step(tick, control)
        if result is not None:
            result_list.append(result)

    assert result_list == [ModeResult("aim_and_fire", "failure", "T3")]
    assert control.fire_list == []
