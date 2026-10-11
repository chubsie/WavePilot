"""임무 파일 — TOML 한 장을 읽어 검증한 MissionSpec 을 돌려준다 (MIS-01, D-14).

TOML 모양 (모든 섹션·키 필수 — 선택 키 표시 제외, 모르는 섹션·키는 거부):
    [target]       class, upper_color ("red" | "any"), action ("heart")
    [search_area]  north_m, east_m — 각각 [min, max], min < max
                   altitude_m (선택, > 0) — 탐색 고도. 없으면 ParamDefaults.search_altitude_m
    [safety]       battery_check (선택, bool, 기본 true) — false 면 배터리 부족 강제 복귀를 끈다(시뮬 데모용)
    [safety]       geofence_margin_m (>= 0), battery_reserve_pct (0 <= x < 100), max_flight_sec (> 0)
    [selector]     (선택 섹션) allow_return_home (선택, bool, 기본 true) — false 면 선택기가 return_home 을 고르지 않는다.
                   안전 감시의 강제 복귀(지오펜스·최대 비행시간·배터리)는 그대로다

복귀 지점 필드는 없다 — RETURN 은 initialize GPS(운용자 위치, NED 원점)로 간다 (D-12).
탐색 구역은 홈 기준 북·동 미터 사각형이다 (D-13).
표준 라이브러리만 import 한다 (import 경계 — 다른 모듈에 의존하지 않는다).
"""

import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

UPPER_COLOR_ANY = "any"  # 파일 값 "any" -> target_upper_color None
MISSION_UPPER_COLORS = ("red", UPPER_COLOR_ANY)
MISSION_ACTIONS = ("heart",)

# 섹션별 허용 키 — 필수 키 검사와 모르는 키 거부를 이 표 하나로 한다 (D-14)
_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "target": ("class", "upper_color", "action"),
    "search_area": ("north_m", "east_m"),
    "safety": ("geofence_margin_m", "battery_reserve_pct", "max_flight_sec"),
}
# 선택 키 — 없어도 되지만 있으면 검증한다. 맵마다 탐지 가능한 고도가 달라 탐색 고도는 파일에서 바꿀 수 있게 둔다
_OPTIONAL_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "search_area": ("altitude_m",),
    "safety": ("battery_check",),
    "selector": ("allow_return_home",),
}
# 선택 섹션 — 없어도 되는 섹션. 키는 모두 _OPTIONAL_SECTION_KEYS 의 선택 키다
_OPTIONAL_SECTIONS = ("selector",)


@dataclass(frozen=True, slots=True)
class MissionSpec:
    target_class: str
    target_upper_color: str | None  # "red" 또는 None(any)
    action: str  # "heart"
    search_north_min_m: float
    search_north_max_m: float
    search_east_min_m: float
    search_east_max_m: float
    geofence_margin_m: float  # >= 0
    battery_reserve_pct: float  # 0 <= x < 100
    max_flight_sec: float  # > 0
    search_altitude_m: float | None = None  # > 0, None 이면 기본 탐색 고도
    battery_check: bool = True  # False 면 배터리 강제 복귀를 끈다
    allow_return_home: bool = True  # False 면 선택기가 return_home 을 고르지 않는다(강제 복귀는 그대로)


def parse_mission(data: dict) -> MissionSpec:
    if not isinstance(data, Mapping):
        raise ValueError(f"mission must be a table, got {data!r}")

    _validate_sections(data)
    for section, keys in _SECTION_KEYS.items():
        _validate_section_keys(section, data[section], keys, _OPTIONAL_SECTION_KEYS.get(section, ()))
    for section in _OPTIONAL_SECTIONS:
        if section in data:
            _validate_section_keys(section, data[section], (), _OPTIONAL_SECTION_KEYS[section])

    target_class, target_upper_color, action = _read_target(data["target"])

    search_area = data["search_area"]
    north_min_m, north_max_m = _read_search_range("search_area.north_m", search_area["north_m"])
    east_min_m, east_max_m = _read_search_range("search_area.east_m", search_area["east_m"])
    search_altitude_m = _read_optional_altitude("search_area.altitude_m", search_area.get("altitude_m"))

    geofence_margin_m, battery_reserve_pct, max_flight_sec = _read_safety(data["safety"])
    battery_check = _read_optional_bool("safety.battery_check", data["safety"].get("battery_check"), default=True)

    selector = data.get("selector", {})
    allow_return_home = _read_optional_bool(
        "selector.allow_return_home", selector.get("allow_return_home"), default=True
    )

    return MissionSpec(
        target_class=target_class,
        target_upper_color=target_upper_color,
        action=action,
        search_north_min_m=north_min_m,
        search_north_max_m=north_max_m,
        search_east_min_m=east_min_m,
        search_east_max_m=east_max_m,
        geofence_margin_m=geofence_margin_m,
        battery_reserve_pct=battery_reserve_pct,
        max_flight_sec=max_flight_sec,
        search_altitude_m=search_altitude_m,
        battery_check=battery_check,
        allow_return_home=allow_return_home,
    )


