"""브라우저 프레임 -> YOLO DetectionSource -> 결과를 bridge 로 되돌림 (SIM-01, D-02, D-11, D-12).

가짜 YOLO 모델(tests/yolo_fakes.py)과 문자열 sentinel 을 내는 가짜 디코더로 돌린다 — cv2·ultralytics 없이 base 환경에서 통과한다.
"""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import BrowserFrame, SimBridge
from modules.simulation.browser.detection_source import (
    TELEMETRY_HISTORY_SIZE,
    BrowserDetectionSource,
    BrowserFrameStream,
    describe_frame,
    run_live_yolo,
)
from modules.yolo_detection.interface import DetectionSource
from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
SHORT_POLL_SEC = 0.05
JOIN_TIMEOUT_SEC = 2.0
BAD_JPEG = b"bad"
PERSON_BOX = ([100.0, 200.0, 150.0, 300.0], 0, 0.9, 3)


class FakeClock:
    def __init__(self, now_sec: float = 0.0) -> None:
        self.now_sec = now_sec

    def __call__(self) -> float:
        return self.now_sec


def _decode_stub(jpeg_bytes: bytes) -> object:
    return None if jpeg_bytes == BAD_JPEG else f"image:{jpeg_bytes.decode()}"


def _make_telemetry(time_sec: float) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=0.0, east_m=0.0, down_m=-30.0, roll_deg=0.0, pitch_deg=0.0,
        yaw_deg=0.0, gimbal_pitch_deg=-45.0, gimbal_yaw_deg=0.0, battery_pct=90.0, flight_mode="GUIDED",
    )


def _make_frame(seq: int, timestamp_sec: float, received_at_sec: float = 0.0, jpeg_bytes: bytes | None = None) -> BrowserFrame:
    return BrowserFrame(
        seq=seq,
        timestamp_sec=timestamp_sec,
        jpeg_bytes=jpeg_bytes if jpeg_bytes is not None else f"jpeg{seq}".encode(),
        telemetry=_make_telemetry(timestamp_sec),
        received_at_sec=received_at_sec,
    )


def _make_model(result_count: int) -> FakeYoloModel:
    return FakeYoloModel([FakeResult(make_boxes([PERSON_BOX])) for _ in range(result_count)])


def _make_factory(model: FakeYoloModel):
    return lambda items: create_jpeg_yolo_source(items, model=model, decode=_decode_stub)


# --- jpeg_source ---


def test_create_jpeg_yolo_source_skips_undecodable_and_feeds_decoded_images() -> None:
    model = _make_model(2)
    source = create_jpeg_yolo_source([(0.1, b"a"), (0.2, BAD_JPEG), (0.3, b"c")], model=model, decode=_decode_stub)

    frames = [source.read_frame(), source.read_frame()]

    assert [frame.timestamp_sec for frame in frames] == [0.1, 0.3]
    assert source.read_frame() is None
    assert [call.image for call in model.calls] == ["image:a", "image:c"]


# --- BrowserFrameStream ---


def test_stream_yields_only_newest_frame() -> None:
    bridge = SimBridge()
    bridge.put_frame(_make_frame(1, 0.1))
    bridge.put_frame(_make_frame(2, 0.2))
    stream = BrowserFrameStream(bridge, poll_timeout_sec=SHORT_POLL_SEC)

    first = next(iter(stream))

    assert first == (0.2, b"jpeg2")
    assert stream.last_frame.seq == 2


def test_stream_never_yields_same_frame_twice_across_timeout() -> None:
    bridge = SimBridge()
    bridge.put_frame(_make_frame(2, 0.2))
    stream = BrowserFrameStream(bridge, poll_timeout_sec=SHORT_POLL_SEC)
    items = iter(stream)
    next(items)
    # 같은 프레임만 슬롯에 있는 채로 몇 번 타임아웃이 지난 뒤 새 프레임이 온다
    timer = threading.Timer(SHORT_POLL_SEC * 3, bridge.put_frame, args=(_make_frame(3, 0.3),))
    timer.start()

    second = next(items)

    timer.join()
    assert second == (0.3, b"jpeg3")


def test_stream_rejects_non_positive_poll_timeout() -> None:
    with pytest.raises(ValueError):
        BrowserFrameStream(SimBridge(), poll_timeout_sec=0)


