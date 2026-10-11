"""track id 안정화 테스트 — ByteTrack 이 같은 대상에 새 id 를 줘도 Frame 의 track_id 가 유지되는지.

- TrackIdStabilizer: 같은 클래스·가까운 위치·짧은 간격이면 직전 안정 id 를 잇고, 아니면 새 id.
- 커스텀 트래커 yaml: 존재·키·값, YoloDetectionSource 기본값으로 쓰이는지.
- ultralytics 가 있으면(yolo 그룹) yaml 이 실제 BYTETracker 로 로드되고, 작은 흔들리는 박스에서 기본 설정보다 id 가 덜 바뀌는지.
"""

import random
from itertools import pairwise
from pathlib import Path

import pytest

from modules.yolo_detection.interface.detection import Detection, Frame
from modules.yolo_detection.source import DEFAULT_TRACKER, YoloDetectionSource
from modules.yolo_detection.trackers.id_stabilizer import TrackIdStabilizer
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

TRACKER_YAML = Path(DEFAULT_TRACKER)
EXPECTED_TRACKER_KEYS = {
    "tracker_type", "track_high_thresh", "track_low_thresh", "new_track_thresh", "track_buffer", "match_thresh", "fuse_score",
}
PERSON_HEIGHT_PX = 30.0


def _person(track_id: int | None, center_x: float, center_y: float = 180.0, height: float = PERSON_HEIGHT_PX) -> Detection:
    width = height * 0.4
    xyxy = (center_x - width / 2, center_y - height / 2, center_x + width / 2, center_y + height / 2)
    return Detection(class_id=0, class_name="person", confidence=0.4, xyxy=xyxy, track_id=track_id)


def _car(track_id: int, center_x: float) -> Detection:
    return Detection(class_id=2, class_name="car", confidence=0.6, xyxy=(center_x - 20, 160.0, center_x + 20, 200.0), track_id=track_id)


def _frame(frame_id: int, timestamp_sec: float, *detections: Detection) -> Frame:
    return Frame(frame_id=frame_id, timestamp_sec=timestamp_sec, image_width=640, image_height=360, detections=detections)


def _ids(frame: Frame) -> list[int | None]:
    return [detection.track_id for detection in frame.detections]


# --- TrackIdStabilizer ---


def test_switched_id_at_same_place_keeps_stable_id() -> None:
    stabilizer = TrackIdStabilizer()

    first = stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))
    switched = stabilizer.stabilize(_frame(1, 0.5, _person(31, 304.0)))
    switched_again = stabilizer.stabilize(_frame(2, 1.0, _person(33, 309.0)))

    assert _ids(first) == [30]
    assert _ids(switched) == [30]
    assert _ids(switched_again) == [30]


def test_old_raw_id_returning_keeps_same_stable_id() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))
    stabilizer.stabilize(_frame(1, 0.4, _person(31, 302.0)))

    returned = stabilizer.stabilize(_frame(2, 0.8, _person(30, 303.0)))

    assert _ids(returned) == [30]


def test_far_away_new_id_gets_its_own_id() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 100.0)))

    frame = stabilizer.stabilize(_frame(1, 0.3, _person(31, 500.0)))

    assert _ids(frame) == [31]


def test_new_id_after_long_gap_gets_its_own_id() -> None:
    stabilizer = TrackIdStabilizer(max_gap_sec=1.5)
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))

    frame = stabilizer.stabilize(_frame(1, 2.0, _person(31, 300.0)))

    assert _ids(frame) == [31]


def test_new_id_of_other_class_is_not_linked() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))

    frame = stabilizer.stabilize(_frame(1, 0.3, _car(31, 300.0)))

    assert _ids(frame) == [31]


def test_new_id_with_very_different_box_height_is_not_linked() -> None:
    stabilizer = TrackIdStabilizer(max_height_ratio=2.0)
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0, height=20.0)))

    frame = stabilizer.stabilize(_frame(1, 0.3, _person(31, 300.0, height=60.0)))

    assert _ids(frame) == [31]


def test_new_person_next_to_visible_person_is_not_merged() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))

    frame = stabilizer.stabilize(_frame(1, 0.1, _person(30, 301.0), _person(31, 310.0)))

    assert _ids(frame) == [30, 31]


def test_two_people_switching_ids_each_keep_their_own_id() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 200.0), _person(31, 400.0)))

    frame = stabilizer.stabilize(_frame(1, 0.5, _person(40, 398.0), _person(41, 203.0)))

    assert _ids(frame) == [31, 30]


def test_untracked_detection_passes_through() -> None:
    stabilizer = TrackIdStabilizer()

    frame = stabilizer.stabilize(_frame(0, 0.0, _person(None, 300.0), _person(7, 100.0)))

    assert _ids(frame) == [None, 7]


