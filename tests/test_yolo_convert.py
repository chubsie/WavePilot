"""YOLO boxes -> Detection 변환 테스트 (DET-01, SC1)."""

import pytest

from interfaces.detection import Frame
from tests.yolo_fakes import FakeBoxes, FakeTensor, make_boxes
from yolo_detection.convert import COCO_NAME_BY_ID, TRACKED_CLASS_IDS, convert_boxes

UNTRACKED_CLASS_IDS = (1, 16)  # bicycle, dog


def _build_frame(detections: tuple) -> Frame:
    return Frame(
        frame_id=0,
        timestamp_sec=0.0,
        image_width=1280,
        image_height=720,
        detections=detections,
    )


def test_coco_name_by_id_inverts_project_vocabulary() -> None:
    assert COCO_NAME_BY_ID == {0: "person", 2: "car", 5: "bus", 7: "truck"}
    assert TRACKED_CLASS_IDS == [0, 2, 5, 7]


def test_convert_boxes_maps_rows_in_order_with_int_ids() -> None:
    boxes = make_boxes([
        ([1.0, 2.0, 30.0, 40.0], 0, 0.9, 1.0),
        ([5.0, 6.0, 50.0, 60.0], 2, 0.8, 2.0),
        ([7.0, 8.0, 70.0, 80.0], 5, 0.7, 3.0),
    ])

    detections = convert_boxes(boxes)

    assert [d.class_name for d in detections] == ["person", "car", "bus"]
    assert [d.class_id for d in detections] == [0, 2, 5]
    assert [d.track_id for d in detections] == [1, 2, 3]
    assert all(type(d.class_id) is int and type(d.track_id) is int for d in detections)


def test_convert_boxes_keeps_detections_when_track_id_missing() -> None:
    boxes = make_boxes([
        ([1.0, 2.0, 30.0, 40.0], 0, 0.9, None),
        ([5.0, 6.0, 50.0, 60.0], 7, 0.8, None),
    ])

    detections = convert_boxes(boxes)

    assert len(detections) == 2
    assert all(d.track_id is None for d in detections)


def test_convert_boxes_returns_empty_for_none_and_empty_boxes() -> None:
    empty = FakeBoxes(xyxy=FakeTensor(), cls=FakeTensor(), conf=FakeTensor(), id=None)

    assert convert_boxes(None) == ()
    assert convert_boxes(empty) == ()


@pytest.mark.parametrize("class_id", UNTRACKED_CLASS_IDS)
def test_convert_boxes_drops_classes_outside_vocabulary(class_id: int) -> None:
    boxes = make_boxes([
        ([1.0, 2.0, 30.0, 40.0], class_id, 0.9, 1.0),
        ([5.0, 6.0, 50.0, 60.0], 2, 0.8, 2.0),
    ])

    detections = convert_boxes(boxes)

    assert [d.class_name for d in detections] == ["car"]


def test_convert_boxes_clamps_confidence_to_one() -> None:
    boxes = make_boxes([([1.0, 2.0, 30.0, 40.0], 0, 1.0000001, 1.0)])

    (detection,) = convert_boxes(boxes)

    assert detection.confidence == 1.0


def test_convert_boxes_rounds_xyxy_to_one_decimal() -> None:
    raw = [10.04, 20.06, 30.15, 40.0]
    boxes = make_boxes([(raw, 0, 0.9, 1.0)])

    (detection,) = convert_boxes(boxes)

    assert detection.xyxy == tuple(round(v, 1) for v in raw)
    assert all(type(v) is float for v in detection.xyxy)


def test_convert_boxes_output_round_trips_through_frame_json() -> None:
    boxes = make_boxes([
        ([1.0, 2.0, 30.0, 40.0], 0, 0.9, 1.0),
        ([5.0, 6.0, 50.0, 60.0], 2, 0.8, 2.0),
    ])
    frame = _build_frame(convert_boxes(boxes))

    assert Frame.from_dict(frame.to_dict()) == frame
