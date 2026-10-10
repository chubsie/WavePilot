"""Mac 쪽 원격 YOLO 클라이언트 검사 (SIM-05, D-03, D-04) — 실제 07-01 서버(port 0) + 가짜 모델·디코더.

디코더는 본문이 b"BAD" 면 None 을 내 서버가 422 로 답하게 한다. ultralytics·cv2 없이 base 환경에서 통과한다.
"""

import dataclasses
import functools
import logging
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import BrowserFrame, SimBridge
from modules.simulation.browser.detection_source import BrowserDetectionSource
from modules.yolo_detection.remote import client as client_module
from modules.yolo_detection.remote.client import RemoteYoloDetectionSource, RemoteYoloError, create_remote_yolo_source
from modules.yolo_detection.remote.protocol import MAX_JPEG_BYTES
from modules.yolo_detection.remote.server import YoloDetectionService, create_yolo_server
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_LOGGER = "modules.yolo_detection.remote.client"
TIMEOUT_SEC = 0.5
QUEUED_RESULT_COUNT = 20
GOOD_JPEG = b"jpeg-ok"
BAD_JPEG = b"BAD"
PERSON_BOX = ([10.0, 20.0, 30.0, 40.0], 0, 0.9, 5)


def decode_fake_jpeg(jpeg_bytes: bytes) -> object | None:
    return None if jpeg_bytes == BAD_JPEG else object()


@pytest.fixture
def server_url():
    results = [FakeResult(make_boxes([PERSON_BOX])) for _ in range(QUEUED_RESULT_COUNT)]
    service = YoloDetectionService(FakeYoloModel(results=results), decode=decode_fake_jpeg)
    service.warmup(object())  # 큐의 첫 결과를 쓴다

    server = create_yolo_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get_closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def create_source(url: str, items: list[tuple[float, bytes]], **kwargs) -> RemoteYoloDetectionSource:
    return RemoteYoloDetectionSource(items, url, timeout_sec=TIMEOUT_SEC, **kwargs)


def read_all_frames(source: RemoteYoloDetectionSource) -> list:
    frames = []
    while (frame := source.read_frame()) is not None:
        frames.append(frame)
    return frames


# --- 정상 왕복 ---


def test_items_round_trip_into_server_frames(server_url: str, monkeypatch) -> None:
    server_totals: list[float] = []
    real_parse = client_module.parse_detect_response

    def recording_parse(body: bytes):
        frame, timing = real_parse(body)
        server_totals.append(timing.total_ms)
        return frame, timing

    monkeypatch.setattr(client_module, "parse_detect_response", recording_parse)
    source = create_source(server_url, [(0.1, GOOD_JPEG), (0.2, GOOD_JPEG), (0.3, GOOD_JPEG)])

    frames = [source.read_frame() for _ in range(3)]

    assert [frame.timestamp_sec for frame in frames] == [0.1, 0.2, 0.3]
    assert [frame.frame_id for frame in frames] == [0, 1, 2]
    assert [detection.track_id for detection in frames[0].detections] == [5]
    assert len(source.latencies) == 3
    assert [latency.timestamp_sec for latency in source.latencies] == [0.1, 0.2, 0.3]
    assert [latency.orin_ms for latency in source.latencies] == server_totals
    for latency in source.latencies:
        assert latency.network_ms == max(0.0, latency.total_ms - latency.orin_ms)
    assert source.read_frame() is None
    assert source.failure_count == 0


def test_rounded_timestamp_echo_is_accepted(server_url: str) -> None:
    source = create_source(server_url, [(1.23456, GOOD_JPEG)])

    frame = source.read_frame()

    assert frame.timestamp_sec == 1.235
    assert source.failure_count == 0


def test_latencies_is_a_copy(server_url: str) -> None:
    source = create_source(server_url, [(0.1, GOOD_JPEG)])
    source.read_frame()

    assert isinstance(source.latencies, tuple)


# --- 생성 시점 ---


def test_construction_against_closed_port_raises() -> None:
    url = get_closed_port_url()

    with pytest.raises(RemoteYoloError, match="127.0.0.1"):
        create_source(url, [(0.1, GOOD_JPEG)])


