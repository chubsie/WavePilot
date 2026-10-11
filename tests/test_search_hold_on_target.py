"""후보가 시야에 들어오면 search 는 멈춰 확인하고, approach 는 표적에서 멀어지는 목표를 보내지 않는다.

트래커가 같은 사람에게 0.4~1 s 마다 새 track id 를 주는 실측 상황(id 교체)에서도 '같은 표적'으로 이어 붙이는지 본다.
"""

import dataclasses
import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import GoTo, SetGimbal, SetYaw, TakeOff
from modules.perception.interface import DEFAULT_CAMERA, Track
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.approach import ApproachMode
from modules.rule_mode.interface import ModeTick, RuleModeSettings
from modules.rule_mode.search import CANDIDATE_LOST_SEC, FOUND_STREAK_TICKS, HOLD_MAX_SEC, SCAN_STEP_COUNT, SearchMode
from modules.simulation.kinematic_flight import KinematicFlightControl
from modules.simulation.scripted_targets import ScriptedTarget, ScriptedTargetSource
from modules.yolo_detection.interface import Frame

TICK_SEC = 0.1
CRUISE_ALTITUDE_M = 15.0
SEARCH_PARAMS = {"timeout_sec": 160.0}
TARGET_NORTH_M = 60.0


# --- search 도우미 ---


def _make_track(track_id: int, north_m: float = TARGET_NORTH_M, east_m: float = 0.0) -> Track:
    return Track(
        track_id=track_id,
        class_name="person",
        north_m=north_m,
        east_m=east_m,
        first_seen_sec=0.0,
        last_seen_sec=1.0,
        observation_count=3,
        mean_confidence=0.8,
        is_active=True,
    )


def _make_flight() -> KinematicFlightControl:
    flight = KinematicFlightControl()
    flight.send_command(TakeOff(CRUISE_ALTITUDE_M))
    for _ in range(300):
        if flight.get_telemetry().flight_mode == "hover":
            return flight
        flight.advance(TICK_SEC)

    pytest.fail("take off did not finish")


def _make_tick(flight: KinematicFlightControl, tracks: tuple[Track, ...] = ()) -> ModeTick:
    return ModeTick(
        now_sec=flight.now_sec,
        telemetry=flight.get_telemetry(),
        tracks=tracks,
        frame=None,
        frame_telemetry=None,
        hearted_track_ids=frozenset(),
    )


def _step(mode: SearchMode, flight: KinematicFlightControl, tracks: tuple[Track, ...] = ()) -> ModeResult | None:
    result = mode.step(_make_tick(flight, tracks), flight)
    flight.advance(TICK_SEC)

    return result


def _step_until(mode: SearchMode, flight: KinematicFlightControl, is_done, max_ticks: int = 300) -> None:
    for _ in range(max_ticks):
        assert _step(mode, flight) is None
        if is_done():
            return

    pytest.fail("condition not reached")


def _get_commands_after(flight: KinematicFlightControl, index: int) -> list:
    return [command for _, command in flight.commands_received[index:]]


def _count_yaw(flight: KinematicFlightControl) -> int:
    return sum(isinstance(command, SetYaw) for _, command in flight.commands_received)


def _count_goto(flight: KinematicFlightControl) -> int:
    return sum(isinstance(command, GoTo) for _, command in flight.commands_received)


def _flicker(track_id: int):
    """4 tick 보이고 1 tick 빠진다 — FOUND_STREAK_TICKS 연속에 닿지 않는 후보."""
    return lambda i: (_make_track(track_id),) if i % 5 != 4 else ()


# --- search: 후보가 보이면 멈춘다 ---


def test_candidate_in_view_stops_scan_rotation_and_holds():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())
    _step_until(mode, flight, lambda: _count_yaw(flight) == 1)
    _step(mode, flight)
    command_count = len(flight.commands_received)
    yaw_at_sight_deg = flight.get_telemetry().yaw_deg

    tracks_at = _flicker(7)
    for i in range(int((HOLD_MAX_SEC - 1.0) / TICK_SEC)):
        assert _step(mode, flight, tracks_at(i)) is None

    # 처음 본 tick 에 지금 yaw 로 SetYaw 하나만 — 스캔 다음 단계·다음 지점 GoTo 는 없다
    assert _get_commands_after(flight, command_count) == [SetYaw(yaw_at_sight_deg)]
    assert flight.get_telemetry().yaw_deg == pytest.approx(yaw_at_sight_deg, abs=1e-6)


