"""modules/yolo_detection/run_yolo.py CLI 테스트 (DET-01, DET-02, 계약 C4).

base 환경(ultralytics·cv2 없음)에서 돈다 — 가짜 모델을 주입해 인자 파싱, 벤치 계산, JSON lines 출력을 검증한다.
실제 yolo11n 으로 도는 CLI 는 tests/test_yolo_live.py 가 맡는다.
"""

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest

from modules.yolo_detection.interface.detection import Detection, Frame
from modules.yolo_detection.run_yolo import (
    build_bench_summary,
    parse_args,
    parse_device,
    run_detection,
    stream_frames,
    summarize_latencies,
)
from modules.yolo_detection.source import YoloDetectionSource

BENCH_SUMMARY_KEYS = {
    "weights",
    "device",
    "frames_measured",
    "warmup",
    "fps_mean",
    "latency_ms_mean",
    "latency_ms_p95",
    "image_width",
    "image_height",
}


@dataclass(frozen=True)
class _FakeResult:
    orig_shape: tuple[int, int]
    boxes: Any


class _FakeTrackModel:
    def __init__(self) -> None:
        self.track_kwargs: list[dict] = []

    def track(self, image: Any, **kwargs) -> list[_FakeResult]:
        self.track_kwargs.append(kwargs)
        return [_FakeResult(orig_shape=(720, 1280), boxes=None)]


class _ListDetectionSource:
    def __init__(self, frames: list[Frame]) -> None:
        self._frames = iter(frames)

    def read_frame(self) -> Frame | None:
        return next(self._frames, None)


class _ScriptedClock:
    def __init__(self, times: list[float]) -> None:
        self._times = iter(times)

    def __call__(self) -> float:
        return next(self._times)


def _make_frame(frame_id: int) -> Frame:
    detection = Detection(
        class_id=2,
        class_name="car",
        confidence=0.9,
        xyxy=(10.0, 20.0, 110.0, 80.0),
        track_id=frame_id + 1,
    )
    return Frame(
        frame_id=frame_id,
        timestamp_sec=frame_id * 0.1,
        image_width=1280,
        image_height=720,
        detections=(detection,),
    )


def _make_detection_source(frame_count: int) -> tuple[YoloDetectionSource, _FakeTrackModel]:
    model = _FakeTrackModel()
    timed_images = [(index * 0.1, object()) for index in range(frame_count)]
    return YoloDetectionSource(timed_images, model=model, device="cpu"), model


# --- 인자 파싱 ---


def test_parse_args_defaults() -> None:
    args = parse_args(["--frames", "d"])

    assert args.weights == "models/yolo11n.pt"
    assert args.device == "auto"
    assert args.tracker == "bytetrack.yaml"
    assert args.fps == 10.0
    assert args.output is None
    assert args.bench is False
    assert args.warmup == 10
    assert args.frames == "d"
    assert args.video is None


@pytest.mark.parametrize("argv", [[], ["--frames", "d", "--video", "v.mp4"]])
def test_parse_args_requires_exactly_one_input(argv) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", None), ("0", 0), ("mps", "mps"), ("cpu", "cpu")],
)
def test_parse_device_maps_cli_value(value, expected) -> None:
    result = parse_device(value)

    assert result == expected
    assert type(result) is type(expected)


# --- 지연 요약 ---


def test_summarize_latencies_skips_warmup() -> None:
    summary = summarize_latencies([1.0] * 2 + [0.1] * 20, warmup=2)

    assert summary == {
        "frames_measured": 20,
        "warmup": 2,
        "fps_mean": 10.0,
        "latency_ms_mean": 100.0,
        "latency_ms_p95": 100.0,
    }


def test_summarize_latencies_uses_nearest_rank_p95() -> None:
    summary = summarize_latencies([index / 1000 for index in range(1, 21)], warmup=0)

    assert summary["latency_ms_mean"] == 10.5
    assert summary["latency_ms_p95"] == 19.0
    assert summary["fps_mean"] == 95.24


