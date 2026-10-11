"""search 모드 — 지점마다 호버하며 360° 회전 스캔으로 타깃을 찾는다 (RM-01, D-05, D-06, D-07, D-04).

첫 지점은 시작 위치(첫 tick 텔레메트리 N/E, 고도 search_altitude_m)이고, 그다음은 build_zigzag_waypoints 의
지그재그 웨이포인트다. 지점마다 GoTo 를 보내고 arrival_tolerance_m 이내로 도착하면 스캔한다 (Phase 2 D-09).

스캔: SetGimbal(SCAN_GIMBAL_PITCH_DEG, 0) 한 번 뒤, 스캔 시작 yaw 에서 +SCAN_STEP_DEG 씩 SCAN_STEP_COUNT 번
SetYaw(절대 heading)를 보낸다. 각 단계는 yaw 가 목표 SCAN_YAW_TOLERANCE_DEG 이내가 되거나
SCAN_STEP_TIMEOUT_SEC 가 지나면 다음으로 넘어간다. 한 바퀴를 다 돌면 다음 지점으로 간다.

- success: 같은 표적 후보가 FOUND_STREAK_TICKS tick 연속 관측되면 그 표적의 최신 track 으로 끝난다 (D-06)
  — 이동·스캔 어느 단계에서든 매 tick 확인한다
  후보는 target_class 의 활성·미하트·색 어긋나지 않은 track 이다. 트래커가 같은 사람에게 새 id 를 자주 주므로
  연속 판정은 track id 가 아니라 '같은 표적'(지면 위치가 SAME_TARGET_RADIUS_M 이내)으로 이어 붙인다.

후보 확인 호버: 후보가 시야에 들어오면 즉시 회전·이동을 멈추고(스캔 중이면 현재 yaw 로 SetYaw, 이동 중이면 현재 위치로
GoTo) 제자리에서 확인한다. 후보가 CANDIDATE_LOST_SEC 동안 안 보이면 멈췄던 스캔 단계/지점 이동을 다시 보낸다.
HOLD_MAX_SEC 안에 확정되지 않으면 그 위치를 이번 수색에서 무시하고 재개한다 (깜빡이는 오탐에 묶이지 않도록).
- failure: 타깃 없이 모든 지점의 스캔을 마침 (D-07), 또는 명령 거부 (D-04)
- timeout: 경과 시간 >= timeout_sec (D-07)
끝난 뒤 step 을 다시 부르면 RuntimeError ("정확히 한 번" 계약).
"""

import logging
import math
from dataclasses import dataclass

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import Command, FlightControl, GoTo, SetGimbal, SetYaw
from modules.perception.interface import Track
from modules.rule_mode.interface import (
    SAME_TARGET_RADIUS_M,
    ModeParams,
    ModeTick,
    RuleModeSettings,
    SearchArea,
    track_label,
)

logger = logging.getLogger(__name__)

RULE_MODE = "search"
FOUND_STREAK_TICKS = 5  # D-06 의 N — 10 Hz 에서 0.5 s

# 후보 확인 호버 — 후보가 이만큼 안 보이면 스캔을 재개하고, 이만큼 머물러도 확정되지 않으면 그 위치를 버린다
CANDIDATE_LOST_SEC = 1.5
HOLD_MAX_SEC = 5.0

# 지상 표적이 FPV 아래쪽에 들어오도록 내려다본다 — 15 m 고도에서 약 26 m 앞 지면이 화면 가운데
SCAN_GIMBAL_PITCH_DEG = -30.0
# 시뮬레이터는 SetYaw 를 최단 호로 돌린다 — 180° 미만 단계여야 방향이 한쪽으로 고정된다
SCAN_STEP_DEG = 90.0
SCAN_STEP_COUNT = 4
SCAN_YAW_TOLERANCE_DEG = 5.0
# 90°/s 기준 한 단계 1 s — 실제 기체가 느리거나 멈춰도 스캔이 끝나도록 넉넉히
SCAN_STEP_TIMEOUT_SEC = 4.0