def test_candidate_seen_while_moving_stops_at_current_position():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())
    _step_until(mode, flight, lambda: _count_goto(flight) == 2)
    for _ in range(10):
        _step(mode, flight)
    command_count = len(flight.commands_received)
    telemetry = flight.get_telemetry()

    tracks_at = _flicker(7)
    for i in range(20):
        assert _step(mode, flight, tracks_at(i)) is None

    stop_goto = GoTo(telemetry.north_m, telemetry.east_m, CRUISE_ALTITUDE_M)
    assert _get_commands_after(flight, command_count) == [stop_goto]
    telemetry_now = flight.get_telemetry()
    assert math.hypot(telemetry_now.north_m - stop_goto.north_m, telemetry_now.east_m - stop_goto.east_m) < 1.0


def test_candidate_continues_across_track_id_changes():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())

    # 매 tick 새 id, 위치는 0.5 m 씩 흔들린다 — id 별 streak 이면 영원히 1 이다
    result = None
    for i in range(FOUND_STREAK_TICKS):
        result = _step(mode, flight, (_make_track(100 + i, north_m=TARGET_NORTH_M + 0.5 * (i % 2)),))

    assert result == ModeResult("search", "success", f"T{100 + FOUND_STREAK_TICKS - 1}")


def test_far_apart_tracks_are_not_joined_as_one_target():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())

    for i in range(FOUND_STREAK_TICKS * 2):
        assert _step(mode, flight, (_make_track(100 + i, east_m=10.0 * (i % 2)),)) is None


def test_scan_resumes_when_candidate_disappears():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())
    _step_until(mode, flight, lambda: _count_yaw(flight) == 2)
    resume_yaw_deg = [command for _, command in flight.commands_received if isinstance(command, SetYaw)][-1]

    for _ in range(2):
        assert _step(mode, flight, (_make_track(7),)) is None
    hold_count = len(flight.commands_received)

    # 사라진 뒤 CANDIDATE_LOST_SEC 동안은 그대로 멈춰 있다
    for _ in range(int(CANDIDATE_LOST_SEC / TICK_SEC) - 1):
        assert _step(mode, flight) is None
    assert len(flight.commands_received) == hold_count

    # 그 뒤 멈췄던 스캔 단계를 다시 보내고 한 바퀴를 마저 돈다
    _step_until(mode, flight, lambda: len(flight.commands_received) > hold_count, max_ticks=3)
    assert _get_commands_after(flight, hold_count) == [resume_yaw_deg]
    _step_until(mode, flight, lambda: _count_goto(flight) == 2)
    # 스캔 단계 SCAN_STEP_COUNT 개 + 멈춤 SetYaw 하나 + 다시 보낸 두 번째 단계
    assert _count_yaw(flight) == SCAN_STEP_COUNT + 2


def test_unconfirmed_candidate_is_dropped_after_hold_max():
    flight = _make_flight()
    mode = SearchMode(SEARCH_PARAMS, RuleModeSettings())
    _step_until(mode, flight, lambda: _count_yaw(flight) == 1)
    start_sec = flight.now_sec

    tracks_at = _flicker(7)
    i = 0
    while _count_yaw(flight) < 3:
        assert _step(mode, flight, tracks_at(i)) is None
        i += 1
        assert i < 200

    # 멈춤 SetYaw 뒤 HOLD_MAX_SEC 가 지나서야 스캔 단계를 다시 보낸다
    yaw_time_list = [time_sec for time_sec, command in flight.commands_received if isinstance(command, SetYaw)]
    assert yaw_time_list[2] - yaw_time_list[1] == pytest.approx(HOLD_MAX_SEC, abs=TICK_SEC + 1e-6)
    assert yaw_time_list[1] == pytest.approx(start_sec, abs=1e-6)

    # 같은 위치의 후보는 이번 수색에서 다시 멈추게 하지 않는다 — 스캔이 계속 진행된다
    for _ in range(30):
        assert _step(mode, flight, tracks_at(i)) is None
        i += 1
    assert _count_yaw(flight) >= 4


# --- approach 도우미 ---


class _FrameRewriter:
    """ScriptedTargetSource 프레임의 탐지를 바꾼다 — 트래커 id 교체·튀는 박스를 흉내 낸다."""

    def __init__(self, source: ScriptedTargetSource, rewrite) -> None:
        self._source = source
        self._rewrite = rewrite
        self._frame_index = 0

    def read_frame(self) -> Frame:
        frame = self._source.read_frame()
        detections = tuple(self._rewrite(self._frame_index, detection) for detection in frame.detections)
        self._frame_index += 1

        return dataclasses.replace(frame, detections=detections)

    def telemetry_for(self, timestamp_sec: float):
        return self._source.telemetry_for(timestamp_sec)


