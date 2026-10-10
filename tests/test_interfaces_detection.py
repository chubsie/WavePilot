"""modules.yolo_detection.interface.detection 계약: YOLO 추적 필드 보존, 직렬화 왕복, 입력 검증."""

import pytest

from modules.yolo_detection.interface.detection import (
    COCO_CLASS_IDS,
    UPPER_COLORS,
    Detection,
    DetectionSource,
    Frame,
)


def _make_detection(track_id: int | None = 3) -> Detection:
    return Detection(
        class_id=COCO_CLASS_IDS["car"],
        class_name="car",
        confidence=0.87,
        xyxy=(100.0, 200.0, 180.5, 260.0),
        track_id=track_id,
    )


def _make_frame() -> Frame:
    return Frame(
        frame_id=42,
        timestamp_sec=4.2,
        image_width=1280,
        image_height=720,
        detections=(_make_detection(3), _make_detection(None)),
    )


def test_coco_class_ids_match_ultralytics() -> None:
    assert COCO_CLASS_IDS == {"person": 0, "car": 2, "bus": 5, "truck": 7}


def test_detection_round_trip_keeps_xyxy_as_tuple() -> None:
    detection = _make_detection()

    data = detection.to_dict()

    assert data["xyxy"] == [100.0, 200.0, 180.5, 260.0]
    restored = Detection.from_dict(data)
    assert restored == detection
    assert isinstance(restored.xyxy, tuple)


def test_detection_round_trip_with_untracked_id() -> None:
    detection = _make_detection(track_id=None)

    assert Detection.from_dict(detection.to_dict()) == detection


def test_frame_round_trip() -> None:
    frame = _make_frame()

    restored = Frame.from_dict(frame.to_dict())

    assert restored == frame
    assert isinstance(restored.detections, tuple)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("confidence", 1.5),
        ("xyxy", [200.0, 200.0, 100.0, 260.0]),
        ("xyxy", [100.0, 300.0, 180.0, 260.0]),
        ("track_id", "3"),
        ("class_id", "2"),
        ("xyxy", [1.0, 2.0, 3.0]),
        ("upper_color", "blue"),
        ("upper_color", 1),
        ("upper_color", True),
        ("upper_color", ""),
    ],
)
def test_detection_from_dict_rejects_bad_field(field: str, bad_value: object) -> None:
    data = _make_detection().to_dict()
    data[field] = bad_value

    with pytest.raises(ValueError):
        Detection.from_dict(data)


def test_upper_colors_are_red_and_other() -> None:
    assert UPPER_COLORS == ("red", "other")


def test_detection_upper_color_defaults_to_none_and_is_always_serialized() -> None:
    detection = _make_detection()

    data = detection.to_dict()

    assert detection.upper_color is None
    assert data["upper_color"] is None
    assert list(data) == ["class_id", "class_name", "confidence", "xyxy", "track_id", "upper_color"]


@pytest.mark.parametrize("upper_color", ["red", "other"])
def test_detection_round_trip_keeps_upper_color(upper_color: str) -> None:
    detection = Detection(0, "person", 0.9, (10.0, 20.0, 50.0, 120.0), 4, upper_color=upper_color)

    assert Detection.from_dict(detection.to_dict()) == detection


def test_detection_from_dict_reads_missing_or_null_upper_color_as_none() -> None:
    null_data = _make_detection().to_dict()
    null_data["upper_color"] = None
    missing_data = dict(null_data)
    del missing_data["upper_color"]

    assert Detection.from_dict(null_data).upper_color is None
    assert Detection.from_dict(missing_data).upper_color is None


def test_frame_round_trip_keeps_detection_upper_color() -> None:
    person = Detection(0, "person", 0.9, (10.0, 20.0, 50.0, 120.0), 4, upper_color="red")
    frame = Frame(1, 0.1, 1280, 720, (person, _make_detection()))

    restored = Frame.from_dict(frame.to_dict())

    assert restored == frame
    assert restored.detections[0].upper_color == "red"


def test_detection_from_dict_rejects_missing_key() -> None:
    data = _make_detection().to_dict()
    del data["confidence"]

    with pytest.raises(ValueError, match="confidence"):
        Detection.from_dict(data)


def test_frame_from_dict_rejects_non_list_detections() -> None:
    data = _make_frame().to_dict()
    data["detections"] = "none"

    with pytest.raises(ValueError):
        Frame.from_dict(data)


def test_class_with_read_frame_satisfies_detection_source() -> None:
    class EmptySource:
        def read_frame(self) -> Frame | None:
            return None

    assert isinstance(EmptySource(), DetectionSource)
    assert not isinstance(object(), DetectionSource)