def build_zigzag_waypoints(
    area: SearchArea,
    altitude_m: float,
    lane_spacing_m: float,
    start_north_m: float,
    start_east_m: float,
) -> tuple[GoTo, ...]:
    _require_positive_finite("altitude_m", altitude_m)
    _require_positive_finite("lane_spacing_m", lane_spacing_m)
    if not math.isfinite(start_north_m) or not math.isfinite(start_east_m):
        raise ValueError(f"start must be finite, got ({start_north_m!r}, {start_east_m!r})")

    # 레인을 구역 안에 가운데 정렬한다 — 실제 간격이 요청보다 넓어지지 않는다
    width_m = area.east_max_m - area.east_min_m
    lane_count = max(1, math.ceil(width_m / lane_spacing_m))
    actual_spacing_m = width_m / lane_count
    lane_east_list = [area.east_min_m + (i + 0.5) * actual_spacing_m for i in range(lane_count)]

    # (마지막 레인에서 시작하는가, north_max 끝에서 시작하는가) — 동률은 이 순서가 이긴다
    corner_list = [(False, False), (False, True), (True, False), (True, True)]
    starts_at_last_lane, starts_at_north_max = min(
        corner_list,
        key=lambda corner: math.hypot(
            (area.north_max_m if corner[1] else area.north_min_m) - start_north_m,
            lane_east_list[-1 if corner[0] else 0] - start_east_m,
        ),
    )
    if starts_at_last_lane:
        lane_east_list.reverse()

    waypoint_list: list[GoTo] = []
    enters_at_north_max = starts_at_north_max
    for lane_east_m in lane_east_list:
        enter_north_m, exit_north_m = (
            (area.north_max_m, area.north_min_m) if enters_at_north_max else (area.north_min_m, area.north_max_m)
        )
        waypoint_list.append(GoTo(enter_north_m, lane_east_m, altitude_m))
        waypoint_list.append(GoTo(exit_north_m, lane_east_m, altitude_m))
        enters_at_north_max = not enters_at_north_max

    return tuple(waypoint_list)


def get_patrol_length_m(waypoints: tuple[GoTo, ...], start_north_m: float, start_east_m: float) -> float:
    length_m = 0.0
    north_m, east_m = start_north_m, start_east_m
    for waypoint in waypoints:
        length_m += math.hypot(waypoint.north_m - north_m, waypoint.east_m - east_m)
        north_m, east_m = waypoint.north_m, waypoint.east_m

    return length_m


@dataclass(slots=True)
class _Candidate:
    """같은 표적으로 이어 붙인 후보 — 위치와 track id 는 가장 최근 관측으로 갱신한다."""

    north_m: float
    east_m: float
    track_id: int
    track_seen_sec: float  # 따르는 track 의 last_seen_sec — 새 id 가 옛 id 보다 최신이다
    seen_tick_sec: float  # 마지막으로 후보 track 이 보인 tick 시각
    streak: int = 0


