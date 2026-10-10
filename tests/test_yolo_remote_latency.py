"""원격 YOLO 지연 분해·분포 표와 bench CLI 검사 (SIM-05, D-04).

bench 는 실제 07-01 서버(port 0)에 가짜 모델·디코더를 붙여 돌린다 — ultralytics·cv2 없이 base 환경에서 통과한다.
"""

import socket
import threading
from pathlib import Path

import pytest

from modules.yolo_detection.remote import bench
from modules.yolo_detection.remote.latency import FrameLatency, calculate_percentile, format_latency_table
from modules.yolo_detection.remote.server import YoloDetectionService, create_yolo_server
from tests.yolo_fakes import FakeResult, FakeYoloModel, make_boxes

QUEUED_RESULT_COUNT = 10
PERSON_BOX = ([10.0, 20.0, 30.0, 40.0], 0, 0.9, 1)


# --- calculate_percentile ---


def test_percentile_of_empty_values_raises() -> None:
    with pytest.raises(ValueError):
        calculate_percentile([], 50)


@pytest.mark.parametrize("q", [0, 50, 95, 100])
def test_percentile_of_single_value_is_that_value(q: float) -> None:
    assert calculate_percentile([10.0], q) == 10.0


def test_percentile_interpolates_linearly() -> None:
    assert calculate_percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.5
    assert calculate_percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert calculate_percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


@pytest.mark.parametrize("q", [-1, 100.5])
def test_percentile_rejects_out_of_range_q(q: float) -> None:
    with pytest.raises(ValueError):
        calculate_percentile([1.0, 2.0], q)


# --- FrameLatency ---


def test_network_ms_is_total_minus_orin() -> None:
    latency = FrameLatency.from_timing(1.5, total_ms=20.0, orin_ms=15.0)

    assert latency == FrameLatency(timestamp_sec=1.5, total_ms=20.0, orin_ms=15.0, network_ms=5.0)


def test_network_ms_is_never_negative() -> None:
    latency = FrameLatency.from_timing(1.5, total_ms=12.0, orin_ms=15.0)

    assert latency.network_ms == 0.0


# --- format_latency_table ---


def test_table_lists_count_failures_and_percentiles_per_metric() -> None:
    latencies = [FrameLatency.from_timing(index * 0.1, total_ms=10.0 + index, orin_ms=6.0) for index in range(5)]

    lines = format_latency_table(latencies, failures=2)

    assert lines[0].startswith("n=5 failures=2")
    assert any("p50" in line and "p95" in line and "max" in line for line in lines)
    rows = {line.split()[0]: line for line in lines[1:] if line.split()[0] in {"total", "orin", "net"}}
    assert set(rows) == {"total", "orin", "net"}
    assert "14.0" in rows["total"]  # total max
    assert "6.0" in rows["orin"]
    assert "8.0" in rows["net"]  # net max = 14 - 6


def test_table_without_latencies_has_single_line() -> None:
    lines = format_latency_table([], failures=3)

    assert lines == ["n=0 failures=3"]


# --- bench CLI ---


def decode_fake_jpeg(jpeg_bytes: bytes) -> object | None:
    return None if jpeg_bytes == b"BAD" else object()


@pytest.fixture
def server_url():
    results = [FakeResult(make_boxes([PERSON_BOX])) for _ in range(QUEUED_RESULT_COUNT)]
    service = YoloDetectionService(FakeYoloModel(results=results), decode=decode_fake_jpeg)
    service.warmup(object())

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


def write_images(directory: Path) -> None:
    (directory / "a.jpg").write_bytes(b"jpeg-a")
    (directory / "b.jpg").write_bytes(b"jpeg-b")


def test_bench_prints_table_for_requested_count(server_url: str, tmp_path: Path, capsys) -> None:
    write_images(tmp_path)

    exit_code = bench.main(["--url", server_url, "--images", str(tmp_path), "--count", "3"])

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "n=3 failures=0" in stdout
    row_names = {line.split()[0] for line in stdout.splitlines() if line.strip()}
    assert {"total", "orin", "net"} <= row_names


def test_bench_returns_2_for_empty_directory(tmp_path: Path, capsys) -> None:
    exit_code = bench.main(["--url", "http://127.0.0.1:1", "--images", str(tmp_path)])

    assert exit_code == 2
    assert "no .jpg" in capsys.readouterr().err


def test_bench_returns_1_when_server_is_down(tmp_path: Path, capsys) -> None:
    write_images(tmp_path)

    exit_code = bench.main(["--url", get_closed_port_url(), "--images", str(tmp_path), "--timeout-sec", "0.5"])

    assert exit_code == 1
    assert capsys.readouterr().err.strip()


def test_get_jpeg_paths_sorts_jpg_and_jpeg(tmp_path: Path) -> None:
    for name in ("b.jpeg", "a.jpg", "c.png"):
        (tmp_path / name).write_bytes(b"x")

    assert [path.name for path in bench.get_jpeg_paths(tmp_path)] == ["a.jpg", "b.jpeg"]


def test_iter_jpeg_items_cycles_files_with_distinct_timestamps(tmp_path: Path) -> None:
    write_images(tmp_path)
    paths = bench.get_jpeg_paths(tmp_path)

    items = list(bench.iter_jpeg_items(paths, 3))

    assert [round(timestamp_sec, 3) for timestamp_sec, _ in items] == [0.0, 0.1, 0.2]
    assert [jpeg_bytes for _, jpeg_bytes in items] == [b"jpeg-a", b"jpeg-b", b"jpeg-a"]
