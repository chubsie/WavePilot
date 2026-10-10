"""안전 감시 (C3) — 순수 판정 함수 경계값, SafetyConfig 검증, SafetyMonitor 이력 기반 판정."""

import math

import pytest

from modules.flight_control.interface import GpsFix, Telemetry, ned_to_gps
from modules.mission.operator import LinkStatus
from modules.mission.safety import (
    REASON_BATTERY,
    REASON_GEOFENCE,
    REASON_GPS_JUMP,
    REASON_GPS_LOST,
    REASON_LINK_LOST,
    REASON_MAX_FLIGHT_TIME,
    SAFETY_REASONS,
    SafetyConfig,
    SafetyMonitor,
    SafetyVerdict,
    gps_speed_mps,
    is_outside_geofence,
    required_battery_pct,
)
from modules.rule_mode.interface import DEFAULT_SEARCH_AREA, SearchArea

_FROM_NED = object()  # gps 기본값 sentinel — NED 위치에서 만든 fix


def _fix(north: float, east: float = 0.0) -> GpsFix:
    return ned_to_gps(north, east, -15.0)


def _telemetry(t: float, north: float = 70.0, east: float = 0.0, battery: float = 90.0, gps=_FROM_NED) -> Telemetry:
    if gps is _FROM_NED:
        gps = _fix(north, east)

    return Telemetry(
        time_sec=t,
        north_m=north,
        east_m=east,
        down_m=-15.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=0.0,
        gimbal_pitch_deg=-90.0,
        gimbal_yaw_deg=0.0,
        battery_pct=battery,
        flight_mode="hover",
        gps=gps,
    )


def _connected(t: float) -> LinkStatus:
    return LinkStatus(True, t)


def _monitor(**overrides) -> SafetyMonitor:
    return SafetyMonitor(SafetyConfig(search_area=DEFAULT_SEARCH_AREA, **overrides))


# --- 사유 상수 ---


def test_safety_reasons_order_is_priority_order():
    assert SAFETY_REASONS == (
        REASON_LINK_LOST,
        REASON_GPS_LOST,
        REASON_GPS_JUMP,
        REASON_BATTERY,
        REASON_GEOFENCE,
        REASON_MAX_FLIGHT_TIME,
    )
    assert SAFETY_REASONS == ("link_lost", "gps_lost", "gps_jump", "battery", "geofence", "max_flight_time")


def test_safety_verdict_rejects_unknown_reason():
    with pytest.raises(ValueError, match="field 'reason' must be one of"):
        SafetyVerdict("operator", "")


# --- 구역 판정 (D-15) ---


@pytest.mark.parametrize(
    ("north", "east", "expected"),
    [
        (70.0, 0.0, False),  # 구역 안
        (140.0, 0.0, False),  # 넓힌 경계 위
        (140.1, 0.0, True),
        (70.0, 70.1, True),
        (70.0, 69.9, False),
    ],
)
def test_geofence_expanded_rectangle(north, east, expected):
    assert is_outside_geofence(north, east, DEFAULT_SEARCH_AREA, 20.0) is expected


_FAR_AREA = SearchArea(50.0, 150.0, -50.0, 50.0)  # margin 10 → 넓힌 사각형 40~160 N, 홈(0,0)은 밖


@pytest.mark.parametrize(
    ("north", "east", "expected"),
    [
        (20.0, 0.0, False),  # 홈→(40,0) 통로 위
        (20.0, 9.9, False),
        (20.0, 10.1, True),
        (-10.1, 0.0, True),  # 홈 뒤쪽 margin 밖
        (-9.9, 0.0, False),
        (45.0, 0.0, False),  # 넓힌 사각형 안
    ],
)
def test_geofence_corridor_from_home_outside_area(north, east, expected):
    assert is_outside_geofence(north, east, _FAR_AREA, 10.0) is expected


def test_geofence_corridor_degenerates_to_home_circle_when_home_inside():
    # 홈(0,0)이 넓힌 사각형(0~140 N) 안 — 통로는 홈 반경 margin 원이라 사각형 판정과 같다
    assert is_outside_geofence(-19.9, 0.0, DEFAULT_SEARCH_AREA, 20.0) is False
    assert is_outside_geofence(0.0, 150.0, DEFAULT_SEARCH_AREA, 20.0) is True


