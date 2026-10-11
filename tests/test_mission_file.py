"""임무 파일 파싱·검증·로드 테스트 (MIS-01, D-12, D-13, D-14)."""

import math
import re
from pathlib import Path

import pytest

from modules.decision.params import ParamDefaults
from modules.drone.run_mission import DEFAULT_TARGET_COLOR
from modules.mission import MissionSpec, load_mission_file
from modules.mission.mission_file import MISSION_ACTIONS, MISSION_UPPER_COLORS, UPPER_COLOR_ANY, parse_mission

EXAMPLE_MISSION_PATH = Path(__file__).resolve().parents[1] / "missions" / "red_heart.toml"

EXPECTED_EXAMPLE_SPEC = MissionSpec(
    target_class="person",
    target_upper_color="red",
    action="heart",
    search_north_min_m=20.0,
    search_north_max_m=120.0,
    search_east_min_m=-50.0,
    search_east_max_m=50.0,
    geofence_margin_m=20.0,
    battery_reserve_pct=15.0,
    max_flight_sec=300.0,
)

VALID_MISSION_TOML = """
[target]
class = "person"
upper_color = "red"
action = "heart"

[search_area]
north_m = [20.0, 120.0]
east_m = [-50.0, 50.0]

[safety]
geofence_margin_m = 20.0
battery_reserve_pct = 15.0
max_flight_sec = 300.0
"""


def _valid_mission_dict() -> dict:
    # 매 호출 새 dict — 테스트가 고쳐 써도 서로 영향이 없다
    return {
        "target": {"class": "person", "upper_color": "red", "action": "heart"},
        "search_area": {"north_m": [20.0, 120.0], "east_m": [-50.0, 50.0]},
        "safety": {"geofence_margin_m": 20.0, "battery_reserve_pct": 15.0, "max_flight_sec": 300.0},
    }


def _field_pattern(field: str) -> str:
    return re.escape(f"field '{field}'")


# --- 계약 상수 ---


def test_contract_constants() -> None:
    assert UPPER_COLOR_ANY == "any"
    assert MISSION_UPPER_COLORS == ("red", "any")
    assert MISSION_ACTIONS == ("heart",)


# --- 정상 파싱 ---


def test_parse_valid_mission() -> None:
    assert parse_mission(_valid_mission_dict()) == EXPECTED_EXAMPLE_SPEC


def test_upper_color_any_becomes_none() -> None:
    data = _valid_mission_dict()
    data["target"]["upper_color"] = "any"

    assert parse_mission(data).target_upper_color is None


def test_int_values_are_stored_as_float() -> None:
    data = _valid_mission_dict()
    data["search_area"]["north_m"] = [20, 120]
    data["safety"]["max_flight_sec"] = 300

    spec = parse_mission(data)

    assert spec.search_north_min_m == 20.0 and isinstance(spec.search_north_min_m, float)
    assert spec.search_north_max_m == 120.0 and isinstance(spec.search_north_max_m, float)
    assert spec.max_flight_sec == 300.0 and isinstance(spec.max_flight_sec, float)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("safety", "geofence_margin_m", 0.0),
        ("safety", "battery_reserve_pct", 0.0),
        ("search_area", "east_m", [-100.0, -10.0]),
    ],
)
def test_boundary_values_are_accepted(section: str, key: str, value: object) -> None:
    data = _valid_mission_dict()
    data[section][key] = value

    parse_mission(data)


def test_mission_spec_is_frozen() -> None:
    spec = parse_mission(_valid_mission_dict())

    with pytest.raises(AttributeError):
        spec.max_flight_sec = 1.0  # type: ignore[misc]


# --- 섹션·키 구조 ---


def test_not_a_table_is_rejected() -> None:
    with pytest.raises(ValueError, match="mission must be a table"):
        parse_mission(["target"])  # type: ignore[arg-type]


@pytest.mark.parametrize("section", ["target", "search_area", "safety"])
def test_missing_section_is_rejected(section: str) -> None:
    data = _valid_mission_dict()
    del data[section]

    with pytest.raises(ValueError, match=_field_pattern(section) + " is required"):
        parse_mission(data)