def _run_approach(target: ScriptedTarget, rewrite, params: dict, max_sec: float = 60.0):
    """(결과, GoTo 를 보낸 순간의 (드론 위치, 표적 실제 위치, GoTo) 목록) 을 돌려준다."""
    control = KinematicFlightControl()
    control.send_command(TakeOff(CRUISE_ALTITUDE_M))
    while control.get_telemetry().flight_mode != "hover":
        control.advance(TICK_SEC)
    control.send_command(SetGimbal(-30.0, 0.0))
    control.advance(1.0)

    source = _FrameRewriter(ScriptedTargetSource(control, [target]), rewrite)
    aggregator = TrackAggregator(DEFAULT_CAMERA)
    mode = ApproachMode(params, RuleModeSettings())
    goto_record_list = []
    start_sec = control.now_sec

    while control.now_sec - start_sec < max_sec:
        frame = source.read_frame()
        frame_telemetry = source.telemetry_for(frame.timestamp_sec)
        aggregator.update(frame, frame_telemetry)
        telemetry = control.get_telemetry()
        tick = ModeTick(
            now_sec=control.now_sec,
            telemetry=telemetry,
            tracks=tuple(aggregator.snapshot(control.now_sec)),
            frame=frame,
            frame_telemetry=frame_telemetry,
            hearted_track_ids=frozenset(),
        )

        command_count = len(control.commands_received)
        result = mode.step(tick, control)
        target_north_m, target_east_m, _ = target.get_position_ned(telemetry.time_sec)
        for _, command in control.commands_received[command_count:]:
            if isinstance(command, GoTo):
                drone_point = (telemetry.north_m, telemetry.east_m)
                goto_record_list.append((drone_point, (target_north_m, target_east_m), command))
        if result is not None:
            return result, goto_record_list

        control.advance(TICK_SEC)

    pytest.fail(f"approach did not finish within {max_sec} s")


def _churn_track_id(frame_index: int, detection):
    # 0.5 s 마다 새 id — 실측(0.4~1 s)과 비슷하게
    return dataclasses.replace(detection, track_id=detection.track_id + 100 * (frame_index // 5))


def _distance_m(point_a, point_b) -> float:
    return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])


# --- approach: id 교체에도 이어 접근하고 멀어지지 않는다 ---


def test_approach_continues_when_track_id_changes():
    target = ScriptedTarget(7, "person", TARGET_NORTH_M, 0.0, north_mps=0.5)
    params = {"target_track": "T7", "stop_distance_m": 5.0, "timeout_sec": 40.0}

    result, goto_record_list = _run_approach(target, _churn_track_id, params)

    assert result.rule_mode == "approach"
    assert result.outcome == "success"
    assert result.target_track != "T7"  # 마지막으로 따른 새 id
    drone_point, target_point, _ = goto_record_list[-1]
    assert _distance_m(drone_point, target_point) <= 5.0 + 2.5


def test_approach_ignores_estimate_jump_and_keeps_goal_toward_target():
    target = ScriptedTarget(7, "person", 40.0, 5.0)
    params = {"target_track": "T7", "stop_distance_m": 5.0, "timeout_sec": 30.0}

    # 목표를 다시 잡는 1 s 경계마다 2 프레임씩 박스가 화면 왼쪽 끝으로 튄다 — 옆으로 떨어진 엉뚱한 추정
    def jump_box_sideways(frame_index: int, detection):
        if frame_index == 0 or frame_index % 10 not in (0, 1):
            return detection
        x1, y1, x2, y2 = detection.xyxy
        return dataclasses.replace(detection, xyxy=(0.0, y1, x2 - x1, y2))

    result, goto_record_list = _run_approach(target, jump_box_sideways, params)

    # 튄 추정을 따르면 목표가 표적 옆 수십 m 로 간다 — 모든 목표는 표적 쪽(stop_distance_m 언저리)이어야 한다
    assert result == ModeResult("approach", "success", "T7")
    for drone_point, target_point, goto in goto_record_list:
        goal_distance_m = _distance_m((goto.north_m, goto.east_m), target_point)
        assert goal_distance_m <= _distance_m(drone_point, target_point) + 0.5
        assert goal_distance_m <= 5.0 + 3.0


def test_approach_skips_goal_when_tracked_target_is_behind_the_goal():
    flight = _make_flight()
    mode = ApproachMode({"target_track": "T7", "stop_distance_m": 5.0, "timeout_sec": 30.0}, RuleModeSettings())

    # 앞쪽 30 m 로 출발 — 첫 목표는 그쪽 25 m
    assert mode.step(_make_tick(flight, (_make_track(7, north_m=30.0),)), flight) is None
    assert [command for _, command in flight.commands_received if isinstance(command, GoTo)][-1].north_m == 25.0
    flight.advance(1.0)
    goto_count = _count_goto(flight)

    # 트래커가 표적을 뒤쪽 -30 m 로 옮긴다 — 튄 추정은 받지 않으므로 목표는 여전히 앞쪽, 하지만 그 GoTo 는 보내지 않는다
    assert mode.step(_make_tick(flight, (_make_track(7, north_m=-30.0),)), flight) is None
    assert _count_goto(flight) == goto_count
