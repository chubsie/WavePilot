import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.decision.params import ParamDefaults
from modules.flight_control.interface import CommandAck, GoTo, SetGimbal, SetYaw, TakeOff
from modules.perception.interface import Track
from modules.rule_mode.interface import ModeTick, RuleMode, RuleModeSettings, SearchArea
from modules.rule_mode.search import (
    FOUND_STREAK_TICKS,
    SCAN_GIMBAL_PITCH_DEG,
    SCAN_STEP_COUNT,
    SCAN_STEP_TIMEOUT_SEC,
    SearchMode,
    build_zigzag_waypoints,
    get_patrol_length_m,
)
from modules.simulation.kinematic_flight import KinematicFlightControl

TICK_SEC = 0.1
DEFAULT_AREA = SearchArea(20.0, 120.0, -50.0, 50.0)
PARAMS = {"timeout_sec": ParamDefaults().search_timeout_sec}
HOME_POINT = GoTo(0.0, 0.0, 15.0)


def _make_track(track_id: int, class_name: str = "person", is_active: bool = True) -> Track:
    return Track(
        track_id=track_id,
        class_name=class_name,
        north_m=60.0,
        east_m=0.0,
        first_seen_sec=0.0,
        last_seen_sec=1.0,
        observation_count=3,
        mean_confidence=0.8,
        is_active=is_active,
    )


def _take_off(flight: KinematicFlightControl) -> None:
    flight.send_command(TakeOff(15.0))
    for _ in range(300):
        if flight.get_telemetry().flight_mode == "hover":
            return
        flight.advance(TICK_SEC)

    pytest.fail("take off did not finish")


def _make_tick(flight: KinematicFlightControl, tracks=(), hearted=frozenset()) -> ModeTick:
    return ModeTick(
        now_sec=flight.now_sec,
        telemetry=flight.get_telemetry(),
        tracks=tracks,
        frame=None,
        frame_telemetry=None,
        hearted_track_ids=hearted,
    )


def _run(mode, flight, tracks_at=lambda i: (), hearted=frozenset(), max_ticks=1500) -> tuple[ModeResult, int, float]:
    start_sec = flight.now_sec
    for i in range(max_ticks):
        result = mode.step(_make_tick(flight, tracks_at(i), hearted), flight)
        if result is not None:
            return result, i, flight.now_sec - start_sec
        flight.advance(TICK_SEC)

    pytest.fail(f"mode did not finish within {max_ticks} ticks")


def _make_flight() -> KinematicFlightControl:
    flight = KinematicFlightControl()
    _take_off(flight)

    return flight


def _get_goto_list(flight: KinematicFlightControl) -> list[GoTo]:
    return [command for _, command in flight.commands_received if isinstance(command, GoTo)]


def _get_yaw_list(flight: KinematicFlightControl) -> list[float]:
    return [command.yaw_deg for _, command in flight.commands_received if isinstance(command, SetYaw)]


def _get_gimbal_list(flight: KinematicFlightControl) -> list[SetGimbal]:
    return [command for _, command in flight.commands_received if isinstance(command, SetGimbal)]


class _FrozenYawFlight(KinematicFlightControl):
    """SetYaw 를 수락만 하고 돌지 않는 기체 — 스캔 단계 타임아웃 경로를 본다."""

    def send_command(self, command) -> CommandAck:
        if isinstance(command, SetYaw) and not self.reject_commands:
            self._commands_received.append((self.now_sec, command))
            return CommandAck(True, "frozen yaw")

        return super().send_command(command)


# --- build_zigzag_waypoints ---


def test_default_area_zigzag_from_home():
    waypoints = build_zigzag_waypoints(DEFAULT_AREA, 15.0, 25.0, 0.0, 0.0)

    assert waypoints == (
        GoTo(20.0, -37.5, 15.0),
        GoTo(120.0, -37.5, 15.0),
        GoTo(120.0, -12.5, 15.0),
        GoTo(20.0, -12.5, 15.0),
        GoTo(20.0, 12.5, 15.0),
        GoTo(120.0, 12.5, 15.0),
        GoTo(120.0, 37.5, 15.0),
        GoTo(20.0, 37.5, 15.0),
    )


def test_starts_at_nearest_corner_and_walks_lanes_in_reverse():
    waypoints = build_zigzag_waypoints(DEFAULT_AREA, 15.0, 25.0, 130.0, 60.0)

    assert waypoints == (
        GoTo(120.0, 37.5, 15.0),
        GoTo(20.0, 37.5, 15.0),
        GoTo(20.0, 12.5, 15.0),
        GoTo(120.0, 12.5, 15.0),
        GoTo(120.0, -12.5, 15.0),
        GoTo(20.0, -12.5, 15.0),
        GoTo(20.0, -37.5, 15.0),
        GoTo(120.0, -37.5, 15.0),
    )