def test_geofence_corridor_follows_home_position():
    assert is_outside_geofence(0.0, 150.0, DEFAULT_SEARCH_AREA, 20.0, home_north_m=0.0, home_east_m=200.0) is False
    assert is_outside_geofence(0.0, 150.0, DEFAULT_SEARCH_AREA, 20.0, home_north_m=0.0, home_east_m=0.0) is True


# --- 배터리 필요량 (D-16) ---


def test_required_battery_pct_formula():
    assert required_battery_pct(100.0, 0.25, 15.0, 5.0, 15.0) == pytest.approx(23.75)
    assert required_battery_pct(0.0, 0.25, 15.0, 5.0, 15.0) == pytest.approx(18.75)
    assert required_battery_pct(100.0, 0.0, 15.0, 5.0, 15.0) == pytest.approx(15.0)


# --- GPS 속도 ---


def test_gps_speed_same_position_is_zero():
    assert gps_speed_mps(_fix(0.0), 0.0, _fix(0.0), 1.0) == pytest.approx(0.0)


def test_gps_speed_north_and_east_match():
    assert gps_speed_mps(_fix(0.0), 0.0, _fix(50.0), 1.0) == pytest.approx(50.0, abs=0.5)
    assert gps_speed_mps(_fix(0.0), 0.0, _fix(0.0, 50.0), 1.0) == pytest.approx(50.0, abs=0.5)


def test_gps_speed_non_positive_dt_is_zero():
    assert gps_speed_mps(_fix(0.0), 1.0, _fix(50.0), 1.0) == 0.0
    assert gps_speed_mps(_fix(0.0), 2.0, _fix(50.0), 1.0) == 0.0


def test_gps_speed_ignores_altitude():
    low = GpsFix(37.5665, 126.9780, 0.0)
    high = GpsFix(37.5665, 126.9780, 100.0)

    assert gps_speed_mps(low, 0.0, high, 1.0) == pytest.approx(0.0)


# --- SafetyConfig ---


def test_safety_config_defaults_match_contract():
    config = SafetyConfig(search_area=DEFAULT_SEARCH_AREA)

    assert (
        config.geofence_margin_m,
        config.battery_reserve_pct,
        config.max_flight_sec,
        config.home_north_m,
        config.home_east_m,
        config.return_speed_mps,
        config.landing_time_sec,
        config.default_drain_pct_per_sec,
        config.drain_window_sec,
        config.drain_min_span_sec,
        config.gps_lost_sec,
        config.gps_jump_speed_mps,
    ) == (20.0, 15.0, 300.0, 0.0, 0.0, 5.0, 15.0, 0.25, 30.0, 10.0, 2.0, 30.0)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("geofence_margin_m", -1.0),
        ("battery_reserve_pct", 100.0),
        ("battery_reserve_pct", -1.0),
        ("max_flight_sec", 0.0),
        ("return_speed_mps", 0.0),
        ("landing_time_sec", -1.0),
        ("gps_lost_sec", math.nan),
        ("gps_jump_speed_mps", True),
        ("home_north_m", math.inf),
    ],
)
def test_safety_config_rejects_invalid_values(field_name, value):
    with pytest.raises(ValueError, match=f"field '{field_name}' must be"):
        SafetyConfig(search_area=DEFAULT_SEARCH_AREA, **{field_name: value})


def test_safety_config_accepts_zero_margin():
    assert SafetyConfig(search_area=DEFAULT_SEARCH_AREA, geofence_margin_m=0.0).geofence_margin_m == 0.0


def test_safety_config_rejects_non_search_area():
    with pytest.raises(ValueError, match="field 'search_area' must be"):
        SafetyConfig(search_area=(20.0, 120.0, -50.0, 50.0))


def test_safety_config_rejects_min_span_longer_than_window():
    with pytest.raises(ValueError, match="field 'drain_min_span_sec' must be"):
        SafetyConfig(search_area=DEFAULT_SEARCH_AREA, drain_window_sec=5.0, drain_min_span_sec=10.0)


# --- SafetyMonitor: 단일 사유 ---


def test_monitor_normal_state_returns_none():
    assert _monitor().check(10.0, _telemetry(10.0), 10.0, _connected(10.0)) is None


def test_monitor_link_lost_detail():
    assert _monitor().check(6.0, _telemetry(6.0), 6.0, LinkStatus(False, 5.0)) == SafetyVerdict(
        REASON_LINK_LOST, "last_heartbeat=5.0"
    )
    assert _monitor().check(6.0, _telemetry(6.0), 6.0, LinkStatus(False, None)) == SafetyVerdict(
        REASON_LINK_LOST, "last_heartbeat=never"
    )