@pytest.mark.parametrize(("latencies", "warmup"), [([0.1, 0.1], 2), ([0.1, 0.1], -1)])
def test_summarize_latencies_rejects_bad_warmup(latencies, warmup) -> None:
    with pytest.raises(ValueError):
        summarize_latencies(latencies, warmup=warmup)


# --- 스트리밍 ---


def test_stream_frames_measures_each_read_and_writes_json_lines() -> None:
    frames = [_make_frame(index) for index in range(3)]
    clock = _ScriptedClock([0.0, 0.05, 1.0, 1.05, 2.0, 2.05, 3.0, 3.0])
    lines: list[str] = []

    latencies, last_frame = stream_frames(_ListDetectionSource(frames), lines.append, clock)

    assert latencies == pytest.approx([0.05, 0.05, 0.05])
    assert last_frame == frames[-1]
    assert [Frame.from_dict(json.loads(line)) for line in lines] == frames


def test_stream_frames_without_writer_still_returns_latencies() -> None:
    frames = [_make_frame(index) for index in range(2)]
    clock = _ScriptedClock([0.0, 0.1, 1.0, 1.1, 2.0, 2.0])

    latencies, last_frame = stream_frames(_ListDetectionSource(frames), None, clock)

    assert latencies == pytest.approx([0.1, 0.1])
    assert last_frame == frames[-1]


def test_build_bench_summary_has_contract_keys() -> None:
    summary = build_bench_summary("models/yolo11n.pt", 0, [0.1] * 5, 1, _make_frame(0))

    assert set(summary) == BENCH_SUMMARY_KEYS
    assert summary["device"] == "0"
    assert summary["weights"] == "models/yolo11n.pt"
    assert (summary["image_width"], summary["image_height"]) == (1280, 720)


# --- run_detection ---


def test_run_detection_bench_writes_one_summary_line() -> None:
    args = parse_args(["--frames", "x", "--bench", "--warmup", "1"])
    detection_source, model = _make_detection_source(5)
    result_stream = io.StringIO()

    exit_code = run_detection(args, detection_source, "cpu", result_stream)

    lines = result_stream.getvalue().splitlines()
    summary = json.loads(lines[0])
    assert exit_code == 0
    assert len(lines) == 1
    assert summary["frames_measured"] == 4
    assert (summary["image_width"], summary["image_height"]) == (1280, 720)
    assert summary["device"] == "cpu"
    assert model.track_kwargs[0]["persist"] is True
    assert model.track_kwargs[0]["tracker"] == "bytetrack.yaml"


def test_run_detection_writes_frame_lines_to_result_stream() -> None:
    args = parse_args(["--frames", "x"])
    detection_source, _ = _make_detection_source(5)
    result_stream = io.StringIO()

    exit_code = run_detection(args, detection_source, "cpu", result_stream)

    lines = result_stream.getvalue().splitlines()
    assert exit_code == 0
    assert [Frame.from_dict(json.loads(line)).frame_id for line in lines] == [0, 1, 2, 3, 4]


def test_run_detection_output_option_writes_file_only(tmp_path) -> None:
    output_path = tmp_path / "out.jsonl"
    args = parse_args(["--frames", "x", "--output", str(output_path)])
    detection_source, _ = _make_detection_source(5)
    result_stream = io.StringIO()

    exit_code = run_detection(args, detection_source, "cpu", result_stream)

    assert exit_code == 0
    assert result_stream.getvalue() == ""
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 5


def test_run_detection_bench_with_too_few_frames_fails_silently_on_stdout() -> None:
    args = parse_args(["--frames", "x", "--bench", "--warmup", "10"])
    detection_source, _ = _make_detection_source(0)
    result_stream = io.StringIO()

    exit_code = run_detection(args, detection_source, "cpu", result_stream)

    assert exit_code != 0
    assert result_stream.getvalue() == ""
