"""YOLO 탐지 실행·측정 CLI — 녹화 프레임/비디오를 실제 yolo11n 으로 돌려 Frame JSON lines 또는 --bench 요약 한 줄을 낸다 (DET-01, DET-02).

실행 (저장소 루트에서):
    Mac:    uv run --group yolo python tools/run_yolo.py --frames <dir> --device mps
    벤치:   uv run --group yolo python tools/run_yolo.py --frames <dir> --bench --warmup 10
    Jetson: PYTHONPATH=. python3 tools/run_yolo.py --weights models/yolo11n.engine --frames <dir> --bench --device 0

지연은 `read_frame` 한 번의 시간이다 — 이미지 디코드, 추론, 추적, Frame 변환을 모두 포함한다.
"""

import argparse
import contextlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Callable, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # PYTHONPATH 없이 python tools/run_yolo.py 로 실행해도 interfaces·yolo_detection 을 찾게
    sys.path.insert(0, str(REPO_ROOT))

from interfaces.detection import DetectionSource, Frame
from yolo_detection.image_source import DirectoryImageSource, VideoImageSource
from yolo_detection.source import YoloDetectionSource

DEFAULT_WEIGHTS = "models/yolo11n.pt"  # cwd 에 자동 다운로드되지 않게 gitignore 된 models/ 로 고정
DEFAULT_TRACKER = "bytetrack.yaml"  # 버전마다 기본 트래커가 달라질 수 있어 명시한다
DEFAULT_FPS = 10.0  # mock 탐지 소스의 기본 fps 와 같다
DEFAULT_WARMUP_FRAMES = 10  # 첫 프레임들은 모델 로드·MPS/TensorRT 워밍업이라 평균을 왜곡한다
LATENCY_PERCENTILE = 0.95
SUMMARY_DECIMALS = 2
MS_PER_SEC = 1000.0
EXIT_NO_FRAMES = 1
EXIT_BAD_INPUT = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python tools/run_yolo.py", description="Run yolo11n + ByteTrack on recorded frames")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--frames", help="directory of .jpg/.jpeg/.png frames (sorted by name)")
    source_group.add_argument("--video", help="video file path")
    parser.add_argument("--weights", default=DEFAULT_WEIGHTS, help=f"model weights (default {DEFAULT_WEIGHTS})")
    parser.add_argument("--device", default="auto", help="auto | mps | cpu | 0 (default auto)")
    parser.add_argument("--tracker", default=DEFAULT_TRACKER, help=f"tracker config (default {DEFAULT_TRACKER})")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="timestamp rate for --frames (default 10.0)")
    parser.add_argument("--output", help="write Frame JSON lines to this file instead of stdout")
    parser.add_argument("--bench", action="store_true", help="print one JSON summary line instead of Frame lines")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_FRAMES, help="frames excluded from --bench stats (default 10)")
    return parser.parse_args(argv)


def parse_device(value: str) -> str | int | None:
    if value == "auto":
        return None
    if value.isdigit():
        return int(value)
    return value


def resolve_auto_device() -> str | int:
    # 모듈 상단에서 import 하면 torch 가 없는 base 환경에서 이 모듈 import 가 깨진다 (SC3)
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return 0
    return "cpu"


def build_image_source(args: argparse.Namespace) -> DirectoryImageSource | VideoImageSource:
    if args.frames:
        return DirectoryImageSource(args.frames, fps=args.fps)
    return VideoImageSource(args.video)


def stream_frames(
    detection_source: DetectionSource,
    write_line: Callable[[str], None] | None,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[float], Frame | None]:
    latencies: list[float] = []
    last_frame = None

    while True:
        start = clock()
        frame = detection_source.read_frame()
        elapsed = clock() - start
        if frame is None:
            break

        latencies.append(elapsed)
        if write_line is not None:
            write_line(json.dumps(frame.to_dict()))
        last_frame = frame

    return latencies, last_frame


def summarize_latencies(latencies_sec: list[float], warmup: int) -> dict:
    if warmup < 0:
        raise ValueError(f"warmup must be >= 0, got {warmup}")

    measured = latencies_sec[warmup:]
    if not measured:
        raise ValueError(f"need more than {warmup} frames for --bench, got {len(latencies_sec)}")

    mean_sec = sum(measured) / len(measured)
    p95_sec = sorted(measured)[math.ceil(LATENCY_PERCENTILE * len(measured)) - 1]

    return {
        "frames_measured": len(measured),
        "warmup": warmup,
        "fps_mean": round(1 / mean_sec, SUMMARY_DECIMALS),
        "latency_ms_mean": round(mean_sec * MS_PER_SEC, SUMMARY_DECIMALS),
        "latency_ms_p95": round(p95_sec * MS_PER_SEC, SUMMARY_DECIMALS),
    }


def build_bench_summary(
    weights: str,
    device: str | int,
    latencies_sec: list[float],
    warmup: int,
    last_frame: Frame,
) -> dict:
    return {
        "weights": weights,
        "device": str(device),
        **summarize_latencies(latencies_sec, warmup),
        "image_width": last_frame.image_width,
        "image_height": last_frame.image_height,
    }


def run_detection(
    args: argparse.Namespace,
    detection_source: DetectionSource,
    device: str | int,
    result_stream: TextIO,
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    with contextlib.ExitStack() as stack:
        # Frame 줄은 --output 파일로, 둘 다 없으면 result_stream 으로, --bench 만이면 어디에도 안 쓴다 (stdout 은 요약 한 줄)
        if args.output:
            output_file = stack.enter_context(open(args.output, "w", encoding="utf-8"))
            write_line = lambda line: output_file.write(line + "\n")
        elif not args.bench:
            write_line = lambda line: result_stream.write(line + "\n")
        else:
            write_line = None

        latencies, last_frame = stream_frames(detection_source, write_line, clock)

    if last_frame is None:
        print("no frames read", file=sys.stderr)
        return EXIT_NO_FRAMES

    if args.bench:
        try:
            summary = build_bench_summary(args.weights, device, latencies, args.warmup, last_frame)
        except ValueError as error:
            print(error, file=sys.stderr)
            return EXIT_NO_FRAMES
        result_stream.write(json.dumps(summary) + "\n")

    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    result_stream = sys.stdout

    # ultralytics LOGGER 는 import 시점의 sys.stdout 에 붙는다 — 지연 import 가 이 블록 안에서 일어나야
    # 다운로드·경고 로그가 JSON 출력 stdout 을 오염시키지 않는다
    with contextlib.redirect_stdout(sys.stderr):
        try:
            image_source = build_image_source(args)
        except (FileNotFoundError, ValueError) as error:
            print(error, file=sys.stderr)
            sys.exit(EXIT_BAD_INPUT)

        device = parse_device(args.device)
        if device is None:
            device = resolve_auto_device()

        detection_source = YoloDetectionSource(image_source, weights=args.weights, device=device, tracker=args.tracker)
        exit_code = run_detection(args, detection_source, device, result_stream)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