def test_monitor_max_flight_time_boundary():
    assert _monitor().check(299.9, _telemetry(299.9), 299.9, _connected(299.9)) is None
    assert _monitor().check(300.0, _telemetry(300.0), 300.0, _connected(300.0)) == SafetyVerdict(
        REASON_MAX_FLIGHT_TIME, "elapsed=300.0 max=300.0"
    )


def test_monitor_geofence_boundary():
    assert _monitor().check(1.0, _telemetry(1.0, north=140.1), 1.0, _connected(1.0)) == SafetyVerdict(
        REASON_GEOFENCE, "north=140.1 east=0.0"
    )
    assert _monitor().check(1.0, _telemetry(1.0, north=140.0), 1.0, _connected(1.0)) is None


def test_monitor_battery_with_default_drain_boundary():
    monitor = _monitor()

    verdict = monitor.check(1.0, _telemetry(1.0, north=100.0, battery=23.75), 1.0, _connected(1.0))

    assert monitor.drain_pct_per_sec == 0.25
    assert verdict == SafetyVerdict(REASON_BATTERY, "battery=23.8 required=23.8 drain=0.250")
    assert _monitor().check(1.0, _telemetry(1.0, north=100.0, battery=23.76), 1.0, _connected(1.0)) is None


def test_monitor_battery_uses_ned_distance_when_gps_missing():
    verdict = _monitor().check(1.0, _telemetry(1.0, north=100.0, battery=23.75, gps=None), 1.0, _connected(1.0))

    assert verdict is not None and verdict.reason == REASON_BATTERY


def test_monitor_battery_distance_uses_configured_home():
    telemetry = _telemetry(1.0, north=100.0, battery=20.0)

    # 홈 (100, 0) 이면 거리 0 → 필요량 18.75
    assert _monitor(home_north_m=100.0).check(1.0, telemetry, 1.0, _connected(1.0)) is None
    assert _monitor().check(1.0, telemetry, 1.0, _connected(1.0)).reason == REASON_BATTERY


# --- SafetyMonitor: 소모율 창 ---


def test_drain_uses_default_until_min_span_then_measured_slope():
    monitor = _monitor()

    for t in range(10):
        monitor.check(float(t), _telemetry(float(t), battery=90.0 - 0.5 * t), float(t), _connected(float(t)))
        assert monitor.drain_pct_per_sec == 0.25

    monitor.check(10.0, _telemetry(10.0, battery=85.0), 10.0, _connected(10.0))

    assert monitor.drain_pct_per_sec == pytest.approx(0.5, abs=1e-9)


def test_drain_only_reflects_window():
    monitor = _monitor()
    battery_by_time = {}
    for t in range(41):
        battery_by_time[t] = 100.0 - t if t <= 20 else 80.0 - 0.2 * (t - 20)

    for t, battery in battery_by_time.items():
        monitor.check(float(t), _telemetry(float(t), battery=battery), float(t), _connected(float(t)))

    # 창 30 s → 첫 샘플 t=10
    expected = (battery_by_time[10] - battery_by_time[40]) / 30.0
    assert monitor.drain_pct_per_sec == pytest.approx(expected)


@pytest.mark.parametrize("step", [0.5, 0.0])
def test_drain_falls_back_to_default_when_battery_rises_or_flat(step):
    monitor = _monitor()

    for t in range(16):
        monitor.check(float(t), _telemetry(float(t), battery=50.0 + step * t), float(t), _connected(float(t)))

    assert monitor.drain_pct_per_sec == 0.25


# --- SafetyMonitor: GPS 끊김·점프 (D-18) ---


def test_gps_lost_after_duration():
    monitor = _monitor()
    verdicts = {}

    for t in (10.0, 11.0, 11.9, 12.0):
        verdicts[t] = monitor.check(t, _telemetry(t, gps=None), t, _connected(t))

    assert verdicts[11.9] is None
    assert verdicts[12.0] == SafetyVerdict(REASON_GPS_LOST, "gps_lost_for=2.0")


def test_gps_lost_timer_resets_on_fix():
    monitor = _monitor()
    verdicts = {}

    track = ((10.0, None), (10.5, None), (11.0, _fix(70.0)), (11.5, None), (12.5, None), (13.0, None), (13.5, None))

    for t, gps in track:
        verdicts[t] = monitor.check(t, _telemetry(t, gps=gps), t, _connected(t))

    assert verdicts[13.0] is None
    assert verdicts[13.5].reason == REASON_GPS_LOST