@pytest.mark.parametrize("section", ["target", "search_area", "safety"])
def test_section_must_be_table(section: str) -> None:
    data = _valid_mission_dict()
    data[section] = "oops"

    with pytest.raises(ValueError, match=_field_pattern(section) + " must be a table"):
        parse_mission(data)


@pytest.mark.parametrize("section", ["return_point", "home"])
def test_unknown_section_is_rejected(section: str) -> None:
    data = _valid_mission_dict()
    data[section] = {"north_m": 0.0}

    with pytest.raises(ValueError, match=_field_pattern(section) + " is not allowed"):
        parse_mission(data)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("target", "class"),
        ("target", "upper_color"),
        ("target", "action"),
        ("search_area", "north_m"),
        ("search_area", "east_m"),
        ("safety", "geofence_margin_m"),
        ("safety", "battery_reserve_pct"),
        ("safety", "max_flight_sec"),
    ],
)
def test_missing_key_is_rejected(section: str, key: str) -> None:
    data = _valid_mission_dict()
    del data[section][key]

    with pytest.raises(ValueError, match=_field_pattern(f"{section}.{key}") + " is required"):
        parse_mission(data)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("target", "colour"),
        ("safety", "home_north_m"),  # 복귀 지점 필드 없음 (D-12)
        ("search_area", "home"),
        ("safety", "return_altitude_m"),
    ],
)
def test_unknown_key_is_rejected(section: str, key: str) -> None:
    data = _valid_mission_dict()
    data[section][key] = 1.0

    with pytest.raises(ValueError, match=_field_pattern(f"{section}.{key}") + " is not allowed"):
        parse_mission(data)


def test_typo_reports_not_allowed_before_required() -> None:
    data = _valid_mission_dict()
    data["target"]["colour"] = data["target"].pop("upper_color")

    with pytest.raises(ValueError, match=_field_pattern("target.colour") + " is not allowed"):
        parse_mission(data)


# --- target 값 ---


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("class", "", "must be a non-empty string"),
        ("class", 3, "must be a non-empty string"),
        ("upper_color", "blue", "must be one of ('red', 'any')"),
        ("upper_color", None, "must be one of ('red', 'any')"),
        ("action", "fire", "must be one of ('heart',)"),
    ],
)
def test_invalid_target_value_is_rejected(key: str, value: object, message: str) -> None:
    data = _valid_mission_dict()
    data["target"][key] = value

    with pytest.raises(ValueError, match=_field_pattern(f"target.{key}") + " " + re.escape(message)):
        parse_mission(data)


# --- search_area 값 ---


@pytest.mark.parametrize("key", ["north_m", "east_m"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([20.0], "must be [min, max] list of 2 numbers"),
        ([1.0, 2.0, 3.0], "must be [min, max] list of 2 numbers"),
        ("20,120", "must be [min, max] list of 2 numbers"),
        (20.0, "must be [min, max] list of 2 numbers"),
        (["a", 120.0], "must be a finite number"),
        ([True, 120.0], "must be a finite number"),
        ([20.0, math.nan], "must be a finite number"),
        ([-math.inf, 120.0], "must be a finite number"),
        ([120.0, 20.0], "must be [min, max] with min < max"),
        ([50.0, 50.0], "must be [min, max] with min < max"),
    ],
)
def test_invalid_search_range_is_rejected(key: str, value: object, message: str) -> None:
    data = _valid_mission_dict()
    data["search_area"][key] = value

    with pytest.raises(ValueError, match=_field_pattern(f"search_area.{key}") + " " + re.escape(message)):
        parse_mission(data)


# --- safety 값 ---


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("geofence_margin_m", -0.1, "must be >= 0"),
        ("battery_reserve_pct", -1.0, "must be in [0, 100)"),
        ("battery_reserve_pct", 100.0, "must be in [0, 100)"),
        ("max_flight_sec", 0.0, "must be > 0"),
        ("max_flight_sec", -5.0, "must be > 0"),
    ],
)
def test_out_of_range_safety_value_is_rejected(key: str, value: float, message: str) -> None:
    data = _valid_mission_dict()
    data["safety"][key] = value

    with pytest.raises(ValueError, match=_field_pattern(f"safety.{key}") + " " + re.escape(message)):
        parse_mission(data)


