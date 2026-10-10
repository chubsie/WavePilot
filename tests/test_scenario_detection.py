"""시나리오 로더·SimClock (Task 1) 과 정방향 카메라·ScenarioDetectionSource (Task 2) 테스트."""

import re
from pathlib import Path

import pytest

from interfaces.detection import COCO_CLASS_IDS, DetectionSource
from interfaces.geometry import project_ned_to_pixel
from mocks.camera import MIN_VISIBLE_AREA_RATIO, project_object_box
from mocks.clock import SimClock
from mocks.detection_source import ScenarioDetectionSource
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

    with pytest.raises(ValueError, match=re.escape(f"scenario tmp: {field}")):
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


# --- Task 2: 정방향 카메라 모델 ---


CAR_SIZE_M = (4.5, 1.8, 1.5)


def test_camera_box_for_car_ahead_is_inside_image(hover_telemetry, default_camera) -> None:
    box = project_object_box(30.0, 0.0, CAR_SIZE_M, default_camera, hover_telemetry)

    assert box is not None
    x1, y1, x2, y2 = box
    assert 0.0 <= x1 < x2 <= default_camera.width_px
    assert 0.0 <= y1 < y2 <= default_camera.height_px


def test_camera_box_behind_or_far_outside_is_none(hover_telemetry, default_camera) -> None:
    assert project_object_box(-100.0, 0.0, CAR_SIZE_M, default_camera, hover_telemetry) is None
    assert project_object_box(30.0, 200.0, CAR_SIZE_M, default_camera, hover_telemetry) is None


def test_camera_box_mostly_clipped_is_none(hover_telemetry, default_camera) -> None:
    # 폭 20 m 객체의 중심이 화면 오른쪽 끝 바로 밖 — 일부는 보이지만 절반 미만
    wide_size_m = (1.0, 20.0, 1.0)
    east_m = _find_east_for_ground_u(30.0, default_camera.width_px + 20.0, hover_telemetry, default_camera)
    west_edge = project_ned_to_pixel((30.0, east_m - 10.0, 0.0), default_camera, hover_telemetry)

    box = project_object_box(30.0, east_m, wide_size_m, default_camera, hover_telemetry)

    assert west_edge is not None and west_edge[0] < default_camera.width_px
    assert box is None
    assert MIN_VISIBLE_AREA_RATIO == 0.5


def test_camera_box_contains_ground_center(hover_telemetry, default_camera) -> None:
    box = project_object_box(30.0, 5.0, CAR_SIZE_M, default_camera, hover_telemetry)
    ground_center = project_ned_to_pixel((30.0, 5.0, 0.0), default_camera, hover_telemetry)

    x1, y1, x2, y2 = box
    assert x1 <= ground_center[0] <= x2
    assert y1 <= ground_center[1] <= y2


def _find_east_for_ground_u(north_m, target_u, telemetry, camera) -> float:
    # 이분 탐색: east 가 커질수록 u 가 커진다 (yaw 0, 화면 오른쪽 = east)
    low_m, high_m = 0.0, 200.0
    for _ in range(60):
        middle_m = (low_m + high_m) / 2.0
        u, _v = project_ned_to_pixel((north_m, middle_m, 0.0), camera, telemetry)
        if u < target_u:
            low_m = middle_m
        else:
            high_m = middle_m
    return (low_m + high_m) / 2.0


# --- Task 2: ScenarioDetectionSource ---


class FakeFlightControl:
    """MockFlightControl 이 아닌 최소 FlightControl — 소스가 Protocol 에만 의존하는지 확인."""

    def __init__(self, telemetry) -> None:
        self._telemetry = telemetry

    def get_telemetry(self):
        return self._telemetry

    def send_command(self, command):
        from interfaces.flight_control import CommandAck

        return CommandAck(accepted=True, reason="fake")


def _read_all_frames(source) -> list:
    frames = []
    while (frame := source.read_frame()) is not None:
        frames.append(frame)
    return frames


@pytest.fixture
def checkpoint_scenario(scenarios_dir: Path):
    return load_scenario(scenarios_dir / "checkpoint.toml")


def test_source_emits_frames_at_fps_until_duration(checkpoint_scenario, hover_telemetry, default_camera) -> None:
    clock = SimClock()
    source = ScenarioDetectionSource(
        checkpoint_scenario, FakeFlightControl(hover_telemetry), default_camera, clock, fps=10.0
    )

    frames = _read_all_frames(source)

    assert isinstance(source, DetectionSource)
    assert len(frames) == 401
    assert [frame.frame_id for frame in frames[:3]] == [0, 1, 2]
    assert [frame.timestamp_sec for frame in frames[:3]] == [0.0, 0.1, 0.2]
    assert frames[-1].timestamp_sec == 40.0
    assert clock.now_sec == pytest.approx(40.0)
    assert source.read_frame() is None
    assert (frames[0].image_width, frames[0].image_height) == (1280, 720)


def test_source_detections_match_scenario_objects(checkpoint_scenario, hover_telemetry, default_camera) -> None:
    source = ScenarioDetectionSource(
        checkpoint_scenario, FakeFlightControl(hover_telemetry), default_camera, SimClock()
    )
    objects_by_track = {obj.track_id: obj for obj in checkpoint_scenario.objects}

    frames = _read_all_frames(source)

    for frame in frames:
        for detection in frame.detections:
            obj = objects_by_track[detection.track_id]
            assert detection.class_name == obj.kind
            assert detection.class_id == COCO_CLASS_IDS[obj.kind]
            assert detection.confidence == obj.confidence
            north_m, east_m = obj.position_at(frame.timestamp_sec)
            ground_u, _v = project_ned_to_pixel((north_m, east_m, 0.0), default_camera, hover_telemetry)
            assert detection.xyxy[0] <= ground_u <= detection.xyxy[2]


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_source_sees_every_object_in_every_scenario(scenarios_dir: Path, name, hover_telemetry, default_camera) -> None:
    # 시나리오는 감시 위치에서 보이는 지면 영역 안에 놓여야 한다 — 매 프레임 모든 객체가 탐지된다
    scenario = load_scenario(scenarios_dir / f"{name}.toml")
    source = ScenarioDetectionSource(scenario, FakeFlightControl(hover_telemetry), default_camera, SimClock())

    frames = _read_all_frames(source)

    assert all(len(frame.detections) == len(scenario.objects) for frame in frames)
