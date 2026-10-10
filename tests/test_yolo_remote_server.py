"""Orin YOLO HTTP 서버 검사 (SIM-05, D-02) — 실제 소켓(port 0) + 가짜 모델·디코더.

ultralytics·cv2 없이 돈다: FakeYoloModel 이 track() 을 대신하고, 디코더는 바이트 접두어로 성공/실패를 정한다.
"""

import http.client
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from modules.yolo_detection.remote.protocol import (
    DETECT_PATH,
    HEALTH_PATH,
    JPEG_CONTENT_TYPE,
    MAX_JPEG_BYTES,
    RESET_PATH,
    TIMESTAMP_HEADER,
    format_timestamp_header,
    parse_detect_response,
)
from modules.yolo_detection.remote.server import YoloDetectionService, create_yolo_server
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

WAVEPILOT_ROOT = Path(__file__).resolve().parents[1]
DECODED_IMAGE = object()  # 디코더가 돌려주는 가짜 BGR 이미지
GOOD_JPEG = b"jpeg-ok-frame"
BROKEN_JPEG = b"broken"
RAISING_JPEG = b"raise"
QUEUED_RESULT_COUNT = 20

BOX_ROWS = [([10.0, 20.0, 30.0, 40.0], 0, 0.9, 7), ([100.0, 110.0, 200.0, 210.0], 2, 0.5, 8)]


@dataclass
class RecordingTracker:
    reset_count: int = 0

    def reset(self) -> None:
        self.reset_count += 1


@dataclass
class FakePredictor:
    trackers: list[RecordingTracker] = field(default_factory=lambda: [RecordingTracker(), RecordingTracker()])


@dataclass
class FailingYoloModel(FakeYoloModel):
    """다음 track() 한 번을 RuntimeError 로 실패시킬 수 있고, ultralytics 처럼 predictor.trackers 를 가진다."""

    fail_next: bool = False
    predictor: FakePredictor = field(default_factory=FakePredictor)

    def track(self, source, **kwargs) -> list[FakeResult]:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("engine exploded")
        return super().track(source, **kwargs)


def decode_fake_jpeg(jpeg_bytes: bytes) -> object:
    if jpeg_bytes.startswith(RAISING_JPEG):
        raise OSError("decoder crashed")
    if jpeg_bytes.startswith(GOOD_JPEG):
        return DECODED_IMAGE
    return None


@dataclass
class RunningServer:
    url: str
    model: FailingYoloModel
    service: YoloDetectionService


@pytest.fixture
def running_server():
    results = [FakeResult(boxes=make_boxes(BOX_ROWS)) for _ in range(QUEUED_RESULT_COUNT)]
    model = FailingYoloModel(results=results)
    service = YoloDetectionService(model, decode=decode_fake_jpeg, weights="models/fake.engine", device=0)
    service.warmup(object())

    server = create_yolo_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    host, port = server.server_address[:2]
    yield RunningServer(url=f"http://{host}:{port}", model=model, service=service)

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


OPENER = build_opener(ProxyHandler({}))  # 로컬 테스트가 시스템 프록시를 타지 않게