def test_stream_stops_when_bridge_closed_externally() -> None:
    bridge = SimBridge()
    stream = BrowserFrameStream(bridge, poll_timeout_sec=SHORT_POLL_SEC)
    bridge.close()

    assert list(stream) == []


# --- BrowserDetectionSource ---


def test_read_frame_publishes_detections_and_latency() -> None:
    bridge = SimBridge()
    clock = FakeClock(10.25)
    bridge.put_frame(_make_frame(1, 0.1, received_at_sec=10.0))
    source = BrowserDetectionSource(bridge, yolo_factory=_make_factory(_make_model(1)), clock=clock, poll_timeout_sec=SHORT_POLL_SEC)

    frame = source.read_frame()

    assert frame is not None
    assert frame.timestamp_sec == 0.1
    assert frame.detections[0].track_id == 3
    assert bridge.latest_detections() == frame.to_dict()
    assert bridge.status()["yolo_latency_ms"] == pytest.approx(250.0)


def test_telemetry_for_returns_telemetry_attached_to_frame() -> None:
    bridge = SimBridge()
    browser_frame = _make_frame(1, 0.1234)
    bridge.put_frame(browser_frame)
    source = BrowserDetectionSource(bridge, yolo_factory=_make_factory(_make_model(1)), poll_timeout_sec=SHORT_POLL_SEC)

    frame = source.read_frame()

    assert source.telemetry_for(frame.timestamp_sec) is browser_frame.telemetry
    assert source.telemetry_for(99.0) is None


def test_telemetry_history_keeps_last_frames_only() -> None:
    bridge = SimBridge()
    stream = BrowserFrameStream(bridge, poll_timeout_sec=SHORT_POLL_SEC)
    items = iter(stream)
    frame_count = TELEMETRY_HISTORY_SIZE + 5

    for seq in range(1, frame_count + 1):
        bridge.put_frame(_make_frame(seq, seq / 10))
        next(items)

    assert stream.telemetry_for(0.1) is None
    assert stream.telemetry_for(frame_count / 10) is not None
    assert stream.telemetry_for((frame_count - TELEMETRY_HISTORY_SIZE + 1) / 10) is not None


def test_close_unblocks_read_frame() -> None:
    bridge = SimBridge()
    source = BrowserDetectionSource(bridge, yolo_factory=_make_factory(_make_model(0)), poll_timeout_sec=SHORT_POLL_SEC)
    results: list = []
    thread = threading.Thread(target=lambda: results.append(source.read_frame()))
    thread.start()

    source.close()
    thread.join(timeout=JOIN_TIMEOUT_SEC)

    assert not thread.is_alive()
    assert results == [None]


def test_browser_detection_source_is_detection_source() -> None:
    source = BrowserDetectionSource(SimBridge(), yolo_factory=_make_factory(_make_model(0)))

    assert isinstance(source, DetectionSource)


# --- describe_frame / run_live_yolo ---


def test_run_live_yolo_prints_one_line_per_frame_until_close() -> None:
    bridge = SimBridge()
    lines: list[str] = []
    bridge.put_frame(_make_frame(1, 0.1))

    def on_frame(line: str) -> None:
        lines.append(line)
        if len(lines) == 1:
            bridge.put_frame(_make_frame(2, 0.2))
        else:
            bridge.close()

    run_live_yolo(bridge, yolo_factory=_make_factory(_make_model(2)), on_frame=on_frame)

    assert len(lines) == 2
    assert lines[0] == "frame 0 t=0.10s 1 det | person#3 0.90"
    assert lines[1].startswith("frame 1 t=0.20s")


def test_describe_frame_without_detections() -> None:
    model = FakeYoloModel([FakeResult(None)])
    frame = create_jpeg_yolo_source([(0.5, b"a")], model=model, decode=_decode_stub).read_frame()

    assert describe_frame(frame) == "frame 0 t=0.50s 0 det"


# --- base 환경 격리 ---


def test_modules_import_without_heavy_deps() -> None:
    blocker = "; ".join(f"sys.modules[{name!r}] = None" for name in ("cv2", "numpy", "torch", "ultralytics"))
    code = f"import sys; {blocker}; import modules.simulation.browser.detection_source, modules.yolo_detection.jpeg_source"

    completed = subprocess.run([sys.executable, "-c", code], cwd=WAVEPILOT_ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
