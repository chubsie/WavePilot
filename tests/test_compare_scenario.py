"""비교 시나리오 형식·로더·변환 (SIM-04, outline C1) 과 실제 시나리오 4개·비교 임무 파일 내용."""

import colorsys
import itertools
import json
import math
import re
from pathlib import Path

import pytest

from modules.mission import load_mission_file
from modules.rule_mode.interface import SearchArea
from modules.rule_mode.search import build_zigzag_waypoints, get_patrol_length_m
from modules.simulation.compare_scenario import (
    ACTOR_MODELS,
    SCENARIO_ID_PATTERN,
    SCENARIOS_DIR,
    SHIRT_COLORS,
    CompareScenario,
    ScenarioActor,
    build_ground_truth,
    list_scenario_ids,
    load_compare_scenario,
    parse_compare_scenario,
    scenario_to_browser_dict,
)
from modules.yolo_detection.upper_color import MIN_RED_SATURATION, RED_HUE_HIGH_MIN, RED_HUE_LOW_MAX

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]

ACTOR_NORTH_RANGE_M = (10.0, 35.0)
ACTOR_EAST_RANGE_M = (-15.0, 15.0)
MIN_ACTOR_SPACING_M = 4.0
CONFUSION_PAIR_RANGE_M = (2.0, 3.0)


def _valid_scenario_dict() -> dict:
    return {
        "name": "sample",
        "description": "테스트용",
        "mission": "missions/compare.toml",
        "battery_pct": 80,
        "actors": [
            {
                "id": "red_1",
                "model": "soldier",
                "north_m": 16,
                "east_m": -3.0,
                "heading_deg": 90.0,
                "walk_m": 6.0,
                "shirt": "red",
                "shirt_rgb": [208, 24, 24],
            },
            {
                "id": "blue_1",
                "model": "soldier",
                "north_m": 19.0,
                "east_m": 4.0,
                "heading_deg": 20.0,
                "shirt": "other",
                "shirt_rgb": [31, 79, 208],
            },
            {"id": "car_1", "model": "car", "north_m": 25.0, "east_m": 9.0, "heading_deg": 60.0},
        ],
    }


def _parse(data: dict) -> CompareScenario:
    return parse_compare_scenario(data, "sample")


# --- 계약 상수 ---


def test_contract_constants():
    assert SCENARIOS_DIR == WAVEPILOT_ROOT / "scenarios"
    assert SCENARIO_ID_PATTERN.pattern == r"^[a-z0-9_]{1,32}$"
    assert ACTOR_MODELS == ("soldier", "car")
    assert SHIRT_COLORS == ("red", "other")


# --- parse_compare_scenario: 정상 ---


def test_parse_valid_scenario():
    scenario = _parse(_valid_scenario_dict())

    assert scenario.scenario_id == "sample"
    assert scenario.description == "테스트용"
    assert scenario.mission_path == "missions/compare.toml"
    assert scenario.battery_pct == 80.0
    assert [actor.actor_id for actor in scenario.actors] == ["red_1", "blue_1", "car_1"]

    red = scenario.actors[0]
    assert red == ScenarioActor("red_1", "soldier", 16.0, -3.0, 90.0, 6.0, "red", (208, 24, 24))
    assert isinstance(red.north_m, float)
    assert isinstance(red.shirt_rgb, tuple)

    assert scenario.actors[1].walk_m == 0.0
    assert scenario.actors[2].shirt is None
    assert scenario.actors[2].shirt_rgb is None


def test_parse_battery_optional_and_100_allowed():
    data = _valid_scenario_dict()
    del data["battery_pct"]
    assert _parse(data).battery_pct is None

    data["battery_pct"] = 100
    assert _parse(data).battery_pct == 100.0


def test_parse_rejects_non_table():
    with pytest.raises(ValueError, match="scenario must be a table"):
        parse_compare_scenario([1, 2], "sample")  # type: ignore[arg-type]


# --- parse_compare_scenario: 최상위 키 ---


def test_parse_rejects_unknown_top_key():
    data = _valid_scenario_dict()
    data["home"] = [0, 0]

    with pytest.raises(ValueError, match=re.escape("field 'home' is not allowed")):
        _parse(data)


@pytest.mark.parametrize("key", ["name", "description", "mission", "actors"])
def test_parse_rejects_missing_top_key(key):
    data = _valid_scenario_dict()
    del data[key]

    with pytest.raises(ValueError, match=re.escape(f"field '{key}' is required")):
        _parse(data)


@pytest.mark.parametrize("name", ["other", 3, None])
def test_parse_rejects_name_not_stem(name):
    data = _valid_scenario_dict()
    data["name"] = name

    with pytest.raises(ValueError, match=re.escape("field 'name' must be")):
        _parse(data)