def load_mission_file(path: str | os.PathLike) -> MissionSpec:
    # 파일 없음(FileNotFoundError)은 잡지 않는다 — 호출 쪽이 그대로 받는다
    with open(path, "rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"invalid mission file {os.fspath(path)}: {error}") from error

    return parse_mission(data)


# --- 모듈 내부 도우미 ---


def _validate_sections(data: Mapping) -> None:
    for section in data:
        if section not in _SECTION_KEYS and section not in _OPTIONAL_SECTIONS:
            raise ValueError(f"field '{section}' is not allowed")

    for section in _SECTION_KEYS:
        if section not in data:
            raise ValueError(f"field '{section}' is required")
        if not isinstance(data[section], Mapping):
            raise ValueError(f"field '{section}' must be a table, got {data[section]!r}")

    for section in _OPTIONAL_SECTIONS:
        if section in data and not isinstance(data[section], Mapping):
            raise ValueError(f"field '{section}' must be a table, got {data[section]!r}")


def _validate_section_keys(
    section: str, table: Mapping, allowed_keys: tuple[str, ...], optional_keys: tuple[str, ...] = ()
) -> None:
    # 모르는 키를 먼저 본다 — 오타가 "required" 보다 먼저 드러나게
    for key in table:
        if key not in allowed_keys and key not in optional_keys:
            raise ValueError(f"field '{section}.{key}' is not allowed")

    for key in allowed_keys:
        if key not in table:
            raise ValueError(f"field '{section}.{key}' is required")


def _read_target(target: Mapping) -> tuple[str, str | None, str]:
    target_class = target["class"]
    if not isinstance(target_class, str) or not target_class:
        raise ValueError(f"field 'target.class' must be a non-empty string, got {target_class!r}")

    upper_color = target["upper_color"]
    if upper_color not in MISSION_UPPER_COLORS:
        raise ValueError(f"field 'target.upper_color' must be one of {MISSION_UPPER_COLORS}, got {upper_color!r}")

    action = target["action"]
    if action not in MISSION_ACTIONS:
        raise ValueError(f"field 'target.action' must be one of {MISSION_ACTIONS}, got {action!r}")

    target_upper_color = None if upper_color == UPPER_COLOR_ANY else upper_color
    return target_class, target_upper_color, action


def _read_search_range(name: str, value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"field '{name}' must be [min, max] list of 2 numbers, got {value!r}")

    min_m = _require_finite_number(name, value[0])
    max_m = _require_finite_number(name, value[1])
    if min_m >= max_m:
        raise ValueError(f"field '{name}' must be [min, max] with min < max, got {value!r}")

    return min_m, max_m


def _read_safety(safety: Mapping) -> tuple[float, float, float]:
    geofence_margin_m = _require_finite_number("safety.geofence_margin_m", safety["geofence_margin_m"])
    if geofence_margin_m < 0:
        raise ValueError(f"field 'safety.geofence_margin_m' must be >= 0, got {geofence_margin_m!r}")

    battery_reserve_pct = _require_finite_number("safety.battery_reserve_pct", safety["battery_reserve_pct"])
    if not 0 <= battery_reserve_pct < 100:
        raise ValueError(f"field 'safety.battery_reserve_pct' must be in [0, 100), got {battery_reserve_pct!r}")

    max_flight_sec = _require_finite_number("safety.max_flight_sec", safety["max_flight_sec"])
    if max_flight_sec <= 0:
        raise ValueError(f"field 'safety.max_flight_sec' must be > 0, got {max_flight_sec!r}")

    return geofence_margin_m, battery_reserve_pct, max_flight_sec


def _read_optional_bool(name: str, value: object, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"field '{name}' must be a bool, got {value!r}")
    return value


def _read_optional_altitude(name: str, value: object) -> float | None:
    if value is None:
        return None

    altitude_m = _require_finite_number(name, value)
    if altitude_m <= 0:
        raise ValueError(f"field '{name}' must be > 0, got {altitude_m!r}")
    return altitude_m


def _require_finite_number(name: str, value: object) -> float:
    # bool 은 int 하위 타입이라 따로 막는다 (params._require_finite 와 같은 규칙)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")

    return float(value)