@pytest.mark.parametrize("key", ["geofence_margin_m", "battery_reserve_pct", "max_flight_sec"])
@pytest.mark.parametrize("value", [True, math.nan, math.inf, "20"])
def test_non_finite_safety_value_is_rejected(key: str, value: object) -> None:
    data = _valid_mission_dict()
    data["safety"][key] = value

    with pytest.raises(ValueError, match=_field_pattern(f"safety.{key}") + " must be a finite number"):
        parse_mission(data)


# --- 파일 로드 ---


@pytest.mark.parametrize("as_path", [True, False])
def test_load_valid_file(tmp_path: Path, as_path: bool) -> None:
    mission_path = tmp_path / "mission.toml"
    mission_path.write_text(VALID_MISSION_TOML, encoding="utf-8")

    spec = load_mission_file(mission_path if as_path else str(mission_path))

    assert spec == EXPECTED_EXAMPLE_SPEC


def test_load_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_mission_file(tmp_path / "nope.toml")


def test_load_toml_syntax_error(tmp_path: Path) -> None:
    mission_path = tmp_path / "broken.toml"
    mission_path.write_text("[target\nclass = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="^" + re.escape(f"invalid mission file {mission_path}: ")):
        load_mission_file(mission_path)


def test_load_passes_field_errors_through(tmp_path: Path) -> None:
    mission_path = tmp_path / "bad_value.toml"
    mission_path.write_text(VALID_MISSION_TOML.replace('action = "heart"', 'action = "fire"'), encoding="utf-8")

    with pytest.raises(ValueError, match=_field_pattern("target.action")):
        load_mission_file(mission_path)


# --- 예제 임무 파일 ---


def test_example_mission_file_loads() -> None:
    assert load_mission_file(EXAMPLE_MISSION_PATH) == EXPECTED_EXAMPLE_SPEC


def test_example_mission_matches_param_defaults() -> None:
    # 기본 실행과 예제 파일 실행이 같은 구역·같은 상체 색을 쓴다 (C1).
    # 상체 색 기본값은 ParamDefaults 가 아니라 run_mission --target-color 기본값에 있다
    spec = load_mission_file(EXAMPLE_MISSION_PATH)
    defaults = ParamDefaults()

    assert spec.search_north_min_m == defaults.search_area_north_min_m
    assert spec.search_north_max_m == defaults.search_area_north_max_m
    assert spec.search_east_min_m == defaults.search_area_east_min_m
    assert spec.search_east_max_m == defaults.search_area_east_max_m
    assert spec.target_upper_color == DEFAULT_TARGET_COLOR


# --- 선택 키 search_area.altitude_m ---


def test_search_altitude_is_optional_and_defaults_to_none():
    assert parse_mission(_valid_mission_dict()).search_altitude_m is None


def test_search_altitude_is_read_when_given():
    data = _valid_mission_dict()
    data["search_area"]["altitude_m"] = 8.0

    assert parse_mission(data).search_altitude_m == 8.0


@pytest.mark.parametrize("bad_value", [0.0, -3.0, float("nan"), "8"])
def test_search_altitude_rejects_non_positive_or_non_number(bad_value):
    data = _valid_mission_dict()
    data["search_area"]["altitude_m"] = bad_value

    with pytest.raises(ValueError, match="search_area.altitude_m"):
        parse_mission(data)


# --- 선택 키 safety.battery_check ---


def test_battery_check_defaults_to_true_and_reads_false():
    assert parse_mission(_valid_mission_dict()).battery_check is True

    data = _valid_mission_dict()
    data["safety"]["battery_check"] = False
    assert parse_mission(data).battery_check is False


def test_battery_check_must_be_bool():
    data = _valid_mission_dict()
    data["safety"]["battery_check"] = "no"

    with pytest.raises(ValueError, match="safety.battery_check"):
        parse_mission(data)