def test_parse_rejects_non_string_description():
    data = _valid_scenario_dict()
    data["description"] = 3

    with pytest.raises(ValueError, match=re.escape("field 'description' must be a string")):
        _parse(data)


@pytest.mark.parametrize("mission", ["", 3, None, "/etc/x.toml", "../x.toml", "missions/../../x.toml"])
def test_parse_rejects_bad_mission(mission):
    data = _valid_scenario_dict()
    data["mission"] = mission

    with pytest.raises(ValueError, match=re.escape("field 'mission' must be")):
        _parse(data)


@pytest.mark.parametrize("battery_pct", [0, -1, 101, True, math.nan, math.inf, "80"])
def test_parse_rejects_bad_battery(battery_pct):
    data = _valid_scenario_dict()
    data["battery_pct"] = battery_pct

    with pytest.raises(ValueError, match=re.escape("field 'battery_pct' must be")):
        _parse(data)


# --- parse_compare_scenario: actors ---


@pytest.mark.parametrize("actors", [[], "red_1", {"id": "x"}])
def test_parse_rejects_bad_actors_list(actors):
    data = _valid_scenario_dict()
    data["actors"] = actors

    with pytest.raises(ValueError, match=re.escape("field 'actors' must be")):
        _parse(data)


def test_parse_rejects_non_table_actor():
    data = _valid_scenario_dict()
    data["actors"][0] = "red_1"

    with pytest.raises(ValueError, match=re.escape("field 'actors[0]' must be a table")):
        _parse(data)


def test_parse_rejects_unknown_actor_key():
    data = _valid_scenario_dict()
    data["actors"][0]["color"] = "red"

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].color' is not allowed")):
        _parse(data)


@pytest.mark.parametrize("key", ["id", "model", "north_m", "east_m", "heading_deg", "shirt", "shirt_rgb"])
def test_parse_rejects_missing_actor_key(key):
    data = _valid_scenario_dict()
    del data["actors"][0][key]

    with pytest.raises(ValueError, match=re.escape(f"field 'actors[0].{key}' is required")):
        _parse(data)


@pytest.mark.parametrize("actor_id", ["Red 1", "a" * 33, "", 3, "red/1"])
def test_parse_rejects_bad_actor_id(actor_id):
    data = _valid_scenario_dict()
    data["actors"][0]["id"] = actor_id

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].id' must be")):
        _parse(data)


def test_parse_rejects_duplicate_actor_id():
    data = _valid_scenario_dict()
    data["actors"][1]["id"] = "red_1"

    with pytest.raises(ValueError, match=re.escape("field 'actors[1].id' must be unique")):
        _parse(data)


def test_parse_rejects_unknown_model():
    data = _valid_scenario_dict()
    data["actors"][0]["model"] = "tank"

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].model' must be one of ('soldier', 'car')")):
        _parse(data)


@pytest.mark.parametrize("key", ["north_m", "east_m", "heading_deg", "walk_m"])
@pytest.mark.parametrize("value", [True, "16", math.inf, math.nan])
def test_parse_rejects_non_finite_actor_number(key, value):
    data = _valid_scenario_dict()
    data["actors"][0][key] = value

    with pytest.raises(ValueError, match=re.escape(f"field 'actors[0].{key}' must be a finite number")):
        _parse(data)


def test_parse_rejects_negative_walk():
    data = _valid_scenario_dict()
    data["actors"][0]["walk_m"] = -1.0

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].walk_m' must be >= 0")):
        _parse(data)


@pytest.mark.parametrize("key, value", [("shirt", "red"), ("shirt_rgb", [208, 24, 24])])
def test_parse_rejects_shirt_on_car(key, value):
    data = _valid_scenario_dict()
    data["actors"][2][key] = value

    with pytest.raises(ValueError, match=re.escape(f"field 'actors[2].{key}' is not allowed")):
        _parse(data)


def test_parse_rejects_walking_car():
    data = _valid_scenario_dict()
    data["actors"][2]["walk_m"] = 2.0

    with pytest.raises(ValueError, match=re.escape("field 'actors[2].walk_m' must be 0 for car")):
        _parse(data)


def test_parse_rejects_unknown_shirt():
    data = _valid_scenario_dict()
    data["actors"][0]["shirt"] = "blue"

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].shirt' must be one of ('red', 'other')")):
        _parse(data)


@pytest.mark.parametrize(
    "shirt_rgb",
    [[208, 24], [208, 24, 24, 0], [256, 0, 0], [-1, 0, 0], [1.5, 0, 0], [True, 0, 0], "#d01818", (208, 24, 24)],
)
def test_parse_rejects_bad_shirt_rgb(shirt_rgb):
    data = _valid_scenario_dict()
    data["actors"][0]["shirt_rgb"] = shirt_rgb

    with pytest.raises(ValueError, match=re.escape("field 'actors[0].shirt_rgb' must be")):
        _parse(data)


