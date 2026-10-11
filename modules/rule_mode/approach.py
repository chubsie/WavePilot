"""approach 모드 — 대상 track 쪽으로 다가가 stop_distance_m 앞에서 멈춘다 (RM-05, D-08, D-10, D-11).

매 tick 대상 track 의 박스와 촬영 순간 텔레메트리로 지면 위치를 추정하고(target_estimate), 두 추정(지면 투영 /
박스 높이)을 INFO 로그에 함께 남긴다 (D-08). REESTIMATE_PERIOD_SEC 마다 드론->대상 직선 위에서 추정 위치보다
stop_distance_m 앞 지점으로 GoTo 를 갱신하고, 같은 때 SetYaw(방위)와 SetGimbal(대상 쪽 pitch)을 함께 보낸다 (D-10).
고도는 approach 시작 고도를 유지한다.

도착은 드론과 최신 추정 위치의 수평 거리 <= stop_distance_m + arrival_tolerance_m 가 ARRIVAL_TICK_COUNT tick 연속일 때
호버 GoTo 와 success 다 (D-11). 목표가 정확히 stop_distance_m 떨어져 있어 엄격한 <= 는 몇 cm 넘침에도 뒤집히므로
공용 설정 arrival_tolerance_m 만큼 여유를 둔다.

놓침 (RM-05): 이번 프레임에 대상 탐지가 없고 그 track 이 없거나 비활성인 채로 LOST_AFTER_SEC 를 넘으면 failure.
timeout_sec 경과는 호버 후 timeout, 명령 거부(CommandAck.accepted False)는 failure (D-04).
끝난 뒤 step 이 다시 불리면 None 을 돌려준다 (호출자는 다시 부르지 않는 것이 계약).

표적 이어 받기: 트래커가 같은 사람에게 새 track id 를 자주 준다. 이번 프레임에 지금 id 의 탐지가 없으면, 마지막으로 본
표적 위치에서 SAME_TARGET_RADIUS_M 안의 같은 클래스 활성 track 을 같은 표적으로 보고 그 id 로 갈아탄다.
끝날 때 ModeResult.target_track 은 마지막으로 따른 id 다.

멀어지지 않기: 추정이 ESTIMATE_JUMP_M 넘게 튀면 ESTIMATE_JUMP_CONFIRM_COUNT 번 연속 같은 곳으로 튈 때까지 무시하고,
작은 변화는 ESTIMATE_SMOOTHING 으로 평활한다. 새 목표점이 트래커가 본 표적 위치에서 드론보다
RETREAT_TOLERANCE_M 넘게 멀면 그 GoTo 는 보내지 않는다 (직전 목표로 계속 간다).
"""

import logging
import math
import re

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import CommandAck, FlightControl, GoTo, SetGimbal, SetYaw, Telemetry
from modules.perception.interface import Track, get_frame_camera
from modules.rule_mode.interface import SAME_TARGET_RADIUS_M, ModeParams, ModeTick, RuleModeSettings, track_label
from modules.rule_mode.target_estimate import estimate_target_position, get_ground_plane_down_m
from modules.yolo_detection.interface import Detection

logger = logging.getLogger(__name__)

REESTIMATE_PERIOD_SEC = 1.0
ARRIVAL_TICK_COUNT = 3  # 0.3 s 연속 — 추정이 흔들려 한 tick 넘침에 뒤집히는 것만 막는다
LOST_AFTER_SEC = 3.0
GIMBAL_PITCH_MIN_DEG = -90.0
GIMBAL_PITCH_MAX_DEG = 0.0
DEFAULT_STOP_DISTANCE_M = 8.0  # decision/params.py ParamDefaults.approach_stop_distance_m 와 같은 값
DEFAULT_TIMEOUT_SEC = 30.0

