"""search 모드 — 수색 구역 위를 지그재그(boustrophedon)로 순찰하며 타깃을 찾는다 (RM-01, D-05, D-06, D-07, D-04).

레인은 북쪽 방향이고, 드론에서 가장 가까운 레인 끝에서 시작한다. 웨이포인트마다 GoTo 를 한 번씩 보내고
텔레메트리가 arrival_tolerance_m 이내가 되면 다음으로 넘어간다 (Phase 2 D-09).

- success: target_class 의 활성·미하트·색 어긋나지 않은 track 이 FOUND_STREAK_TICKS tick 연속 관측되면 그 track 으로 끝난다 (D-06)
- failure: 타깃 없이 모든 웨이포인트에 도달 (D-07), 또는 GoTo 거부 (D-04)
- timeout: 경과 시간 >= timeout_sec (D-07)
끝난 뒤 step 을 다시 부르면 RuntimeError ("정확히 한 번" 계약).
"""

import math

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import FlightControl, GoTo
from modules.rule_mode.interface import ModeParams, ModeTick, RuleModeSettings, SearchArea, track_label

RULE_MODE = "search"
FOUND_STREAK_TICKS = 5  # D-06 의 N — 10 Hz 에서 0.5 s


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
        self._waypoints: tuple[GoTo, ...] = ()
        self._waypoint_index = 0
        self._streak_by_track: dict[int, int] = {}
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            raise RuntimeError("search mode already finished")

        if self._start_sec is None:
            self._start_sec = tick.now_sec
            self._waypoints = build_zigzag_waypoints(
                self._settings.search_area,
                self._settings.search_altitude_m,
                self._settings.search_lane_spacing_m,
                tick.telemetry.north_m,
                tick.telemetry.east_m,
            )
            if not self._send_goto(control, self._waypoints[0]):
                return self._finish("failure")

        # 마감 tick 에 발견한 것도 인정되도록 발견 확인이 timeout 보다 먼저다
        found_track_id = self._update_streaks(tick)
        if found_track_id is not None:
            return self._finish("success", track_label(found_track_id))

        if tick.now_sec - self._start_sec >= self._timeout_sec:
            return self._finish("timeout")

        if self._has_arrived(tick):
            self._waypoint_index += 1
            if self._waypoint_index == len(self._waypoints):
                return self._finish("failure")
            if not self._send_goto(control, self._waypoints[self._waypoint_index]):
                return self._finish("failure")

        return None

    def _update_streaks(self, tick: ModeTick) -> int | None:
        candidate_id_list = [
            track.track_id
            for track in tick.tracks
            if track.class_name == self._settings.target_class
            and track.is_active
            and track.track_id not in tick.hearted_track_ids
            and not self._is_color_mismatch(track.upper_color)
        ]
        # 후보가 아닌 id 는 버린다 — 공백이 한 번이라도 있으면 streak 이 리셋된다
        self._streak_by_track = {
            track_id: self._streak_by_track.get(track_id, 0) + 1 for track_id in candidate_id_list
        }

        found_id_list = [track_id for track_id, streak in self._streak_by_track.items() if streak >= FOUND_STREAK_TICKS]
        if not found_id_list:
            return None

        # 가장 긴 streak, 동률은 가장 작은 track_id
        return min(found_id_list, key=lambda track_id: (-self._streak_by_track[track_id], track_id))

    # options.py 후보 정의와 같다 — 색을 모르는 track 은 후보로 남긴다
    def _is_color_mismatch(self, upper_color: str | None) -> bool:
        target_upper_color = self._settings.target_upper_color
        if target_upper_color is None or upper_color is None:
            return False

        return upper_color != target_upper_color

    def _has_arrived(self, tick: ModeTick) -> bool:
        waypoint = self._waypoints[self._waypoint_index]
        telemetry = tick.telemetry
        distance_m = math.sqrt(
            (waypoint.north_m - telemetry.north_m) ** 2
            + (waypoint.east_m - telemetry.east_m) ** 2
            + (waypoint.altitude_m - telemetry.altitude_m) ** 2
        )

        return distance_m <= self._settings.arrival_tolerance_m

    def _send_goto(self, control: FlightControl, goto: GoTo) -> bool:
        return control.send_command(goto).accepted

    def _finish(self, outcome: str, target_track: str | None = None) -> ModeResult:
        self._is_finished = True

        return ModeResult(RULE_MODE, outcome, target_track)


def _require_positive_finite(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"argument '{name}' must be a finite number > 0, got {value!r}")
