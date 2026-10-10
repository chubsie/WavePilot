"""실제 yolo11n + ByteTrack 라이브 스모크 (DET-01 SC1·SC4, 수동 게이트 — 기본 실행에서는 마커 yolo 로 빠진다).

실행:
    uv sync --group yolo
    uv run --group yolo pytest -m yolo tests/test_yolo_live.py -q

첫 실행은 models/yolo11n.pt 를 네트워크로 내려받는다 (API 키 불필요, 공개 GitHub release 자산).
ultralytics·cv2·torch 중 하나라도 없으면 모듈 전체가 skip 된다.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.yolo

ultralytics = pytest.importorskip("ultralytics")
cv2 = pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

from ultralytics import YOLO
from ultralytics.utils import ASSETS

from interfaces.detection import Frame
from tools.run_yolo import resolve_auto_device
from yolo_detection.source import YoloDetectionSource

ALLOWED_CLASS_NAMES = {"person", "car", "bus", "truck"}  # 4클래스 어휘 밖이 나오면 convert 필터가 깨진 것
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
}  # 계약 C4 — Jetson 런북이 이 키에 의존한다
CLI_TIMEOUT_SEC = 600.0  # 첫 실행의 모델 로드·MPS 워밍업 여유. 무한 대기는 금지
REPO_ROOT = Path(__file__).resolve().parents[1]
BUS_IMAGE_PATH = ASSETS / "bus.jpg"  # ultralytics 패키지에 동봉 — 바이너리를 커밋하지 않는다


@pytest.fixture(scope="module")
def weights_path() -> str:
    # conftest 의 wavepilot_root 는 function 스코프라 module fixture 에서 쓸 수 없다
    path = REPO_ROOT / "models" / "yolo11n.pt"
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        YOLO(str(path))

    assert path.exists(), f"weights were not downloaded to {path}"
    return str(path)


@pytest.fixture
def bus_image():
    image = cv2.imread(str(BUS_IMAGE_PATH))
    assert image is not None, f"cannot read {BUS_IMAGE_PATH}"
    return image


def _read_all_frames(source: YoloDetectionSource) -> list[Frame]:
    frames = []
    while (frame := source.read_frame()) is not None:
        frames.append(frame)
    return frames


def _copy_bus_frames(directory: Path, count: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        shutil.copyfile(BUS_IMAGE_PATH, directory / f"frame_{index:03d}.jpg")
    return directory


def _run_cli(wavepilot_root: Path, *cli_args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(wavepilot_root)}
    return subprocess.run(
        [sys.executable, "tools/run_yolo.py", *cli_args],
        cwd=wavepilot_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SEC,
    )


def test_live_bus_image_keeps_track_ids(weights_path, bus_image) -> None:
    # 트래커 상태는 모델 객체에 붙으므로 테스트마다 새 모델을 쓴다
    model = YOLO(weights_path)
    timed_images = [(0.0, bus_image), (0.1, bus_image), (0.2, bus_image)]
    source = YoloDetectionSource(timed_images, model=model, device=resolve_auto_device())

    frames = _read_all_frames(source)

    track_id_sets = [{d.track_id for d in frame.detections} for frame in frames]
    class_names = {d.class_name for frame in frames for d in frame.detections}
    assert [frame.frame_id for frame in frames] == [0, 1, 2]
    assert [frame.timestamp_sec for frame in frames] == [0.0, 0.1, 0.2]
    assert all(track_ids and None not in track_ids for track_ids in track_id_sets)
    assert track_id_sets[0] == track_id_sets[1] == track_id_sets[2]
    assert class_names <= ALLOWED_CLASS_NAMES
    assert {"bus", "person"} <= class_names
    assert all(frame.image_width == bus_image.shape[1] for frame in frames)
    assert all(frame.image_height == bus_image.shape[0] for frame in frames)
    assert all(Frame.from_dict(frame.to_dict()) == frame for frame in frames)


def test_live_auto_device_is_mps_when_available(weights_path, bus_image) -> None:
    expected = "mps" if torch.backends.mps.is_available() else "cpu"
    model = YOLO(weights_path)
    source = YoloDetectionSource([(0.0, bus_image)], model=model, device=resolve_auto_device())

    source.read_frame()

    assert resolve_auto_device() == expected
    assert model.predictor.device.type == expected


def test_live_cli_bench_prints_one_summary_line(wavepilot_root, weights_path, tmp_path) -> None:
    frames_dir = _copy_bus_frames(tmp_path / "frames", 12)

    completed = _run_cli(
        wavepilot_root, "--frames", str(frames_dir), "--weights", weights_path, "--bench", "--warmup", "2"
    )

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert completed.returncode == 0, completed.stderr
    assert len(stdout_lines) == 1
    summary = json.loads(stdout_lines[0])
    assert set(summary) == BENCH_SUMMARY_KEYS
    assert summary["frames_measured"] == 10
    assert summary["warmup"] == 2
    assert summary["fps_mean"] > 0
    assert summary["latency_ms_p95"] >= summary["latency_ms_mean"] > 0
    assert summary["device"] in {"mps", "cpu"}
    bus_height, bus_width = cv2.imread(str(BUS_IMAGE_PATH)).shape[:2]
    assert (summary["image_width"], summary["image_height"]) == (bus_width, bus_height)


def test_live_cli_writes_frame_json_lines(wavepilot_root, weights_path, tmp_path) -> None:
    frames_dir = _copy_bus_frames(tmp_path / "frames", 3)
    output_path = tmp_path / "out.jsonl"

    completed = _run_cli(
        wavepilot_root, "--frames", str(frames_dir), "--weights", weights_path, "--output", str(output_path)
    )

    assert completed.returncode == 0, completed.stderr
    frames = [Frame.from_dict(json.loads(line)) for line in output_path.read_text(encoding="utf-8").splitlines()]
    track_id_sets = [{d.track_id for d in frame.detections} for frame in frames]
    assert [frame.frame_id for frame in frames] == [0, 1, 2]
    assert track_id_sets[0] == track_id_sets[1] == track_id_sets[2]
    assert not [line for line in completed.stdout.splitlines() if line.startswith("{")]