# 추정 평활 — 새 추정 쪽으로 이 비율만큼 옮긴다. 이보다 크게 튀면 같은 곳으로 연속 몇 번 튈 때만 받아들인다
ESTIMATE_SMOOTHING = 0.5
ESTIMATE_JUMP_M = 5.0
ESTIMATE_JUMP_CONFIRM_COUNT = 5
# 목표점이 표적에서 드론보다 이만큼 넘게 멀면 멀어지는 GoTo 로 보고 보내지 않는다
RETREAT_TOLERANCE_M = 0.5

_TRACK_LABEL_PATTERN = re.compile(r"T(\d+)")


class ApproachMode:
    rule_mode = "approach"

    def __init__(self, params: ModeParams, settings: RuleModeSettings) -> None:
        self._target_id = _parse_target_track(params.get("target_track"))
        self._stop_distance_m = _parse_positive_param(params, "stop_distance_m", DEFAULT_STOP_DISTANCE_M)
        self._timeout_sec = _parse_positive_param(params, "timeout_sec", DEFAULT_TIMEOUT_SEC)
        self._settings = settings
        self._target_label = track_label(self._target_id)

        # 표적 이어 받기 — 트래커가 마지막으로 본 표적 위치와 클래스
        self._target_class = settings.target_class
        self._target_track_point: tuple[float, float] | None = None

        self._start_sec: float | None = None
        self._approach_altitude_m = 0.0
        self._estimate_point: tuple[float, float] | None = None
        self._estimate_text = "none"
        self._has_detection_estimate = False
        self._jump_point: tuple[float, float] | None = None
        self._jump_count = 0
        self._lost_since_sec: float | None = None
        self._last_goal_sec: float | None = None
        self._arrival_tick_count = 0
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            return None

        if self._start_sec is None:
            self._start_sec = tick.now_sec
            self._approach_altitude_m = tick.telemetry.altitude_m

        has_detection = self._observe(tick)

        if self._is_target_lost(tick, has_detection):
            return self._finish("failure")

        if tick.now_sec - self._start_sec >= self._timeout_sec:
            self._hover(tick.telemetry, control)
            return self._finish("timeout")

        if self._is_goal_due(tick.now_sec):
            if not self._update_goal(tick, control):
                return self._finish("failure")

        if self._has_arrived(tick.telemetry):
            if not self._hover(tick.telemetry, control).accepted:
                return self._finish("failure")
            return self._finish("success")

        return None

    def _observe(self, tick: ModeTick) -> bool:
        """이번 tick 의 대상 탐지로 추정을 갱신한다. 대상 탐지가 있었는지 돌려준다."""
        self._follow_same_target(tick)

        detection = self._find_detection(tick, self._target_id)
        if detection is not None and tick.frame is not None and tick.frame_telemetry is not None:
            # xyxy 는 프레임 픽셀 — 브라우저 FPV(640x360)처럼 보정 해상도와 다르면 같은 화각으로 스케일한다
            camera = get_frame_camera(self._settings.camera, tick.frame.image_width, tick.frame.image_height)
            estimate = estimate_target_position(detection, tick.frame_telemetry, camera)
            if estimate is not None:
                self._has_detection_estimate = True
                self._record_estimate((estimate.north_m, estimate.east_m), estimate.describe())

        # 탐지 추정이 아직 없으면 트래커가 알려 주는 활성 track 위치를 따른다
        track = self._find_track(tick)
        if not self._has_detection_estimate and track is not None and track.is_active:
            self._record_estimate((track.north_m, track.east_m), "used=track")

        return detection is not None

    def _follow_same_target(self, tick: ModeTick) -> None:
        """지금 id 의 탐지가 없으면 마지막 표적 위치 근처의 같은 클래스 track 으로 갈아탄다."""
        track = self._find_track(tick)
        if track is not None and track.is_active:
            self._target_class = track.class_name
            self._target_track_point = (track.north_m, track.east_m)

        if self._find_detection(tick, self._target_id) is not None or self._target_track_point is None:
            return

        is_current_lost = track is None or not track.is_active
        near_list = [
            other
            for other in tick.tracks
            if other.track_id != self._target_id
            and other.is_active
            and other.class_name == self._target_class
            and _get_distance_m((other.north_m, other.east_m), self._target_track_point) <= SAME_TARGET_RADIUS_M
            # 지금 id 가 아직 활성이면 이번 프레임에 실제로 보인 새 id 로만 갈아탄다
            and (is_current_lost or self._find_detection(tick, other.track_id) is not None)
        ]
        if not near_list:
            return

        target_point = self._target_track_point
        successor = min(near_list, key=lambda other: _get_distance_m((other.north_m, other.east_m), target_point))
        logger.info(
            "approach %s continues as %s (same target, %.1f m)",
            self._target_label,
            track_label(successor.track_id),
            _get_distance_m((successor.north_m, successor.east_m), target_point),
        )
        self._target_id = successor.track_id
        self._target_label = track_label(successor.track_id)
        self._target_track_point = (successor.north_m, successor.east_m)

    def _find_detection(self, tick: ModeTick, track_id: int) -> Detection | None:
        if tick.frame is None:
            return None

        for detection in tick.frame.detections:
            if detection.track_id == track_id:
                return detection

        return None

    def _find_track(self, tick: ModeTick) -> Track | None:
        for track in tick.tracks:
            if track.track_id == self._target_id:
                return track

        return None

    def _record_estimate(self, point: tuple[float, float], text: str) -> None:
        """추정을 평활해 반영한다. ESTIMATE_JUMP_M 넘게 튄 추정은 같은 곳으로 연속해서 튈 때만 받아들인다."""
        if self._estimate_point is None:
            self._estimate_point = point
            self._estimate_text = text
            return

        if _get_distance_m(point, self._estimate_point) <= ESTIMATE_JUMP_M:
            self._jump_point, self._jump_count = None, 0
            self._estimate_point = (
                self._estimate_point[0] + ESTIMATE_SMOOTHING * (point[0] - self._estimate_point[0]),
                self._estimate_point[1] + ESTIMATE_SMOOTHING * (point[1] - self._estimate_point[1]),
            )
            self._estimate_text = text
            return

        is_same_jump = self._jump_point is not None and _get_distance_m(point, self._jump_point) <= ESTIMATE_JUMP_M
        self._jump_count = self._jump_count + 1 if is_same_jump else 1
        self._jump_point = point
        if self._jump_count < ESTIMATE_JUMP_CONFIRM_COUNT:
            logger.debug("approach %s ignored estimate jump to (%.1f, %.1f)", self._target_label, *point)
            return

        logger.info("approach %s estimate moved to (%.1f, %.1f)", self._target_label, *point)
        self._estimate_point = point
        self._estimate_text = text
        self._jump_point, self._jump_count = None, 0

    def _is_target_lost(self, tick: ModeTick, has_detection: bool) -> bool:
        track = self._find_track(tick)
        is_missing = not has_detection and (track is None or not track.is_active)
        if not is_missing:
            self._lost_since_sec = None
            return False

        if self._lost_since_sec is None:
            self._lost_since_sec = tick.now_sec

        return tick.now_sec - self._lost_since_sec > LOST_AFTER_SEC

    def _is_goal_due(self, now_sec: float) -> bool:
        return self._last_goal_sec is None or now_sec - self._last_goal_sec >= REESTIMATE_PERIOD_SEC

    def _update_goal(self, tick: ModeTick, control: FlightControl) -> bool:
        """D-10: 추정 위치보다 stop_distance_m 앞으로 GoTo/SetYaw/SetGimbal 을 보낸다. 거부되면 False."""
        self._last_goal_sec = tick.now_sec
        telemetry = tick.telemetry

        if self._estimate_point is None:
            # 추정이 없으면 제자리에서 기다린다
            return self._hover(telemetry, control).accepted

        delta_north_m = self._estimate_point[0] - telemetry.north_m
        delta_east_m = self._estimate_point[1] - telemetry.east_m
        distance_m = math.hypot(delta_north_m, delta_east_m)
        bearing_deg = math.degrees(math.atan2(delta_east_m, delta_north_m))

        if distance_m > self._stop_distance_m:
            ratio = (distance_m - self._stop_distance_m) / distance_m
            goal_north_m = telemetry.north_m + delta_north_m * ratio
            goal_east_m = telemetry.east_m + delta_east_m * ratio
        else:
            goal_north_m, goal_east_m = telemetry.north_m, telemetry.east_m

        plane_drop_m = get_ground_plane_down_m(telemetry) - telemetry.down_m
        gimbal_pitch_deg = max(
            GIMBAL_PITCH_MIN_DEG,
            min(GIMBAL_PITCH_MAX_DEG, -math.degrees(math.atan2(plane_drop_m, distance_m))),
        )

        ack_list: list[CommandAck] = []
        if self._is_retreating((goal_north_m, goal_east_m), telemetry):
            logger.warning(
                "approach %s skipped goal=(%.1f, %.1f): farther from target than drone",
                self._target_label,
                goal_north_m,
                goal_east_m,
            )
        else:
            ack_list.append(control.send_command(GoTo(goal_north_m, goal_east_m, self._approach_altitude_m)))
        ack_list.append(control.send_command(SetYaw(bearing_deg)))
        ack_list.append(control.send_command(SetGimbal(gimbal_pitch_deg, 0.0)))
        logger.info(
            "approach %s goal=(%.1f, %.1f) dist=%.1f %s",
            self._target_label,
            goal_north_m,
            goal_east_m,
            distance_m,
            self._estimate_text,
        )

        rejected_list = [ack for ack in ack_list if not ack.accepted]
        for ack in rejected_list:
            logger.warning("approach %s command rejected: %s", self._target_label, ack.reason)

        return not rejected_list

    def _is_retreating(self, goal: tuple[float, float], telemetry: Telemetry) -> bool:
        """목표점이 트래커가 본 표적 위치에서 지금 드론보다 RETREAT_TOLERANCE_M 넘게 먼가."""
        if self._target_track_point is None:
            return False

        drone_distance_m = _get_distance_m((telemetry.north_m, telemetry.east_m), self._target_track_point)
        goal_distance_m = _get_distance_m(goal, self._target_track_point)

        return goal_distance_m > drone_distance_m + RETREAT_TOLERANCE_M

    def _has_arrived(self, telemetry: Telemetry) -> bool:
        """D-11: 최신 추정 위치까지 수평 거리가 허용 범위 안인 tick 이 ARRIVAL_TICK_COUNT 번 연속."""
        if self._estimate_point is None:
            self._arrival_tick_count = 0
            return False

        distance_m = math.hypot(self._estimate_point[0] - telemetry.north_m, self._estimate_point[1] - telemetry.east_m)
        if distance_m <= self._stop_distance_m + self._settings.arrival_tolerance_m:
            self._arrival_tick_count += 1
        else:
            self._arrival_tick_count = 0

        return self._arrival_tick_count >= ARRIVAL_TICK_COUNT

    def _hover(self, telemetry: Telemetry, control: FlightControl) -> CommandAck:
        return control.send_command(GoTo(telemetry.north_m, telemetry.east_m, self._approach_altitude_m))

    def _finish(self, outcome: str) -> ModeResult:
        self._is_finished = True
        logger.info("approach %s finished: %s", self._target_label, outcome)

        return ModeResult(self.rule_mode, outcome, self._target_label)


def _get_distance_m(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])


def _parse_target_track(value: object) -> int:
    if isinstance(value, str):
        matched = _TRACK_LABEL_PATTERN.fullmatch(value)
        if matched is not None:
            return int(matched.group(1))
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value

    raise ValueError(f"approach param 'target_track' must be a 'T<number>' label or int >= 0, got {value!r}")


def _parse_positive_param(params: ModeParams, key: str, default: float) -> float:
    value = params.get(key)
    if value is None:
        return default

    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"approach param {key!r} must be a finite number > 0, got {value!r}")

    return float(value)
