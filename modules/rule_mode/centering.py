"""중앙 맞춤 제어기 — observe / aim_and_fire 가 함께 쓰는 대상 박스 → SetYaw·SetGimbal 비례 명령 (D-08, D-09, D-16).

조준점(기본 박스 중심, aim_and_fire 는 몸통 높이) 픽셀 오차를 프레임 크기에 맞춘 카메라 내부 파라미터(fx/fy,
cx/cy)로 각 오차로 바꾸고, 촬영 순간 기체 yaw 와 짐벌 pitch 에 이득을 곱한 각 오차를 더해 절대 heading(SetYaw)과
짐벌 pitch(SetGimbal, yaw 0)를 만든다. 위치 명령은 보내지 않는다(제자리 호버). 중앙 판정은 가로 |du| <= ratio*폭
그리고 세로 |dv| <= ratio*높이 (경계 포함, D-09).

대상 찾기·놓침 타이머·params 파싱도 두 모드가 같은 규칙을 쓰도록 여기에 둔다.
"""

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from modules.flight_control.interface import FlightControl, SetGimbal, SetYaw, Telemetry
from modules.perception.interface import CameraIntrinsics, Track, get_frame_camera
from modules.rule_mode.interface import ModeParams
from modules.yolo_detection.interface import Detection, Frame

logger = logging.getLogger(__name__)

# 비례 이득 — 1 미만으로 두어 프레임 지연 때 넘침을 줄인다
YAW_GAIN = 0.6
PITCH_GAIN = 0.6
GIMBAL_PITCH_MIN_DEG = -90.0
GIMBAL_PITCH_MAX_DEG = 0.0
MAX_TOLERANCE_RATIO = 0.5
# 조준점 = 박스 위에서 이 비율 높이. 0.5 는 박스 중심
BOX_CENTER_AIM_RATIO = 0.5

_TRACK_LABEL_PATTERN = re.compile(r"T(\d+)")


@dataclass(frozen=True, slots=True)
class CenteringCommand:
    yaw_deg: float
    gimbal_pitch_deg: float
    error_x_px: float
    error_y_px: float
    is_centered: bool


def compute_centering(
    detection: Detection,
    frame: Frame,
    frame_telemetry: Telemetry,
    camera: CameraIntrinsics,
    tolerance_ratio: float,
    aim_ratio: float = BOX_CENTER_AIM_RATIO,
) -> CenteringCommand:
    aim_u, aim_v = get_aim_point(detection, aim_ratio)

    error_x_px = aim_u - frame.image_width / 2.0
    error_y_px = aim_v - frame.image_height / 2.0
    is_centered = (
        abs(error_x_px) <= tolerance_ratio * frame.image_width
        and abs(error_y_px) <= tolerance_ratio * frame.image_height
    )

    # 박스는 프레임 픽셀 — 브라우저 FPV(640x360)처럼 보정 해상도와 다르면 같은 화각으로 스케일한 카메라로 각을 구한다
    frame_camera = get_frame_camera(camera, frame.image_width, frame.image_height)
    angle_x_deg = math.degrees(math.atan((aim_u - frame_camera.cx_px) / frame_camera.fx_px))
    angle_y_deg = math.degrees(math.atan((aim_v - frame_camera.cy_px) / frame_camera.fy_px))

    # 박스는 촬영 순간 자세로 찍혔으므로 그때의 yaw·짐벌 pitch 를 기준으로 삼는다
    yaw_deg = _wrap_yaw_deg(frame_telemetry.yaw_deg + YAW_GAIN * angle_x_deg)
    gimbal_pitch_deg = max(
        GIMBAL_PITCH_MIN_DEG,
        min(GIMBAL_PITCH_MAX_DEG, frame_telemetry.gimbal_pitch_deg - PITCH_GAIN * angle_y_deg),
    )

    return CenteringCommand(yaw_deg, gimbal_pitch_deg, error_x_px, error_y_px, is_centered)