def send_request(request: Request) -> tuple[int, dict]:
    try:
        with OPENER.open(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def get_json(url: str) -> tuple[int, dict]:
    return send_request(Request(url))


def post_detect(url: str, body: bytes, timestamp_header: str | None = "12.5") -> tuple[int, dict]:
    headers = {"Content-Type": JPEG_CONTENT_TYPE}
    if timestamp_header is not None:
        headers[TIMESTAMP_HEADER] = timestamp_header
    return send_request(Request(url + DETECT_PATH, data=body, headers=headers, method="POST"))


def post_reset(url: str) -> tuple[int, dict]:
    return send_request(Request(url + RESET_PATH, data=b"", method="POST"))


def test_health_reports_ready_and_counts_frames(running_server: RunningServer) -> None:
    status, body = get_json(running_server.url + HEALTH_PATH)

    assert status == 200
    assert body == {"status": "ok", "weights": "models/fake.engine", "device": "0", "frames": 0}

    post_detect(running_server.url, GOOD_JPEG)
    post_detect(running_server.url, GOOD_JPEG)
    _, body = get_json(running_server.url + HEALTH_PATH)

    assert body["frames"] == 2


def test_detect_round_trips_frame(running_server: RunningServer) -> None:
    status, body = post_detect(running_server.url, GOOD_JPEG, format_timestamp_header(12.5))

    assert status == 200
    frame, timing = parse_detect_response(json.dumps(body))
    assert frame.frame_id == 0
    assert frame.timestamp_sec == 12.5
    assert (frame.image_width, frame.image_height) == (1280, 720)
    assert [detection.class_name for detection in frame.detections] == ["person", "car"]
    assert [detection.track_id for detection in frame.detections] == [7, 8]
    assert min(timing.decode_ms, timing.infer_ms, timing.total_ms) >= 0
    assert timing.total_ms >= timing.infer_ms


def test_detect_rounds_timestamp_like_local_source(running_server: RunningServer) -> None:
    _, body = post_detect(running_server.url, GOOD_JPEG, format_timestamp_header(1.23456))

    frame, _ = parse_detect_response(json.dumps(body))

    assert frame.timestamp_sec == round(1.23456, 3)


def test_track_state_persists_across_requests(running_server: RunningServer) -> None:
    warmup_calls = running_server.model.call_count

    _, first = post_detect(running_server.url, GOOD_JPEG)
    _, second = post_detect(running_server.url, GOOD_JPEG)

    assert (first["frame"]["frame_id"], second["frame"]["frame_id"]) == (0, 1)
    request_calls = running_server.model.calls[warmup_calls:]
    assert len(request_calls) == 2
    assert all(call.kwargs["persist"] is True for call in request_calls)
    assert all(call.image is DECODED_IMAGE for call in request_calls)


def test_warmup_does_not_count_and_resets_trackers(running_server: RunningServer) -> None:
    assert running_server.model.call_count == 1
    assert running_server.service.frame_count == 0
    assert [tracker.reset_count for tracker in running_server.model.predictor.trackers] == [1, 1]


@pytest.mark.parametrize(
    ("body", "timestamp_header", "expected_status"),
    [
        pytest.param(BROKEN_JPEG, "12.5", 422, id="broken-jpeg"),
        pytest.param(RAISING_JPEG, "12.5", 422, id="decoder-raises"),
        pytest.param(GOOD_JPEG, None, 400, id="missing-timestamp"),
        pytest.param(GOOD_JPEG, "abc", 400, id="non-numeric-timestamp"),
        pytest.param(GOOD_JPEG, "inf", 400, id="non-finite-timestamp"),
        pytest.param(b"", "12.5", 400, id="empty-body"),
    ],
)
def test_detect_rejects_bad_request_without_tracking(
    running_server: RunningServer, body: bytes, timestamp_header: str | None, expected_status: int
) -> None:
    call_count = running_server.model.call_count

    status, response = post_detect(running_server.url, body, timestamp_header)

    assert status == expected_status
    assert isinstance(response["error"], str)
    assert running_server.model.call_count == call_count
    assert running_server.service.frame_count == 0


def test_detect_rejects_oversized_body_without_reading(running_server: RunningServer) -> None:
    host_port = running_server.url.removeprefix("http://")
    connection = http.client.HTTPConnection(host_port, timeout=5)

    # 본문은 보내지 않는다 — 서버가 읽으려 들면 타임아웃으로 테스트가 실패한다
    connection.putrequest("POST", DETECT_PATH)
    connection.putheader(TIMESTAMP_HEADER, "12.5")
    connection.putheader("Content-Type", JPEG_CONTENT_TYPE)
    connection.putheader("Content-Length", str(MAX_JPEG_BYTES + 1))
    connection.endheaders()
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()

    assert response.status == 400
    assert "too large" in body["error"]
    assert running_server.model.call_count == 1


def test_inference_error_returns_500_and_server_keeps_serving(running_server: RunningServer) -> None:
    running_server.model.fail_next = True

    status, body = post_detect(running_server.url, GOOD_JPEG)

    assert status == 500
    assert "engine exploded" in body["error"]

    status, body = post_detect(running_server.url, GOOD_JPEG)

    assert status == 200
    assert body["frame"]["frame_id"] == 0


def test_reset_restarts_frame_id_and_resets_trackers(running_server: RunningServer) -> None:
    post_detect(running_server.url, GOOD_JPEG)
    post_detect(running_server.url, GOOD_JPEG)

    status, body = post_reset(running_server.url)

    assert status == 200
    assert body == {"status": "reset"}
    assert [tracker.reset_count for tracker in running_server.model.predictor.trackers] == [2, 2]

    _, detect_body = post_detect(running_server.url, GOOD_JPEG)
    _, health_body = get_json(running_server.url + HEALTH_PATH)

    assert detect_body["frame"]["frame_id"] == 0
    assert health_body["frames"] == 3  # reset 은 누적 처리 수를 지우지 않는다


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/nope"), ("POST", "/nope"), ("GET", DETECT_PATH)],
)
def test_unknown_path_returns_404(running_server: RunningServer, method: str, path: str) -> None:
    data = b"" if method == "POST" else None

    status, body = send_request(Request(running_server.url + path, data=data, method=method))

    assert status == 404
    assert isinstance(body["error"], str)


def test_server_imports_without_heavy_deps() -> None:
    code = (
        "import sys; import modules.yolo_detection.remote.protocol, modules.yolo_detection.remote.server; "
        "loaded = {'cv2', 'numpy', 'ultralytics', 'torch'} & set(sys.modules); "
        "assert not loaded, loaded"
    )

    completed = subprocess.run([sys.executable, "-c", code], cwd=WAVEPILOT_ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