def test_lane_count_rounds_up_and_lanes_stay_inside_area():
    waypoints = build_zigzag_waypoints(DEFAULT_AREA, 15.0, 30.0, 0.0, 0.0)

    assert len(waypoints) == 8
    assert {waypoint.east_m for waypoint in waypoints} == {-37.5, -12.5, 12.5, 37.5}
    assert all(20.0 <= waypoint.north_m <= 120.0 and -50.0 <= waypoint.east_m <= 50.0 for waypoint in waypoints)


def test_wide_spacing_gives_single_centered_lane():
    waypoints = build_zigzag_waypoints(DEFAULT_AREA, 15.0, 200.0, 0.0, 0.0)

    assert waypoints == (GoTo(20.0, 0.0, 15.0), GoTo(120.0, 0.0, 15.0))


@pytest.mark.parametrize(
    ("altitude_m", "lane_spacing_m", "start_north_m"),
    [(0.0, 25.0, 0.0), (15.0, 0.0, 0.0), (15.0, -1.0, 0.0), (15.0, 25.0, math.nan), (math.inf, 25.0, 0.0)],
)
def test_bad_waypoint_arguments_are_rejected(altitude_m, lane_spacing_m, start_north_m):
    with pytest.raises(ValueError):
        build_zigzag_waypoints(DEFAULT_AREA, altitude_m, lane_spacing_m, start_north_m, 0.0)


def test_default_patrol_fits_in_search_timeout():
    settings = RuleModeSettings()
    waypoints = build_zigzag_waypoints(
        settings.search_area, settings.search_altitude_m, settings.search_lane_spacing_m, 0.0, 0.0
    )

    length_m = get_patrol_length_m(waypoints, 0.0, 0.0)

    assert length_m == pytest.approx(517.5)
    assert length_m / 5.0 == pytest.approx(103.5)
    assert length_m / 5.0 < ParamDefaults().search_timeout_sec


def test_settings_defaults_match_param_defaults():
    settings = RuleModeSettings()
    defaults = ParamDefaults()

    assert settings.search_area == SearchArea(
        defaults.search_area_north_min_m,
        defaults.search_area_north_max_m,
        defaults.search_area_east_min_m,
        defaults.search_area_east_max_m,
    )
    assert settings.search_altitude_m == defaults.search_altitude_m
    assert settings.search_lane_spacing_m == defaults.search_lane_spacing_m


# --- SearchMode ---


def test_satisfies_rule_mode_protocol():
    mode = SearchMode(PARAMS, RuleModeSettings())

    assert isinstance(mode, RuleMode)
    assert mode.rule_mode == "search"


@pytest.mark.parametrize("params", [{}, {"timeout_sec": 0}, {"timeout_sec": True}, {"timeout_sec": "x"}])
def test_bad_timeout_param_is_rejected(params):
    with pytest.raises(ValueError):
        SearchMode(params, RuleModeSettings())


def test_exhausting_area_without_target_fails_within_timeout():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    result, _, elapsed_sec = _run(mode, flight, max_ticks=2000)

    scan_point_list = [HOME_POINT, *build_zigzag_waypoints(DEFAULT_AREA, 15.0, 25.0, 0.0, 0.0)]
    assert result == ModeResult("search", "failure", None)
    assert _get_goto_list(flight) == scan_point_list
    assert len(_get_yaw_list(flight)) == SCAN_STEP_COUNT * len(scan_point_list)
    assert elapsed_sec < ParamDefaults().search_timeout_sec


def test_first_scan_hovers_at_current_position_and_turns_full_circle():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    # 첫 지점 스캔이 끝나 다음 지점으로 GoTo 가 나갈 때까지
    for _ in range(100):
        assert mode.step(_make_tick(flight), flight) is None
        if len(_get_goto_list(flight)) == 2:
            break
        flight.advance(TICK_SEC)

    assert _get_goto_list(flight)[0] == HOME_POINT
    assert _get_gimbal_list(flight) == [SetGimbal(SCAN_GIMBAL_PITCH_DEG, 0.0)]
    assert _get_yaw_list(flight) == [90.0, 180.0, -90.0, 0.0]
    assert len(_get_goto_list(flight)) == 2


def test_scan_starts_from_current_yaw_and_wraps():
    flight = KinematicFlightControl(start_altitude_m=15.0, start_yaw_deg=135.0)
    mode = SearchMode(PARAMS, RuleModeSettings())

    for _ in range(100):
        mode.step(_make_tick(flight), flight)
        if len(_get_goto_list(flight)) == 2:
            break
        flight.advance(TICK_SEC)

    assert _get_yaw_list(flight) == [-135.0, -45.0, 45.0, 135.0]