def get_aim_point(detection: Detection, aim_ratio: float = BOX_CENTER_AIM_RATIO) -> tuple[float, float]:
    """박스 가로 중심, 위에서 aim_ratio 높이의 픽셀 (u, v)."""
    x1, y1, x2, y2 = detection.xyxy

    return (x1 + x2) / 2.0, y1 + aim_ratio * (y2 - y1)


def send_centering(control: FlightControl, command: CenteringCommand, label: str, rule_mode: str) -> bool:
    """SetYaw + SetGimbal(pitch, 0) 을 보낸다. 둘 다 수락이면 True (D-04: 거절은 호출자가 failure 로)."""
    ack_list = [
        control.send_command(SetYaw(command.yaw_deg)),
        control.send_command(SetGimbal(command.gimbal_pitch_deg, 0.0)),
    ]

    rejected_list = [ack for ack in ack_list if not ack.accepted]
    for ack in rejected_list:
        logger.warning("%s %s command rejected: %s", rule_mode, label, ack.reason)

    return not rejected_list


def find_target_detection(frame: Frame | None, track_id: int) -> Detection | None:
    if frame is None:
        return None

    for detection in frame.detections:
        if detection.track_id == track_id:
            return detection

    return None


def find_target_track(tracks: Iterable[Track], track_id: int) -> Track | None:
    for track in tracks:
        if track.track_id == track_id:
            return track

    return None


class TargetLossTimer:
    """놓침이 grace_sec 를 넘게 이어졌는지 판정한다. 다시 보이면 리셋."""

    def __init__(self, grace_sec: float) -> None:
        self._grace_sec = grace_sec
        self._missing_since_sec: float | None = None

    def update(self, now_sec: float, is_missing: bool) -> bool:
        if not is_missing:
            self._missing_since_sec = None
            return False

        if self._missing_since_sec is None:
            self._missing_since_sec = now_sec

        return now_sec - self._missing_since_sec > self._grace_sec


# --- params 파싱 (approach 와 같은 규칙, 오류 메시지에 모드 이름) ---


def parse_target_track(value: object, rule_mode: str) -> int:
    if isinstance(value, str):
        matched = _TRACK_LABEL_PATTERN.fullmatch(value)
        if matched is not None:
            return int(matched.group(1))
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value

    raise ValueError(f"{rule_mode} param 'target_track' must be a 'T<number>' label or int >= 0, got {value!r}")


def parse_positive_param(params: ModeParams, key: str, default: float, rule_mode: str) -> float:
    value = params.get(key)
    if value is None:
        return default

    if not _is_finite_number(value) or value <= 0:  # type: ignore[operator]
        raise ValueError(f"{rule_mode} param {key!r} must be a finite number > 0, got {value!r}")

    return float(value)  # type: ignore[arg-type]


def parse_frame_count_param(params: ModeParams, key: str, default: int, rule_mode: str) -> int:
    value = params.get(key)
    if value is None:
        return default

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{rule_mode} param {key!r} must be an int >= 1, got {value!r}")

    return value


def parse_ratio_param(params: ModeParams, key: str, default: float, rule_mode: str) -> float:
    value = params.get(key)
    if value is None:
        return default

    if not _is_finite_number(value) or not 0 < value <= MAX_TOLERANCE_RATIO:  # type: ignore[operator]
        raise ValueError(f"{rule_mode} param {key!r} must be a number in (0, {MAX_TOLERANCE_RATIO}], got {value!r}")

    return float(value)  # type: ignore[arg-type]


def _is_finite_number(value: object) -> bool:
    # bool 은 int 의 하위 타입이라 따로 막는다
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _wrap_yaw_deg(yaw_deg: float) -> float:
    """(-180, 180] 로 감싼다."""
    wrapped_deg = math.fmod(yaw_deg, 360.0)
    if wrapped_deg > 180.0:
        wrapped_deg -= 360.0
    elif wrapped_deg <= -180.0:
        wrapped_deg += 360.0

    return wrapped_deg