# --- load_compare_scenario / list_scenario_ids ---

_VALID_TOML = """
name = "{name}"
description = "tmp"
mission = "missions/compare.toml"

[[actors]]
id = "car_1"
model = "car"
north_m = 20.0
east_m = 0.0
heading_deg = 0.0
"""


@pytest.mark.parametrize("scenario_id", ["../x", "Basic", "a/b", "", "a" * 33, None])
def test_load_rejects_bad_id_before_opening(tmp_path, scenario_id):
    # 바깥 파일이 실제로 있어도 열지 않는다 — FileNotFoundError 가 아니라 ValueError
    (tmp_path.parent / "x.toml").write_text(_VALID_TOML.format(name="x"))
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()

    with pytest.raises(ValueError, match="scenario id must match"):
        load_compare_scenario(scenario_id, scenarios_dir)


def test_load_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_compare_scenario("nothing", tmp_path)


def test_load_valid_file(tmp_path):
    (tmp_path / "tmp_one.toml").write_text(_VALID_TOML.format(name="tmp_one"))

    scenario = load_compare_scenario("tmp_one", tmp_path)

    assert scenario.scenario_id == "tmp_one"
    assert scenario.actors[0].model == "car"


def test_load_syntax_error(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text('name = "broken\n')

    with pytest.raises(ValueError) as excinfo:
        load_compare_scenario("broken", tmp_path)

    message = str(excinfo.value)
    assert message.startswith("invalid scenario file ")
    assert str(path) in message


def test_load_passes_parse_error_through(tmp_path):
    (tmp_path / "wrong.toml").write_text(_VALID_TOML.format(name="other"))

    with pytest.raises(ValueError, match=re.escape("field 'name' must be")):
        load_compare_scenario("wrong", tmp_path)


def test_list_scenario_ids_filters_and_sorts(tmp_path):
    for file_name in ("b.toml", "a.toml", "Bad Name.toml", "notes.txt"):
        (tmp_path / file_name).write_text("")

    assert list_scenario_ids(tmp_path) == ("a", "b")


def test_list_scenario_ids_missing_dir(tmp_path):
    assert list_scenario_ids(tmp_path / "missing") == ()


# --- scenario_to_browser_dict / build_ground_truth ---


def test_browser_dict():
    browser = scenario_to_browser_dict(_parse(_valid_scenario_dict()))

    assert browser["id"] == "sample"
    assert browser["battery_pct"] == 80.0
    assert browser["actors"][0] == {
        "id": "red_1",
        "model": "Soldier.glb",
        "dx": -3.0,
        "dz": -16.0,
        "headingDeg": 90.0,
        "walkM": 6.0,
        "shirtColor": 0xD01818,
    }
    assert browser["actors"][1]["shirtColor"] == 0x1F4FD0
    assert browser["actors"][2]["model"] == "ferrari.glb"
    assert browser["actors"][2]["shirtColor"] is None
    json.dumps(browser)


def test_browser_dict_without_battery():
    data = _valid_scenario_dict()
    del data["battery_pct"]

    assert scenario_to_browser_dict(_parse(data))["battery_pct"] is None


def test_ground_truth():
    truth = build_ground_truth(_parse(_valid_scenario_dict()))

    assert truth["scenario"] == "sample"
    assert truth["mission"] == "missions/compare.toml"
    assert truth["actors"][0] == {
        "id": "red_1",
        "kind": "person",
        "color": "red",
        "north_m": 16.0,
        "east_m": -3.0,
        "walk": {"heading_deg": 90.0, "length_m": 6.0},
    }
    assert truth["actors"][1]["color"] == "other"
    assert truth["actors"][1]["walk"] is None
    assert truth["actors"][2] == {
        "id": "car_1",
        "kind": "car",
        "color": None,
        "north_m": 25.0,
        "east_m": 9.0,
        "walk": None,
    }
    assert truth["red_ids"] == ["red_1"]
    json.dumps(truth)


# --- 실제 시나리오 4개·비교 임무 파일 내용 (D-04, D-07) ---

SCENARIO_IDS = ("basic", "confusion", "no_red", "two_red")


def _load_all() -> list[CompareScenario]:
    return [load_compare_scenario(scenario_id) for scenario_id in SCENARIO_IDS]


def _get_people(scenario: CompareScenario, shirt: str) -> list[ScenarioActor]:
    return [actor for actor in scenario.actors if actor.model == "soldier" and actor.shirt == shirt]


def _get_cars(scenario: CompareScenario) -> list[ScenarioActor]:
    return [actor for actor in scenario.actors if actor.model == "car"]


def _get_actor_points(actor: ScenarioActor) -> list[tuple[float, float]]:
    # 기본 위치 + 걷기 경로 양 끝 — 북 += cos·d, 동 += sin·d (drone_camera.html 규약)
    point_list = [(actor.north_m, actor.east_m)]
    heading_rad = math.radians(actor.heading_deg)
    for along_m in (-actor.walk_m / 2, actor.walk_m / 2):
        point_list.append(
            (actor.north_m + math.cos(heading_rad) * along_m, actor.east_m + math.sin(heading_rad) * along_m)
        )
    return point_list


def _is_red_rgb(rgb: tuple[int, int, int]) -> bool:
    hue, saturation, _value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
    hue_cv, saturation_cv = hue * 180, saturation * 255
    is_red_hue = hue_cv <= RED_HUE_LOW_MAX or hue_cv >= RED_HUE_HIGH_MIN
    return is_red_hue and saturation_cv >= MIN_RED_SATURATION


def test_real_scenarios_load():
    assert list_scenario_ids() == SCENARIO_IDS

    for scenario in _load_all():
        assert scenario.mission_path == "missions/compare.toml"
        assert scenario.battery_pct == 100.0


def test_real_scenarios_composition():
    scenarios = {scenario.scenario_id: scenario for scenario in _load_all()}

    for scenario in scenarios.values():
        assert len(_get_people(scenario, "other")) >= 2
        assert len(_get_cars(scenario)) >= 1

    basic = scenarios["basic"]
    assert (len(_get_people(basic, "red")), len(_get_people(basic, "other")), len(_get_cars(basic))) == (1, 2, 2)

    two_red = scenarios["two_red"]
    assert len(_get_people(two_red, "red")) == 2
    assert any(actor.walk_m > 0 for actor in _get_people(two_red, "red"))

    no_red = scenarios["no_red"]
    assert _get_people(no_red, "red") == []
    assert build_ground_truth(no_red)["red_ids"] == []

    confusion = scenarios["confusion"]
    pair_distances = [
        math.hypot(red.north_m - other.north_m, red.east_m - other.east_m)
        for red in _get_people(confusion, "red")
        for other in _get_people(confusion, "other")
    ]
    assert any(CONFUSION_PAIR_RANGE_M[0] <= distance <= CONFUSION_PAIR_RANGE_M[1] for distance in pair_distances)


def test_real_scenarios_placement():
    for scenario in _load_all():
        for actor in scenario.actors:
            for north_m, east_m in _get_actor_points(actor):
                assert ACTOR_NORTH_RANGE_M[0] <= north_m <= ACTOR_NORTH_RANGE_M[1], (scenario.scenario_id, actor)
                assert ACTOR_EAST_RANGE_M[0] <= east_m <= ACTOR_EAST_RANGE_M[1], (scenario.scenario_id, actor)

        for first, second in itertools.combinations(scenario.actors, 2):
            distance_m = math.hypot(first.north_m - second.north_m, first.east_m - second.east_m)
            is_confusion_pair = scenario.scenario_id == "confusion" and {first.shirt, second.shirt} == {"red", "other"}
            if is_confusion_pair and distance_m <= CONFUSION_PAIR_RANGE_M[1]:
                continue
            assert distance_m >= MIN_ACTOR_SPACING_M, (scenario.scenario_id, first.actor_id, second.actor_id)


def test_real_scenarios_shirt_colors_match_upper_color():
    for scenario in _load_all():
        for actor in scenario.actors:
            if actor.model != "soldier":
                continue
            assert _is_red_rgb(actor.shirt_rgb) == (actor.shirt == "red"), (scenario.scenario_id, actor)


def test_compare_mission_covers_actors_and_is_short():
    for scenario in _load_all():
        mission = load_mission_file(WAVEPILOT_ROOT / scenario.mission_path)
        assert mission.max_flight_sec == 180.0

        for actor in scenario.actors:
            for north_m, east_m in _get_actor_points(actor):
                assert mission.search_north_min_m <= north_m <= mission.search_north_max_m
                assert mission.search_east_min_m <= east_m <= mission.search_east_max_m

        area = SearchArea(
            mission.search_north_min_m, mission.search_north_max_m, mission.search_east_min_m, mission.search_east_max_m
        )
        waypoints = build_zigzag_waypoints(area, 15.0, 25.0, 0.0, 0.0)
        assert get_patrol_length_m(waypoints, 0.0, 0.0) <= 300.0


def test_real_scenarios_dicts_are_json():
    for scenario in _load_all():
        browser = scenario_to_browser_dict(scenario)
        truth = build_ground_truth(scenario)

        json.dumps(browser)
        json.dumps(truth)
        assert len(browser["actors"]) == len(truth["actors"]) == len(scenario.actors)