def test_scan_step_times_out_when_yaw_does_not_move():
    flight = _FrozenYawFlight(start_altitude_m=15.0)
    mode = SearchMode(PARAMS, RuleModeSettings())

    for _ in range(300):
        mode.step(_make_tick(flight), flight)
        if len(_get_goto_list(flight)) == 2:
            break
        flight.advance(TICK_SEC)

    yaw_time_list = [time_sec for time_sec, command in flight.commands_received if isinstance(command, SetYaw)]
    next_goto_sec = flight.commands_received[-1][0]
    assert len(yaw_time_list) == SCAN_STEP_COUNT
    step_sec_list = [later - earlier for earlier, later in zip(yaw_time_list, [*yaw_time_list[1:], next_goto_sec])]
    # 0.1 s 누적 오차로 경계 tick 이 하나 밀릴 수 있다
    assert all(
        SCAN_STEP_TIMEOUT_SEC - 1e-6 <= step_sec <= SCAN_STEP_TIMEOUT_SEC + TICK_SEC + 1e-6
        for step_sec in step_sec_list[:-1]
    )
    # 마지막 목표는 스캔 시작 heading 이라 돌지 않은 기체는 이미 도착해 있다
    assert step_sec_list[-1] == pytest.approx(TICK_SEC)


def test_target_seen_while_scanning_succeeds():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    # tick 2 는 첫 지점 스캔 한가운데 — 아직 다음 지점으로 출발하기 전
    result, tick_index, _ = _run(mode, flight, tracks_at=lambda i: (_make_track(7),) if i >= 2 else ())

    assert result == ModeResult("search", "success", "T7")
    assert tick_index == 2 + FOUND_STREAK_TICKS - 1
    assert _get_goto_list(flight) == [HOME_POINT]


def test_target_seen_for_streak_succeeds_on_fifth_observation():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    result, tick_index, _ = _run(mode, flight, tracks_at=lambda i: (_make_track(7),) if i >= 30 else ())

    assert result == ModeResult("search", "success", "T7")
    assert tick_index == 30 + FOUND_STREAK_TICKS - 1


def test_hearted_other_class_and_inactive_tracks_never_end_search():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())
    tracks = (_make_track(7), _make_track(8, class_name="car"), _make_track(9, is_active=False))

    result, _, _ = _run(mode, flight, tracks_at=lambda i: tracks, hearted=frozenset({7}))

    assert result == ModeResult("search", "failure", None)


def test_flickering_target_never_succeeds():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    result, _, _ = _run(mode, flight, tracks_at=lambda i: (_make_track(7),) if i % 5 != 4 else ())

    assert result == ModeResult("search", "failure", None)


def test_streak_restarts_after_gap():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    # tick 0..3 보임, 4 공백, 5..9 보임 -> 두 번째 구간의 5번째 tick 이 9
    result, tick_index, _ = _run(mode, flight, tracks_at=lambda i: (_make_track(7),) if i != 4 else ())

    assert result == ModeResult("search", "success", "T7")
    assert tick_index == 9


def test_timeout_ends_search_without_target():
    flight = _make_flight()
    mode = SearchMode({"timeout_sec": 10.0}, RuleModeSettings())

    result, _, elapsed_sec = _run(mode, flight)

    assert result == ModeResult("search", "timeout", None)
    assert 10.0 <= elapsed_sec <= 10.1 + 1e-6


def test_rejected_first_command_fails_on_first_tick():
    flight = _make_flight()
    flight.reject_commands = True
    mode = SearchMode(PARAMS, RuleModeSettings())

    result, tick_index, _ = _run(mode, flight)

    assert result == ModeResult("search", "failure", None)
    assert tick_index == 0


def test_rejected_scan_yaw_fails():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())
    assert mode.step(_make_tick(flight), flight) is None

    # 첫 SetYaw 이후 거부를 켜면 다음 스캔 단계 SetYaw 가 거부된다
    flight.reject_commands = True
    result = None
    for _ in range(100):
        flight.advance(TICK_SEC)
        result = mode.step(_make_tick(flight), flight)
        if result is not None:
            break

    assert result == ModeResult("search", "failure", None)
    assert len(_get_yaw_list(flight)) == 1


def test_rejected_goto_to_next_point_fails():
    flight = _make_flight()
    mode = SearchMode(PARAMS, RuleModeSettings())

    # 마지막 스캔 단계(4번째 SetYaw)가 나간 뒤 거부를 켠다 — 다음 지점 GoTo 가 거부된다
    result = None
    for _ in range(300):
        result = mode.step(_make_tick(flight), flight)
        if result is not None:
            break
        if len(_get_yaw_list(flight)) == SCAN_STEP_COUNT:
            flight.reject_commands = True
        flight.advance(TICK_SEC)

    assert result == ModeResult("search", "failure", None)
    assert _get_goto_list(flight) == [HOME_POINT]


def test_step_after_finish_raises():
    flight = _make_flight()
    flight.reject_commands = True
    mode = SearchMode(PARAMS, RuleModeSettings())
    mode.step(_make_tick(flight), flight)

    with pytest.raises(RuntimeError):
        mode.step(_make_tick(flight), flight)