def test_reused_raw_number_after_tracker_reset_gets_fresh_id() -> None:
    stabilizer = TrackIdStabilizer(forget_after_sec=5.0)
    stabilizer.stabilize(_frame(0, 0.0, _person(1, 100.0), _person(2, 500.0)))

    # 트래커가 리셋돼 raw id 1 이 다른 사람에게 다시 붙었다 — 번호 1 은 이미 발급했으므로 새 번호를 준다
    frame = stabilizer.stabilize(_frame(1, 6.0, _person(1, 300.0)))

    assert _ids(frame) == [3]


def test_frame_fields_other_than_track_id_are_preserved() -> None:
    stabilizer = TrackIdStabilizer()
    stabilizer.stabilize(_frame(0, 0.0, _person(30, 300.0)))
    original = _frame(5, 0.5, _person(31, 302.0))

    frame = stabilizer.stabilize(original)

    assert frame.frame_id == 5
    assert frame.timestamp_sec == 0.5
    assert frame.detections[0].xyxy == original.detections[0].xyxy
    assert frame.detections[0].confidence == original.detections[0].confidence


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"max_gap_sec": -1.0}, "max_gap_sec"),
        ({"max_height_ratio": 0.5}, "max_height_ratio"),
        ({"max_gap_sec": 3.0, "forget_after_sec": 1.0}, "forget_after_sec"),
    ],
)
def test_stabilizer_rejects_invalid_args(kwargs: dict, field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        TrackIdStabilizer(**kwargs)


def test_yolo_source_outputs_stable_ids() -> None:
    results = [
        FakeResult(boxes=make_boxes([([290.0, 165.0, 302.0, 195.0], 0, 0.4, 30.0)])),
        FakeResult(boxes=make_boxes([([292.0, 166.0, 304.0, 196.0], 0, 0.3, 31.0)])),
    ]
    source = YoloDetectionSource([(0.0, object()), (0.4, object())], model=FakeYoloModel(list(results)))

    ids = [_ids(source.read_frame()) for _ in results]

    assert ids == [[30], [30]]


# --- 커스텀 트래커 yaml ---


def _read_flat_yaml(path: Path) -> dict[str, str]:
    """`key: value  # 주석` 한 단계짜리 yaml 만 읽는다 — base 환경에 PyYAML 이 없어도 돈다."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        key, value = content.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def test_custom_tracker_yaml_is_the_source_default() -> None:
    assert TRACKER_YAML.name == "wavepilot_bytetrack.yaml"
    assert TRACKER_YAML.is_file()


def test_custom_tracker_yaml_has_bytetrack_keys_and_tuned_values() -> None:
    values = _read_flat_yaml(TRACKER_YAML)

    assert set(values) == EXPECTED_TRACKER_KEYS
    assert values["tracker_type"] == "bytetrack"
    assert values["fuse_score"] == "False"
    assert int(values["track_buffer"]) > 30
    assert float(values["track_low_thresh"]) < float(values["track_high_thresh"])


def test_custom_tracker_yaml_loads_into_ultralytics_bytetracker() -> None:
    pytest.importorskip("ultralytics")
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import YAML, IterableSimpleNamespace
    from ultralytics.utils.checks import check_yaml

    config = IterableSimpleNamespace(**YAML.load(check_yaml(DEFAULT_TRACKER)))
    tracker = BYTETracker(config)

    assert tracker.max_frames_lost == int(_read_flat_yaml(TRACKER_YAML)["track_buffer"])
    assert config.fuse_score is False


def _count_id_switches(tracker_config: str, seed: int) -> int:
    np = pytest.importorskip("numpy")
    from ultralytics.engine.results import Boxes
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.utils import YAML, IterableSimpleNamespace
    from ultralytics.utils.checks import check_yaml

    tracker = BYTETracker(IterableSimpleNamespace(**YAML.load(check_yaml(tracker_config))))
    rng = random.Random(seed)
    center_x, bottom_y = 300.0, 200.0
    ids: list[int] = []
    for _ in range(200):
        # 드론 흔들림 지터 + 간헐적 프레임 스킵 + conf 0.25~0.6 흔들림, h 약 30 px
        center_x += 0.8 + rng.uniform(-2.5, 2.5)
        bottom_y += 0.3 + rng.uniform(-2.0, 2.0)
        if rng.random() < 0.25:
            continue
        height = PERSON_HEIGHT_PX * rng.uniform(0.8, 1.2)
        width = height * 0.4
        row = [center_x - width / 2, bottom_y - height, center_x + width / 2, bottom_y, rng.uniform(0.25, 0.6), 0.0]
        output = tracker.update(Boxes(np.array([row], dtype=np.float32), (360, 640)))
        ids.extend(int(track_row[4]) for track_row in output)
    return sum(previous != current for previous, current in pairwise(ids))


def test_custom_tracker_switches_ids_less_than_default_on_small_jittery_box() -> None:
    pytest.importorskip("ultralytics")
    seeds = range(5)

    default_switches = sum(_count_id_switches("bytetrack.yaml", seed) for seed in seeds)
    custom_switches = sum(_count_id_switches(DEFAULT_TRACKER, seed) for seed in seeds)

    assert custom_switches * 3 <= default_switches
