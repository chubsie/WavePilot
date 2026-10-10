"""시나리오 로더·SimClock (Task 1) 과 정방향 카메라·ScenarioDetectionSource (Task 2) 테스트."""

from pathlib import Path

import pytest

from interfaces.detection import COCO_CLASS_IDS
from mocks.clock import SimClock
from mocks.scenario import DEFAULT_CONFIDENCE, load_scenario

SCENARIO_NAMES = ("checkpoint", "road_crossing", "crowd_near_truck")
VEHICLE_KINDS = {"car", "bus", "truck"}

VALID_HEADER = 'name = "tmp"\nduration_sec = 10.0\n'
VALID_OBJECT = """
[[objects]]
id = "veh-1"
kind = "car"
size_m = [4.5, 1.8, 1.5]
waypoints = [ {t = 0.0, north = 30.0, east = -10.0}, {t = 10.0, north = 30.0, east = 10.0} ]
"""


def _write_scenario(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "scenario.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- Task 1: 시나리오 파일 ---


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_files_load_with_vehicle_and_person(scenarios_dir: Path, name: str) -> None:
    scenario = load_scenario(scenarios_dir / f"{name}.toml")

    kinds = {obj.kind for obj in scenario.objects}
    assert scenario.name == name
    assert scenario.duration_sec > 0
    assert kinds & VEHICLE_KINDS
    assert "person" in kinds


def test_scenario_track_ids_follow_file_order(scenarios_dir: Path) -> None:
    scenario = load_scenario(scenarios_dir / "crowd_near_truck.toml")

    track_ids = [obj.track_id for obj in scenario.objects]
    assert track_ids == list(range(1, len(scenario.objects) + 1))


def test_scenario_confidence_defaults(tmp_path: Path) -> None:
    scenario = load_scenario(_write_scenario(tmp_path, VALID_HEADER + VALID_OBJECT))

    assert scenario.objects[0].confidence == DEFAULT_CONFIDENCE


def test_scenario_position_interpolates_and_holds(tmp_path: Path) -> None:
    scenario = load_scenario(_write_scenario(tmp_path, VALID_HEADER + VALID_OBJECT))
    car = scenario.objects[0]

    assert car.position_at(0.0) == pytest.approx((30.0, -10.0))
    assert car.position_at(5.0) == pytest.approx((30.0, 0.0))
    assert car.position_at(25.0) == pytest.approx((30.0, 10.0))


def test_scenario_single_waypoint_is_static(scenarios_dir: Path) -> None:
    scenario = load_scenario(scenarios_dir / "checkpoint.toml")
    person = next(obj for obj in scenario.objects if obj.kind == "person")

    assert person.position_at(0.0) == person.position_at(33.3) == pytest.approx((22.0, 5.0))


def test_scenario_ground_truth_at_returns_every_object(scenarios_dir: Path) -> None:
    scenario = load_scenario(scenarios_dir / "checkpoint.toml")

    truth = scenario.ground_truth_at(20.0)

    assert [item.object_id for item in truth] == [obj.object_id for obj in scenario.objects]
    car = next(item for item in truth if item.kind == "car")
    assert (car.north_m, car.east_m) == pytest.approx((30.0, 0.0))
    assert car.track_id == 1


@pytest.mark.parametrize(
    ("text", "field"),
    [
        (VALID_HEADER + VALID_OBJECT.replace('"car"', '"tank"'), "objects[0].kind"),
        (VALID_HEADER + VALID_OBJECT.replace("[4.5, 1.8, 1.5]", "[4.5, 1.8]"), "objects[0].size_m"),
        (VALID_HEADER.replace("10.0", "0.0") + VALID_OBJECT, "duration_sec"),
        (
            VALID_HEADER + VALID_OBJECT.replace("{t = 10.0", "{t = -1.0"),
            "objects[0].waypoints",
        ),
        (VALID_HEADER + VALID_OBJECT + "confidence = 0.0\n", "objects[0].confidence"),
        (VALID_HEADER + VALID_OBJECT + "confidence = 1.5\n", "objects[0].confidence"),
        (VALID_HEADER + VALID_OBJECT + VALID_OBJECT, "objects[1].id"),
        (VALID_HEADER, "objects"),
    ],
)
def test_scenario_invalid_file_names_the_field(tmp_path: Path, text: str, field: str) -> None:
    path = _write_scenario(tmp_path, text)

    with pytest.raises(ValueError, match=rf"scenario tmp: {field.replace('[', r'\[').replace(']', r'\]')}"):
        load_scenario(path)


def test_scenario_decreasing_waypoint_time_rejected(tmp_path: Path) -> None:
    text = VALID_HEADER + VALID_OBJECT.replace(
        "waypoints = [ {t = 0.0",
        "waypoints = [ {t = 5.0, north = 0.0, east = 0.0}, {t = 0.0",
    )

    with pytest.raises(ValueError, match="non-decreasing"):
        load_scenario(_write_scenario(tmp_path, text))


# --- Task 1: SimClock ---


def test_clock_starts_at_zero_and_advances() -> None:
    clock = SimClock()

    assert clock.now_sec == 0.0
    clock.advance_to(1.5)
    assert clock.now_sec == 1.5


def test_clock_rejects_going_backwards() -> None:
    clock = SimClock()
    clock.advance_to(1.0)

    with pytest.raises(ValueError):
        clock.advance_to(0.5)
