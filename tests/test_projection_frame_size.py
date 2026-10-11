"""프레임 실제 크기 카메라 — 브라우저 FPV(640x360) 박스도 1280x720 과 같은 지면 좌표로 투영돼야 한다.

또 늦게 도착한(더 이른 timestamp) 관측이 와도 track 의 first_seen <= last_seen 이 유지돼야 한다.
"""

import math

import pytest

from modules.decision.interface.state import TrackState
from modules.flight_control.interface import CommandAck, GoTo, Telemetry
from modules.perception.interface import DEFAULT_CAMERA, get_frame_camera, project_ned_to_pixel
from modules.perception.projection import pixel_to_ground
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.approach import ApproachMode
from modules.rule_mode.interface import ModeTick, RuleModeSettings
from modules.yolo_detection.interface import Detection, Frame

SMALL_WIDTH_PX, SMALL_HEIGHT_PX = 640, 360
LARGE_WIDTH_PX, LARGE_HEIGHT_PX = 1280, 720
TRACK_ID = 2

# 실측 시나리오: 드론 north -54 east 0 고도 8 m, yaw 0, 짐벌 -30°, 빨간 옷 보행자 north≈-30 east≈-7
FIELD_TELEMETRY = Telemetry(
    time_sec=10.0,
    north_m=-54.0,
    east_m=0.0,
    down_m=-8.0,
    roll_deg=0.0,
    pitch_deg=0.0,
    yaw_deg=0.0,
    gimbal_pitch_deg=-30.0,
    gimbal_yaw_deg=0.0,
    battery_pct=90.0,
    flight_mode="hover",
)
FIELD_TARGET_NE = (-30.0, -7.0)


class _RecordingControl:
    """ApproachMode 가 보낸 명령을 모은다. 전부 수락."""

    def __init__(self) -> None:
        self.command_list: list[object] = []

    def send_command(self, command: object) -> CommandAck:
        self.command_list.append(command)
        return CommandAck(accepted=True, reason="")


def _make_frame(xyxy: tuple[float, float, float, float], width_px: int, height_px: int, timestamp_sec: float = 10.0):
    detection = Detection(class_id=0, class_name="person", confidence=0.9, xyxy=xyxy, track_id=TRACK_ID)
    return Frame(
        frame_id=1,
        timestamp_sec=timestamp_sec,
        image_width=width_px,
        image_height=height_px,
        detections=(detection,),
    )


def _scale_box(xyxy: tuple[float, float, float, float], factor: float) -> tuple[float, float, float, float]:
    return tuple(value * factor for value in xyxy)  # type: ignore[return-value]


def _get_projected_track(frame: Frame, telemetry: Telemetry):
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    aggregator.update(frame, telemetry)
    (track,) = aggregator.snapshot(telemetry.time_sec)
    return track


def _get_approach_goal(frame: Frame, telemetry: Telemetry) -> GoTo:
    mode = ApproachMode({"target_track": f"T{TRACK_ID}", "stop_distance_m": 5.0}, RuleModeSettings())
    tick = ModeTick(
        now_sec=telemetry.time_sec,
        telemetry=telemetry,
        tracks=(),
        frame=frame,
        frame_telemetry=telemetry,
        hearted_track_ids=frozenset(),
    )
    control = _RecordingControl()

    mode.step(tick, control)

    (goal,) = [command for command in control.command_list if isinstance(command, GoTo)]
    return goal


# --- CameraIntrinsics.scaled_to / get_frame_camera ---


def test_scaled_camera_keeps_field_of_view():
    camera = DEFAULT_CAMERA.scaled_to(SMALL_WIDTH_PX, SMALL_HEIGHT_PX)

    hfov_deg = math.degrees(2.0 * math.atan(camera.cx_px / camera.fx_px))
    assert (camera.width_px, camera.height_px) == (SMALL_WIDTH_PX, SMALL_HEIGHT_PX)
    assert hfov_deg == pytest.approx(90.0)
    assert camera.cy_px / camera.fy_px == pytest.approx(DEFAULT_CAMERA.cy_px / DEFAULT_CAMERA.fy_px)


def test_same_size_frame_keeps_calibrated_camera_object():
    # 1280x720(drone_camera.html·mock) 경로는 수치가 한 비트도 바뀌지 않아야 한다
    assert DEFAULT_CAMERA.scaled_to(LARGE_WIDTH_PX, LARGE_HEIGHT_PX) is DEFAULT_CAMERA
    assert get_frame_camera(DEFAULT_CAMERA, LARGE_WIDTH_PX, LARGE_HEIGHT_PX) is DEFAULT_CAMERA


@pytest.mark.parametrize("size", [(0, 360), (640, 0), (-1, -1)])
def test_scaled_to_rejects_non_positive_size(size):
    with pytest.raises(ValueError, match="frame size must be > 0"):
        DEFAULT_CAMERA.scaled_to(*size)


