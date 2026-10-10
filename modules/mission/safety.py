"""안전 감시 (C3) — 배터리·구역·최대 비행 시간·GPS 이상·링크 두절 판정.

판정 우선순위는 SAFETY_REASONS 순서다: link_lost > gps_lost > gps_jump > battery > geofence > max_flight_time.
여러 사유가 겹치면 첫 사유 하나만 돌려준다(보고·로그도 그 사유).

홈까지 거리와 구역 판정은 telemetry NED(north_m/east_m) 기준이라 GPS 이상 중에도 같은 값을 쓴다.
sim 의 NED 는 참값이지만, 실기에서는 GPS 없이 NED 를 유지하려면 관성·기압 추정이 필요하다(위험 — 실기 전 검증 필요).

이 모듈은 순수 판정만 한다 — 로그·명령은 호출자(임무 루프) 몫이다.
선택기(DecisionModel)·FlightControl 구현을 모르고 Telemetry·LinkStatus·SearchArea 만 본다(D-06, SC6).
"""

import math
from collections import deque
from dataclasses import dataclass

from modules.flight_control.interface import GpsFix, Telemetry
from modules.flight_control.interface.flight_control import METERS_PER_DEG_LATITUDE
from modules.mission.operator import LinkStatus
from modules.rule_mode.interface import SearchArea

REASON_LINK_LOST = "link_lost"
REASON_GPS_LOST = "gps_lost"
REASON_GPS_JUMP = "gps_jump"
REASON_BATTERY = "battery"
REASON_GEOFENCE = "geofence"
REASON_MAX_FLIGHT_TIME = "max_flight_time"

# 판정·보고 우선순위
SAFETY_REASONS = (
    REASON_LINK_LOST,
    REASON_GPS_LOST,
    REASON_GPS_JUMP,
    REASON_BATTERY,
    REASON_GEOFENCE,
    REASON_MAX_FLIGHT_TIME,
)


def _require_finite(name: str, value: object) -> None:
    # bool 은 int 의 하위 타입이라 따로 막는다
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")


def _require_positive(name: str, value: object) -> None:
    _require_finite(name, value)
    if value <= 0:  # type: ignore[operator]
        raise ValueError(f"field '{name}' must be > 0, got {value!r}")