def test_construction_resets_server_frame_ids(server_url: str) -> None:
    first = create_source(server_url, [(0.1, GOOD_JPEG), (0.2, GOOD_JPEG)])
    read_all_frames(first)

    second = create_source(server_url, [(0.3, GOOD_JPEG)])

    assert second.read_frame().frame_id == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "ftp://127.0.0.1:1"},
        {"base_url": "http://127.0.0.1:1", "timeout_sec": 0},
        {"base_url": "http://127.0.0.1:1", "max_consecutive_failures": 0},
    ],
)
def test_invalid_arguments_raise_value_error(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RemoteYoloDetectionSource([], **kwargs)


# --- 프레임 실패 ---


def test_broken_jpeg_is_skipped(server_url: str) -> None:
    source = create_source(server_url, [(0.1, GOOD_JPEG), (0.2, BAD_JPEG), (0.3, GOOD_JPEG)])

    first = source.read_frame()
    second = source.read_frame()

    assert (first.timestamp_sec, second.timestamp_sec) == (0.1, 0.3)
    assert source.failure_count == 1
    assert len(source.latencies) == 2


def test_consecutive_failures_raise(server_url: str) -> None:
    source = create_source(
        server_url, [(0.1, BAD_JPEG), (0.2, BAD_JPEG), (0.3, BAD_JPEG), (0.4, GOOD_JPEG)], max_consecutive_failures=3
    )

    with pytest.raises(RemoteYoloError):
        source.read_frame()


def test_success_resets_consecutive_failures(server_url: str) -> None:
    items = [(0.1, BAD_JPEG), (0.2, BAD_JPEG), (0.3, GOOD_JPEG), (0.4, BAD_JPEG), (0.5, BAD_JPEG), (0.6, GOOD_JPEG)]
    source = create_source(server_url, items, max_consecutive_failures=3)

    frames = read_all_frames(source)

    assert [frame.timestamp_sec for frame in frames] == [0.3, 0.6]
    assert source.failure_count == 4


def test_oversized_jpeg_is_not_sent(server_url: str, caplog) -> None:
    source = create_source(server_url, [(0.1, b"x" * (MAX_JPEG_BYTES + 1)), (0.2, GOOD_JPEG)])

    with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
        frame = source.read_frame()

    assert frame.frame_id == 0  # 큰 프레임은 서버에 가지 않았다
    assert source.failure_count == 1
    assert any("0.1" in record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)


def test_timestamp_mismatch_is_a_failure(server_url: str, monkeypatch) -> None:
    real_parse = client_module.parse_detect_response

    def shifted_parse(body: bytes):
        frame, timing = real_parse(body)
        return dataclasses.replace(frame, timestamp_sec=frame.timestamp_sec + 1.0), timing

    monkeypatch.setattr(client_module, "parse_detect_response", shifted_parse)
    source = create_source(server_url, [(0.1, GOOD_JPEG)])

    assert source.read_frame() is None
    assert source.failure_count == 1


def test_bad_response_body_is_a_failure(server_url: str, monkeypatch) -> None:
    def failing_parse(body: bytes):
        raise ValueError("detect response is not valid JSON")

    monkeypatch.setattr(client_module, "parse_detect_response", failing_parse)
    source = create_source(server_url, [(0.1, GOOD_JPEG)])

    assert source.read_frame() is None
    assert source.failure_count == 1


# --- 요약 로그 ---


def get_summary_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.INFO and "n=" in record.getMessage()]


def test_summary_logged_once_at_end_of_stream(server_url: str, caplog) -> None:
    source = create_source(server_url, [(0.1, GOOD_JPEG), (0.2, BAD_JPEG)])

    with caplog.at_level(logging.INFO, logger=CLIENT_LOGGER):
        read_all_frames(source)
        source.read_frame()
        source.close()
        source.close()

    summaries = get_summary_records(caplog)
    assert len(summaries) == 1
    assert "n=1 failures=1" in summaries[0].getMessage()
    assert "total" in summaries[0].getMessage()


def test_summary_logged_on_close_before_end(server_url: str, caplog) -> None:
    source = create_source(server_url, [(0.1, GOOD_JPEG), (0.2, GOOD_JPEG)])

    with caplog.at_level(logging.INFO, logger=CLIENT_LOGGER):
        source.read_frame()
        source.close()

    assert len(get_summary_records(caplog)) == 1


def test_frame_latency_logged_at_debug(server_url: str, caplog) -> None:
    source = create_source(server_url, [(0.1, GOOD_JPEG)])

    with caplog.at_level(logging.DEBUG, logger=CLIENT_LOGGER):
        source.read_frame()

    debug_lines = [record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(debug_lines) == 1
    assert all(word in debug_lines[0] for word in ("total", "orin", "net"))


# --- BrowserDetectionSource 와의 결합 (D-03, D-05) ---


def make_telemetry(time_sec: float) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=0.0, east_m=0.0, down_m=-30.0, roll_deg=0.0, pitch_deg=0.0,
        yaw_deg=0.0, gimbal_pitch_deg=-45.0, gimbal_yaw_deg=0.0, battery_pct=90.0, flight_mode="GUIDED",
    )


def test_browser_detection_source_uses_remote_factory(server_url: str) -> None:
    bridge = SimBridge()
    browser_frame = BrowserFrame(
        seq=1, timestamp_sec=1.23456, jpeg_bytes=GOOD_JPEG, telemetry=make_telemetry(1.23456), received_at_sec=0.0
    )
    bridge.put_frame(browser_frame)
    yolo_factory = functools.partial(create_remote_yolo_source, base_url=server_url, timeout_sec=TIMEOUT_SEC)
    source = BrowserDetectionSource(bridge, yolo_factory=yolo_factory, poll_timeout_sec=0.05)

    try:
        frame = source.read_frame()
    finally:
        source.close()

    assert frame.frame_id == 0
    assert frame.timestamp_sec == 1.235
    assert source.telemetry_for(frame.timestamp_sec) is browser_frame.telemetry


# --- base 환경 ---


def test_client_imports_without_heavy_deps() -> None:
    code = (
        "import sys\n"
        "for name in ('cv2', 'numpy', 'ultralytics', 'torch'):\n"
        "    sys.modules[name] = None\n"
        "import modules.yolo_detection.remote.client, modules.yolo_detection.remote.latency, "
        "modules.yolo_detection.remote.bench\n"
    )

    completed = subprocess.run([sys.executable, "-c", code], cwd=WAVEPILOT_ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