def test_frame_camera_falls_back_on_broken_size_meta():
    assert get_frame_camera(DEFAULT_CAMERA, 0, 0) is DEFAULT_CAMERA


# --- 투영이 프레임 크기와 무관 ---


def test_small_frame_box_projects_like_same_relative_large_box(hover_telemetry):
    small_box = (300.0, 200.0, 340.0, 300.0)  # 화면 하단 쪽
    small_track = _get_projected_track(_make_frame(small_box, SMALL_WIDTH_PX, SMALL_HEIGHT_PX), hover_telemetry)
    large_track = _get_projected_track(
        _make_frame(_scale_box(small_box, 2.0), LARGE_WIDTH_PX, LARGE_HEIGHT_PX), hover_telemetry
    )

    assert small_track.north_m == pytest.approx(large_track.north_m)
    assert small_track.east_m == pytest.approx(large_track.east_m)


def test_approach_goal_independent_of_frame_size():
    small_box = (180.0, 90.0, 200.0, 140.0)
    small_goal = _get_approach_goal(_make_frame(small_box, SMALL_WIDTH_PX, SMALL_HEIGHT_PX), FIELD_TELEMETRY)
    large_goal = _get_approach_goal(
        _make_frame(_scale_box(small_box, 2.0), LARGE_WIDTH_PX, LARGE_HEIGHT_PX), FIELD_TELEMETRY
    )

    assert small_goal.north_m == pytest.approx(large_goal.north_m)
    assert small_goal.east_m == pytest.approx(large_goal.east_m)


def test_field_scenario_small_frame_projects_near_real_pedestrian():
    """실측 로그 재현: 640x360 화면의 보행자 발 픽셀이 실제 위치 근처로 돌아와야 한다(이전엔 75 m 밖)."""
    small_camera = DEFAULT_CAMERA.scaled_to(SMALL_WIDTH_PX, SMALL_HEIGHT_PX)
    foot_u, foot_v = project_ned_to_pixel((*FIELD_TARGET_NE, 0.0), small_camera, FIELD_TELEMETRY)
    assert 0.0 < foot_u < SMALL_WIDTH_PX / 2.0  # 왼쪽(east 음수)
    assert 0.0 < foot_v < SMALL_HEIGHT_PX

    box = (foot_u - 8.0, foot_v - 40.0, foot_u + 8.0, foot_v)
    frame = _make_frame(box, SMALL_WIDTH_PX, SMALL_HEIGHT_PX)

    track = _get_projected_track(frame, FIELD_TELEMETRY)
    goal = _get_approach_goal(frame, FIELD_TELEMETRY)

    assert math.hypot(track.north_m - FIELD_TARGET_NE[0], track.east_m - FIELD_TARGET_NE[1]) < 0.5
    # approach 는 대상 5 m 앞에 서야 한다 — 드론보다 남쪽(north -54 → -30 쪽)으로, 대상까지 약 5 m
    goal_to_target_m = math.hypot(goal.north_m - FIELD_TARGET_NE[0], goal.east_m - FIELD_TARGET_NE[1])
    assert goal_to_target_m == pytest.approx(5.0, abs=0.5)

    # 회귀 확인: 보정 카메라를 그대로 쓰던 이전 방식이면 수십 m 어긋난다
    old_ground = pixel_to_ground(foot_u, foot_v, DEFAULT_CAMERA, FIELD_TELEMETRY)
    assert old_ground is None or math.hypot(old_ground[0] - FIELD_TARGET_NE[0], old_ground[1] - FIELD_TARGET_NE[1]) > 10.0


# --- 늦게 도착한 관측 ---


def test_out_of_order_observation_keeps_first_seen_before_last_seen(hover_telemetry):
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    box = (600.0, 500.0, 680.0, 650.0)

    aggregator.update(_make_frame(box, LARGE_WIDTH_PX, LARGE_HEIGHT_PX, timestamp_sec=5.0), hover_telemetry)
    aggregator.update(_make_frame(box, LARGE_WIDTH_PX, LARGE_HEIGHT_PX, timestamp_sec=4.8), hover_telemetry)

    (track,) = aggregator.snapshot(5.0)
    assert track.first_seen_sec == 4.8
    assert track.last_seen_sec == 5.0
    assert track.observation_count == 2

    # 실측 크래시 지점: 결정 상태 TrackState 가 first_seen > last_seen 으로 ValueError 를 던졌다
    TrackState(
        track_id=f"T{track.track_id}",
        class_name=track.class_name,
        north_m=track.north_m,
        east_m=track.east_m,
        first_seen_sec=track.first_seen_sec,
        last_seen_sec=track.last_seen_sec,
        observation_count=track.observation_count,
        mean_confidence=track.mean_confidence,
        is_active=track.is_active,
        upper_color=track.upper_color,
    )