class SearchMode:
    rule_mode = RULE_MODE

    def __init__(self, params: ModeParams, settings: RuleModeSettings) -> None:
        if "timeout_sec" not in params:
            raise ValueError("missing param 'timeout_sec'")

        timeout_sec = params["timeout_sec"]
        if not isinstance(timeout_sec, (int, float)) or isinstance(timeout_sec, bool) or not math.isfinite(timeout_sec):
            raise ValueError(f"search param 'timeout_sec' must be a finite number, got {timeout_sec!r}")
        if timeout_sec <= 0:
            raise ValueError(f"search param 'timeout_sec' must be > 0, got {timeout_sec!r}")

        self._timeout_sec = float(timeout_sec)
        self._settings = settings
        self._start_sec: float | None = None
        self._is_finished = False

        # 후보 확인 — 호버 중이 아니면 _hold_start_sec 가 None
        self._candidates: list[_Candidate] = []
        self._ignored_points: list[tuple[float, float]] = []
        self._hold_start_sec: float | None = None

        # 스캔 지점: [시작 위치, 지그재그 웨이포인트...]
        self._scan_points: tuple[GoTo, ...] = ()
        self._point_index = 0

        # 스캔 진행 — 이동 중이면 _scan_step_index 가 None
        self._scan_step_index: int | None = None
        self._scan_start_yaw_deg = 0.0
        self._scan_step_start_sec = 0.0

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            raise RuntimeError("search mode already finished")

        if self._start_sec is None:
            self._start_sec = tick.now_sec
            self._scan_points = self._build_scan_points(tick)
            if not self._send(control, self._scan_points[0]):
                return self._finish("failure")

        # 마감 tick 에 발견한 것도 인정되도록 발견 확인이 timeout 보다 먼저다
        found_track_id = self._update_candidates(tick)
        if found_track_id is not None:
            return self._finish("success", track_label(found_track_id))

        if tick.now_sec - self._start_sec >= self._timeout_sec:
            return self._finish("timeout")

        if self._candidates:
            return self._hold_on_candidate(tick, control)

        if self._hold_start_sec is not None and not self._resume(tick, control):
            return self._finish("failure")

        if self._scan_step_index is None:
            if self._has_arrived(tick) and not self._start_scan(tick, control):
                return self._finish("failure")
            return None

        if self._is_scan_step_done(tick):
            return self._advance_scan(tick, control)

        return None

    def _build_scan_points(self, tick: ModeTick) -> tuple[GoTo, ...]:
        telemetry = tick.telemetry
        start_point = GoTo(telemetry.north_m, telemetry.east_m, self._settings.search_altitude_m)
        zigzag_waypoints = build_zigzag_waypoints(
            self._settings.search_area,
            self._settings.search_altitude_m,
            self._settings.search_lane_spacing_m,
            telemetry.north_m,
            telemetry.east_m,
        )

        return (start_point, *zigzag_waypoints)

    def _start_scan(self, tick: ModeTick, control: FlightControl) -> bool:
        self._scan_start_yaw_deg = tick.telemetry.yaw_deg
        if not self._send(control, SetGimbal(SCAN_GIMBAL_PITCH_DEG, 0.0)):
            return False

        return self._send_scan_step(tick, control, 0)

    def _send_scan_step(self, tick: ModeTick, control: FlightControl, step_index: int) -> bool:
        self._scan_step_index = step_index
        self._scan_step_start_sec = tick.now_sec

        return self._send(control, SetYaw(self._get_scan_target_yaw_deg(step_index)))

    def _get_scan_target_yaw_deg(self, step_index: int) -> float:
        return _wrap_yaw_deg(self._scan_start_yaw_deg + (step_index + 1) * SCAN_STEP_DEG)

    def _is_scan_step_done(self, tick: ModeTick) -> bool:
        assert self._scan_step_index is not None
        target_yaw_deg = self._get_scan_target_yaw_deg(self._scan_step_index)
        error_deg = _wrap_yaw_deg(tick.telemetry.yaw_deg - target_yaw_deg)
        if abs(error_deg) <= SCAN_YAW_TOLERANCE_DEG:
            return True

        return tick.now_sec - self._scan_step_start_sec >= SCAN_STEP_TIMEOUT_SEC

    def _advance_scan(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        assert self._scan_step_index is not None
        next_step_index = self._scan_step_index + 1
        if next_step_index < SCAN_STEP_COUNT:
            if not self._send_scan_step(tick, control, next_step_index):
                return self._finish("failure")
            return None

        # 한 바퀴 끝 — 다음 지점으로 이동, 남은 지점이 없으면 D-07 failure
        self._scan_step_index = None
        self._point_index += 1
        if self._point_index == len(self._scan_points):
            return self._finish("failure")
        if not self._send(control, self._scan_points[self._point_index]):
            return self._finish("failure")

        return None

    def _update_candidates(self, tick: ModeTick) -> int | None:
        """이번 tick 후보 track 을 같은 표적끼리 이어 붙인다. FOUND_STREAK_TICKS 에 닿은 표적의 track id 를 돌려준다."""
        seen_candidate_ids: set[int] = set()
        for track in self._get_candidate_tracks(tick):
            candidate = self._find_same_candidate(track)
            if candidate is None:
                candidate = _Candidate(track.north_m, track.east_m, track.track_id, track.last_seen_sec, tick.now_sec)
                self._candidates.append(candidate)

            # 한 표적에 track 이 여럿(옛 id 가 아직 활성)이어도 streak 은 tick 당 한 번 — 최신 관측 track 을 따른다
            if id(candidate) not in seen_candidate_ids:
                seen_candidate_ids.add(id(candidate))
                candidate.streak += 1
            if track.last_seen_sec >= candidate.track_seen_sec:
                candidate.north_m, candidate.east_m = track.north_m, track.east_m
                candidate.track_id = track.track_id
                candidate.track_seen_sec = track.last_seen_sec

        # 공백이 한 번이라도 있으면 streak 이 리셋된다 — 후보 자체는 CANDIDATE_LOST_SEC 동안 남겨 호버를 이어 간다
        for candidate in self._candidates:
            if id(candidate) in seen_candidate_ids:
                candidate.seen_tick_sec = tick.now_sec
            else:
                candidate.streak = 0
        self._candidates = [
            candidate for candidate in self._candidates if tick.now_sec - candidate.seen_tick_sec < CANDIDATE_LOST_SEC
        ]

        found_list = [candidate for candidate in self._candidates if candidate.streak >= FOUND_STREAK_TICKS]
        if not found_list:
            return None

        # 가장 긴 streak, 동률은 가장 작은 track_id
        return min(found_list, key=lambda candidate: (-candidate.streak, candidate.track_id)).track_id

    def _get_candidate_tracks(self, tick: ModeTick) -> list[Track]:
        return [
            track
            for track in tick.tracks
            if track.class_name == self._settings.target_class
            and track.is_active
            and track.track_id not in tick.hearted_track_ids
            and not self._is_color_mismatch(track.upper_color)
            and not self._is_ignored(track)
        ]

    def _find_same_candidate(self, track: Track) -> _Candidate | None:
        for candidate in self._candidates:
            if candidate.track_id == track.track_id:
                return candidate

        near_list = [
            candidate
            for candidate in self._candidates
            if _get_distance_m(candidate.north_m, candidate.east_m, track.north_m, track.east_m) <= SAME_TARGET_RADIUS_M
        ]
        if not near_list:
            return None

        return min(
            near_list,
            key=lambda candidate: _get_distance_m(candidate.north_m, candidate.east_m, track.north_m, track.east_m),
        )

    def _is_ignored(self, track: Track) -> bool:
        return any(
            _get_distance_m(north_m, east_m, track.north_m, track.east_m) <= SAME_TARGET_RADIUS_M
            for north_m, east_m in self._ignored_points
        )

    def _hold_on_candidate(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        """후보가 보이는 동안 제자리에서 확인한다. 처음 보인 tick 에 회전·이동을 멈춘다."""
        if self._hold_start_sec is None:
            self._hold_start_sec = tick.now_sec
            logger.info("search hold on candidate %s", self._describe_candidates())
            if not self._send(control, self._get_stop_command(tick)):
                return self._finish("failure")
            return None

        if tick.now_sec - self._hold_start_sec < HOLD_MAX_SEC:
            return None

        # 확정되지 않는 후보(깜빡이는 오탐)는 이번 수색에서 버리고 스캔을 재개한다
        logger.info("search gave up unconfirmed candidate %s", self._describe_candidates())
        self._ignored_points.extend((candidate.north_m, candidate.east_m) for candidate in self._candidates)
        self._candidates = []
        if not self._resume(tick, control):
            return self._finish("failure")

        return None

    def _get_stop_command(self, tick: ModeTick) -> Command:
        telemetry = tick.telemetry
        if self._scan_step_index is not None:
            return SetYaw(telemetry.yaw_deg)

        return GoTo(telemetry.north_m, telemetry.east_m, self._settings.search_altitude_m)

    def _resume(self, tick: ModeTick, control: FlightControl) -> bool:
        """멈췄던 스캔 단계 또는 지점 이동을 다시 보낸다."""
        self._hold_start_sec = None
        logger.info("search resumes scan")
        if self._scan_step_index is not None:
            return self._send_scan_step(tick, control, self._scan_step_index)

        return self._send(control, self._scan_points[self._point_index])

    def _describe_candidates(self) -> str:
        return " ".join(
            f"{track_label(candidate.track_id)}@({candidate.north_m:.1f}, {candidate.east_m:.1f})"
            for candidate in self._candidates
        )

    # options.py 후보 정의와 같다 — 색을 모르는 track 은 후보로 남긴다
    def _is_color_mismatch(self, upper_color: str | None) -> bool:
        target_upper_color = self._settings.target_upper_color
        if target_upper_color is None or upper_color is None:
            return False

        return upper_color != target_upper_color

    def _has_arrived(self, tick: ModeTick) -> bool:
        point = self._scan_points[self._point_index]
        telemetry = tick.telemetry
        distance_m = math.sqrt(
            (point.north_m - telemetry.north_m) ** 2
            + (point.east_m - telemetry.east_m) ** 2
            + (point.altitude_m - telemetry.altitude_m) ** 2
        )

        return distance_m <= self._settings.arrival_tolerance_m

    def _send(self, control: FlightControl, command: Command) -> bool:
        return control.send_command(command).accepted

    def _finish(self, outcome: str, target_track: str | None = None) -> ModeResult:
        self._is_finished = True

        return ModeResult(RULE_MODE, outcome, target_track)


def _get_distance_m(north_a_m: float, east_a_m: float, north_b_m: float, east_b_m: float) -> float:
    return math.hypot(north_a_m - north_b_m, east_a_m - east_b_m)


def _wrap_yaw_deg(yaw_deg: float) -> float:
    """(-180, 180] 로 감싼다 — 텔레메트리 yaw 와 같은 범위."""
    wrapped_deg = math.fmod(yaw_deg, 360.0)
    if wrapped_deg > 180.0:
        wrapped_deg -= 360.0
    elif wrapped_deg <= -180.0:
        wrapped_deg += 360.0

    return wrapped_deg


def _require_positive_finite(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"argument '{name}' must be a finite number > 0, got {value!r}")