def _require_non_negative(name: str, value: object) -> None:
    _require_finite(name, value)
    if value < 0:  # type: ignore[operator]
        raise ValueError(f"field '{name}' must be >= 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    search_area: SearchArea
    geofence_margin_m: float = 20.0
    battery_reserve_pct: float = 15.0
    max_flight_sec: float = 300.0
    home_north_m: float = 0.0
    home_east_m: float = 0.0
    return_speed_mps: float = 5.0  # kinematic max_speed 와 같음
    landing_time_sec: float = 15.0  # 복귀 고도 10 m / 착륙 1 m/s + 여유
    default_drain_pct_per_sec: float = 0.25  # 실측 전 보수적 값 (sim 실제 100/600 ≈ 0.167)
    drain_window_sec: float = 30.0  # 소모율 실측 창
    drain_min_span_sec: float = 10.0  # 창 길이가 이보다 짧으면 기본값 사용
    gps_lost_sec: float = 2.0  # gps None 이 이 시간 이상 이어지면 gps_lost
    gps_jump_speed_mps: float = 30.0  # 연속 fix 사이 속도가 이 값 초과면 gps_jump (기체 5 m/s 의 6배)

    def __post_init__(self) -> None:
        if not isinstance(self.search_area, SearchArea):
            raise ValueError(f"field 'search_area' must be a SearchArea, got {type(self.search_area).__name__}")

        _require_non_negative("geofence_margin_m", self.geofence_margin_m)  # D-15: 0 허용

        _require_finite("battery_reserve_pct", self.battery_reserve_pct)
        if not 0 <= self.battery_reserve_pct < 100:
            raise ValueError(f"field 'battery_reserve_pct' must be in [0, 100), got {self.battery_reserve_pct!r}")

        for name in (
            "max_flight_sec",
            "return_speed_mps",
            "default_drain_pct_per_sec",
            "drain_window_sec",
            "drain_min_span_sec",
            "gps_lost_sec",
            "gps_jump_speed_mps",
        ):
            _require_positive(name, getattr(self, name))
        _require_non_negative("landing_time_sec", self.landing_time_sec)
        _require_finite("home_north_m", self.home_north_m)
        _require_finite("home_east_m", self.home_east_m)

        if self.drain_min_span_sec > self.drain_window_sec:
            raise ValueError(
                f"field 'drain_min_span_sec' must be <= 'drain_window_sec', "
                f"got {self.drain_min_span_sec!r} > {self.drain_window_sec!r}"
            )


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    reason: str  # SAFETY_REASONS 중 하나
    detail: str  # 예: "battery=21.0 required=23.4 drain=0.250", "north=150.0 east=0.0"

    def __post_init__(self) -> None:
        if self.reason not in SAFETY_REASONS:
            raise ValueError(f"field 'reason' must be one of {SAFETY_REASONS}, got {self.reason!r}")


def _distance_to_segment_m(
    north_m: float, east_m: float, start_north_m: float, start_east_m: float, end_north_m: float, end_east_m: float
) -> float:
    segment_north = end_north_m - start_north_m
    segment_east = end_east_m - start_east_m
    length_sq = segment_north**2 + segment_east**2

    # 선분 위 최근접점의 비율 t 를 [0, 1] 로 자른다 (길이 0 이면 시작점)
    t = 0.0
    if length_sq > 0:
        t = ((north_m - start_north_m) * segment_north + (east_m - start_east_m) * segment_east) / length_sq
        t = min(max(t, 0.0), 1.0)

    nearest_north = start_north_m + t * segment_north
    nearest_east = start_east_m + t * segment_east
    return math.hypot(north_m - nearest_north, east_m - nearest_east)


def is_outside_geofence(
    north_m: float,
    east_m: float,
    area: SearchArea,
    margin_m: float,
    home_north_m: float = 0.0,
    home_east_m: float = 0.0,
) -> bool:
    """구역을 margin 만큼 넓힌 사각형 밖이고, 홈→넓힌 사각형 최근접점 통로에서도 margin 보다 멀면 True (D-15)."""
    north_min = area.north_min_m - margin_m
    north_max = area.north_max_m + margin_m
    east_min = area.east_min_m - margin_m
    east_max = area.east_max_m + margin_m

    if north_min <= north_m <= north_max and east_min <= east_m <= east_max:
        return False

    # 홈이 넓힌 사각형 안이면 최근접점 = 홈이라 통로는 "홈 반경 margin 원" 이 된다
    corridor_end_north = min(max(home_north_m, north_min), north_max)
    corridor_end_east = min(max(home_east_m, east_min), east_max)

    corridor_distance = _distance_to_segment_m(
        north_m, east_m, home_north_m, home_east_m, corridor_end_north, corridor_end_east
    )
    return corridor_distance > margin_m


def required_battery_pct(
    distance_to_home_m: float,
    drain_pct_per_sec: float,
    reserve_pct: float,
    return_speed_mps: float,
    landing_time_sec: float,
) -> float:
    """복귀에 필요한 배터리 [%] = (거리 / 복귀 속도 + 착륙 시간) * 소모율 + 예비 (D-16)."""
    return_time_sec = distance_to_home_m / return_speed_mps + landing_time_sec
    return return_time_sec * drain_pct_per_sec + reserve_pct


def gps_speed_mps(previous: GpsFix, previous_sec: float, current: GpsFix, current_sec: float) -> float:
    """연속 fix 사이 수평 속도 [m/s] — 등장방형 근사, 고도는 보지 않는다. dt <= 0 이면 0."""
    dt = current_sec - previous_sec
    if dt <= 0:
        return 0.0

    mean_latitude_rad = math.radians((previous.latitude_deg + current.latitude_deg) / 2)
    north_m = (current.latitude_deg - previous.latitude_deg) * METERS_PER_DEG_LATITUDE
    east_m = (current.longitude_deg - previous.longitude_deg) * METERS_PER_DEG_LATITUDE * math.cos(mean_latitude_rad)

    return math.hypot(north_m, east_m) / dt


class SafetyMonitor:
    """매 틱 이력을 갱신하고 SAFETY_REASONS 순서로 첫 사유를 돌려준다.

    - 이력(배터리 창, 마지막 GPS·시각, gps None 시작 시각)은 사유가 나온 틱에도 항상 먼저 갱신한다.
    - 홈까지 거리는 telemetry NED 기준이다 — GPS 이상 중에도 같고, 실기에서는 NED 추정 품질이 위험 요소다.
    - gps 가 None 인 틱은 jump 판정을 건너뛰고, 끊김 뒤 다시 잡힌 첫 fix 는 jump 기준점만 새로 잡는다(D-18).

    최대 비행 시간 기준(이륙 명령 시각부터, MANUAL 시간 포함)은 호출자가 flight_elapsed_sec 로 넘긴다.
    """

    def __init__(self, config: SafetyConfig) -> None:
        self._config = config
        self._battery_samples: deque[tuple[float, float]] = deque()
        self._drain_pct_per_sec = config.default_drain_pct_per_sec
        self._last_gps: GpsFix | None = None
        self._last_gps_sec: float | None = None
        self._gps_missing_since_sec: float | None = None

    @property
    def drain_pct_per_sec(self) -> float:
        """현재 쓰는 소모율 — 실측 기울기 또는 기본값(음수·0 기울기면 기본값)."""
        return self._drain_pct_per_sec

    def check(
        self, now_sec: float, telemetry: Telemetry, flight_elapsed_sec: float, link: LinkStatus
    ) -> SafetyVerdict | None:
        self._update_battery_window(now_sec, telemetry.battery_pct)
        gps_speed = self._update_gps(now_sec, telemetry.gps)

        return self._find_first_violation(now_sec, telemetry, flight_elapsed_sec, link, gps_speed)

    def _update_battery_window(self, now_sec: float, battery_pct: float) -> None:
        samples = self._battery_samples
        samples.append((now_sec, battery_pct))
        while samples[0][0] < now_sec - self._config.drain_window_sec:
            samples.popleft()

        # 끝점 기울기 — 선형 sim 에서는 정확하고, 실기 노이즈는 30 s 창으로 완화한다
        first_sec, first_pct = samples[0]
        last_sec, last_pct = samples[-1]
        span_sec = last_sec - first_sec

        drain = self._config.default_drain_pct_per_sec
        if span_sec >= self._config.drain_min_span_sec:
            measured = (first_pct - last_pct) / span_sec
            if measured > 0:
                drain = measured

        self._drain_pct_per_sec = drain

    def _update_gps(self, now_sec: float, gps: GpsFix | None) -> float | None:
        """jump 판정용 속도를 돌려준다. 끊김 중이거나 기준점이 없으면 None."""
        if gps is None:
            if self._gps_missing_since_sec is None:
                self._gps_missing_since_sec = now_sec
            self._last_gps = None  # 끊김 뒤 첫 fix 는 기준점만 새로 잡는다
            self._last_gps_sec = None
            return None

        self._gps_missing_since_sec = None

        speed = None
        if self._last_gps is not None and self._last_gps_sec is not None:
            speed = gps_speed_mps(self._last_gps, self._last_gps_sec, gps, now_sec)

        self._last_gps = gps
        self._last_gps_sec = now_sec
        return speed

    def _find_first_violation(
        self,
        now_sec: float,
        telemetry: Telemetry,
        flight_elapsed_sec: float,
        link: LinkStatus,
        gps_speed: float | None,
    ) -> SafetyVerdict | None:
        config = self._config

        if not link.connected:
            last = "never" if link.last_heartbeat_sec is None else f"{link.last_heartbeat_sec:.1f}"
            return SafetyVerdict(REASON_LINK_LOST, f"last_heartbeat={last}")

        if self._gps_missing_since_sec is not None:
            missing_sec = now_sec - self._gps_missing_since_sec
            if missing_sec >= config.gps_lost_sec:
                return SafetyVerdict(REASON_GPS_LOST, f"gps_lost_for={missing_sec:.1f}")

        if gps_speed is not None and gps_speed > config.gps_jump_speed_mps:
            return SafetyVerdict(REASON_GPS_JUMP, f"speed={gps_speed:.1f}")

        distance_to_home_m = math.hypot(
            telemetry.north_m - config.home_north_m, telemetry.east_m - config.home_east_m
        )
        required_pct = required_battery_pct(
            distance_to_home_m,
            self._drain_pct_per_sec,
            config.battery_reserve_pct,
            config.return_speed_mps,
            config.landing_time_sec,
        )
        if telemetry.battery_pct <= required_pct:
            return SafetyVerdict(
                REASON_BATTERY,
                f"battery={telemetry.battery_pct:.1f} required={required_pct:.1f} drain={self._drain_pct_per_sec:.3f}",
            )

        if is_outside_geofence(
            telemetry.north_m,
            telemetry.east_m,
            config.search_area,
            config.geofence_margin_m,
            config.home_north_m,
            config.home_east_m,
        ):
            return SafetyVerdict(REASON_GEOFENCE, f"north={telemetry.north_m:.1f} east={telemetry.east_m:.1f}")

        if flight_elapsed_sec >= config.max_flight_sec:
            return SafetyVerdict(
                REASON_MAX_FLIGHT_TIME, f"elapsed={flight_elapsed_sec:.1f} max={config.max_flight_sec:.1f}"
            )

        return None