def _run_gps_track(monitor: SafetyMonitor, track: list[tuple[float, GpsFix | None]]) -> SafetyVerdict | None:
    verdict = None
    for t, gps in track:
        verdict = monitor.check(t, _telemetry(t, gps=gps), t, _connected(t))
    return verdict


def test_first_fix_only_sets_reference():
    assert _run_gps_track(_monitor(), [(0.0, _fix(500.0))]) is None


def test_gps_steady_motion_is_not_jump():
    assert _run_gps_track(_monitor(), [(0.0, _fix(0.0)), (1.0, _fix(5.0)), (2.0, _fix(10.0))]) is None


def test_gps_jump_boundary():
    base = [(0.0, _fix(0.0)), (1.0, _fix(5.0)), (2.0, _fix(10.0))]

    assert _run_gps_track(_monitor(), [*base, (3.0, _fix(41.0))]) == SafetyVerdict(REASON_GPS_JUMP, "speed=31.0")
    assert _run_gps_track(_monitor(), [*base, (3.0, _fix(39.9))]) is None


def test_gps_jump_resets_reference_after_detection():
    monitor = _monitor()

    assert _run_gps_track(monitor, [(0.0, _fix(0.0)), (1.0, _fix(500.0))]).reason == REASON_GPS_JUMP
    assert _run_gps_track(monitor, [(2.0, _fix(505.0))]) is None


def test_first_fix_after_dropout_is_rebased_not_jump():
    track = [(0.0, _fix(0.0)), (0.5, None), (1.0, None), (1.5, _fix(500.0))]

    monitor = _monitor()
    assert _run_gps_track(monitor, track) is None
    assert _run_gps_track(monitor, [(2.5, _fix(505.0))]) is None

    no_dropout = [(0.0, _fix(0.0)), (1.5, _fix(500.0))]
    assert _run_gps_track(_monitor(), no_dropout).reason == REASON_GPS_JUMP


# --- SafetyMonitor: 우선순위 ---


def test_priority_link_lost_over_geofence_and_battery():
    verdict = _monitor().check(1.0, _telemetry(1.0, north=200.0, battery=5.0), 1.0, LinkStatus(False, 0.5))

    assert verdict.reason == REASON_LINK_LOST


def test_priority_gps_lost_over_battery():
    monitor = _monitor()
    monitor.check(0.0, _telemetry(0.0, battery=5.0, gps=None), 0.0, _connected(0.0))

    verdict = monitor.check(2.0, _telemetry(2.0, battery=5.0, gps=None), 2.0, _connected(2.0))

    assert verdict.reason == REASON_GPS_LOST


def test_priority_battery_over_geofence():
    verdict = _monitor().check(1.0, _telemetry(1.0, north=140.1, battery=20.0), 1.0, _connected(1.0))

    assert verdict.reason == REASON_BATTERY


def test_priority_geofence_over_max_flight_time():
    verdict = _monitor().check(400.0, _telemetry(400.0, north=140.1), 400.0, _connected(400.0))

    assert verdict.reason == REASON_GEOFENCE


# --- SafetyMonitor: 사유 틱에도 이력 갱신 ---


def test_history_updates_on_link_lost_ticks():
    monitor = _monitor()

    for t in range(10):
        telemetry = _telemetry(float(t), battery=90.0 - 0.5 * t, gps=None)
        verdict = monitor.check(float(t), telemetry, float(t), LinkStatus(False, None))
        assert verdict.reason == REASON_LINK_LOST

    verdict = monitor.check(10.0, _telemetry(10.0, battery=85.0, gps=None), 10.0, _connected(10.0))

    # 링크 끊김 틱에도 배터리 창과 gps None 시작 시각이 쌓였다
    assert monitor.drain_pct_per_sec == pytest.approx(0.5, abs=1e-9)
    assert verdict == SafetyVerdict(REASON_GPS_LOST, "gps_lost_for=10.0")


def test_gps_motion_during_link_loss_is_not_jump_after_reconnect():
    monitor = _monitor()
    monitor.check(0.0, _telemetry(0.0, gps=_fix(0.0)), 0.0, _connected(0.0))

    for t in range(1, 11):
        monitor.check(float(t), _telemetry(float(t), gps=_fix(5.0 * t)), float(t), LinkStatus(False, 0.0))

    assert monitor.check(11.0, _telemetry(11.0, gps=_fix(55.0)), 11.0, _connected(11.0)) is None
