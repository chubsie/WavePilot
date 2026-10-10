"""YoloDetectionSource 테스트 — 가짜 모델 주입으로 Frame 계약을 검증한다 (DET-01, SC1, SC2, SC3)."""

import sys
import types

import pytest

from modules.yolo_detection.interface.detection import DetectionSource, Frame
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes
from modules.yolo_detection.convert import convert_boxes
from modules.yolo_detection.source import YoloDetectionSource

DEFAULT_TRACK_KWARGS = {
    "persist": True,
    "tracker": "bytetrack.yaml",
    "classes": [0, 2, 5, 7],
    "conf": 0.25,
    "imgsz": 640,
    "device": None,
    "verbose": False,
}
INVALID_ARGS = (
    ("conf", {"conf": 0}),
    ("conf", {"conf": 1.5}),
    ("imgsz", {"imgsz": 0}),
)


def _read_all_frames(source: YoloDetectionSource) -> list[Frame]:
    frames: list[Frame] = []
    while (frame := source.read_frame()) is not None:
        frames.append(frame)
    return frames


def _make_image_source(timestamps: list[float]) -> list[tuple[float, object]]:
    return [(timestamp, object()) for timestamp in timestamps]


def _make_results(count: int) -> list[FakeResult]:
    return [
        FakeResult(boxes=make_boxes([([1.0, 2.0, 30.0, 40.0], 0, 0.9, float(index + 1))]))
        for index in range(count)
    ]


def test_yolo_source_conforms_to_detection_source() -> None:
    source = YoloDetectionSource([], model=FakeYoloModel([]))

    assert isinstance(source, DetectionSource)


def test_yolo_source_emits_sequential_frames_then_none() -> None:
    images = _make_image_source([0.0, 0.1234567, 0.2])
    model = FakeYoloModel(_make_results(3))
    source = YoloDetectionSource(images, model=model)

    frames = _read_all_frames(source)

    assert [frame.frame_id for frame in frames] == [0, 1, 2]
    assert [frame.timestamp_sec for frame in frames] == [0.0, 0.123, 0.2]
    assert all(frame.image_width == 1280 and frame.image_height == 720 for frame in frames)
    assert source.read_frame() is None
    assert model.call_count == 3


def test_yolo_source_passes_same_image_object_to_track() -> None:
    images = _make_image_source([0.0, 0.1])
    model = FakeYoloModel(_make_results(2))
    source = YoloDetectionSource(images, model=model)

    _read_all_frames(source)

    assert [call.image for call in model.calls] == [image for _, image in images]


def test_yolo_source_uses_default_track_kwargs() -> None:
    model = FakeYoloModel(_make_results(1))
    source = YoloDetectionSource(_make_image_source([0.0]), model=model)

    source.read_frame()

    assert model.calls[0].kwargs == DEFAULT_TRACK_KWARGS


def test_yolo_source_forwards_custom_track_args() -> None:
    model = FakeYoloModel(_make_results(1))
    source = YoloDetectionSource(
        _make_image_source([0.0]),
        model=model,
        device="mps",
        tracker="botsort.yaml",
        conf=0.5,
        imgsz=320,
    )

    source.read_frame()

    kwargs = model.calls[0].kwargs
    assert kwargs["device"] == "mps"
    assert kwargs["tracker"] == "botsort.yaml"
    assert kwargs["conf"] == 0.5
    assert kwargs["imgsz"] == 320


def test_yolo_source_frames_match_converted_boxes_and_round_trip() -> None:
    results = _make_results(2)
    expected = [convert_boxes(result.boxes) for result in results]
    source = YoloDetectionSource(_make_image_source([0.0, 0.1]), model=FakeYoloModel(list(results)))

    frames = _read_all_frames(source)

    assert [frame.detections for frame in frames] == expected
    assert all(Frame.from_dict(frame.to_dict()) == frame for frame in frames)


def test_yolo_source_returns_none_without_calling_model_when_images_empty() -> None:
    model = FakeYoloModel([])
    source = YoloDetectionSource([], model=model)

    assert source.read_frame() is None
    assert model.call_count == 0


def test_yolo_source_accepts_generator_image_source() -> None:
    images = (pair for pair in _make_image_source([0.0, 0.1]))
    source = YoloDetectionSource(images, model=FakeYoloModel(_make_results(2)))

    assert len(_read_all_frames(source)) == 2


def test_yolo_source_with_injected_model_never_imports_ultralytics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ultralytics", None)

    YoloDetectionSource([], model=FakeYoloModel([]))


def test_yolo_source_lazily_loads_default_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_weights: list[str] = []
    fake_module = types.ModuleType("ultralytics")
    fake_module.YOLO = lambda weights: loaded_weights.append(weights) or FakeYoloModel([])
    monkeypatch.setitem(sys.modules, "ultralytics", fake_module)

    YoloDetectionSource([])

    assert loaded_weights == ["models/yolo11n.pt"]


def test_yolo_source_passes_engine_weights_through(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded_weights: list[str] = []
    fake_module = types.ModuleType("ultralytics")
    fake_module.YOLO = lambda weights: loaded_weights.append(weights) or FakeYoloModel([])
    monkeypatch.setitem(sys.modules, "ultralytics", fake_module)

    YoloDetectionSource([], weights="models/yolo11n.engine")

    assert loaded_weights == ["models/yolo11n.engine"]


def test_yolo_source_raises_import_error_when_ultralytics_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ultralytics", None)

    with pytest.raises(ImportError):
        YoloDetectionSource([])


@pytest.mark.parametrize(("field_name", "kwargs"), INVALID_ARGS)
def test_yolo_source_rejects_invalid_args_before_importing(
    monkeypatch: pytest.MonkeyPatch, field_name: str, kwargs: dict
) -> None:
    monkeypatch.setitem(sys.modules, "ultralytics", None)

    with pytest.raises(ValueError, match=field_name):
        YoloDetectionSource([], **kwargs)
