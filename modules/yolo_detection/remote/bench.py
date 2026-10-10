"""시뮬레이터 없이 Orin YOLO 서버 지연 분포를 잰다 (SIM-05, D-04).

실행 (저장소 루트에서, yolo 그룹 없이):
    python -m modules.yolo_detection.remote.bench --url http://<orin-ip>:8082 --images <dir> [--count N]

디렉터리의 *.jpg / *.jpeg 를 이름 순으로 보낸다. --count 가 파일 수보다 크면 파일을 돌려 가며 N 장을 채운다.
출력은 RemoteYoloDetectionSource 가 로그로 남기는 것과 같은 total/orin/net p50·p95·max 표다.
"""

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from itertools import cycle, islice
from pathlib import Path

from modules.yolo_detection.remote.client import DEFAULT_TIMEOUT_SEC, RemoteYoloDetectionSource, RemoteYoloError
from modules.yolo_detection.remote.latency import format_latency_table
from modules.yolo_detection.remote.protocol import DEFAULT_PORT

DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
JPEG_SUFFIXES = (".jpg", ".jpeg")
FRAME_INTERVAL_SEC = 0.1  # 서로 다르고 소수 셋째 자리로 깔끔히 반올림되는 timestamp


def _parse_positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m modules.yolo_detection.remote.bench", description="Measure Orin YOLO latency")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"YOLO server URL (default {DEFAULT_URL})")
    parser.add_argument("--images", required=True, type=Path, help="directory of .jpg/.jpeg files")
    parser.add_argument("--count", type=_parse_positive_int, help="frames to send (default: every file once)")
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC, help=f"per-request timeout (default {DEFAULT_TIMEOUT_SEC})")
    parser.add_argument("--log-level", default="WARNING", help="logging level (default WARNING)")
    return parser.parse_args(argv)


def get_jpeg_paths(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in JPEG_SUFFIXES)


def iter_jpeg_items(paths: Sequence[Path], count: int) -> Iterator[tuple[float, bytes]]:
    for index, path in enumerate(islice(cycle(paths), count)):
        yield index * FRAME_INTERVAL_SEC, path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    paths = get_jpeg_paths(args.images) if args.images.is_dir() else []
    if not paths:
        print(f"no .jpg/.jpeg files in {args.images}", file=sys.stderr)
        return 2

    count = args.count if args.count is not None else len(paths)
    try:
        source = RemoteYoloDetectionSource(iter_jpeg_items(paths, count), args.url, timeout_sec=args.timeout_sec)
    except RemoteYoloError as error:
        print(error, file=sys.stderr)
        return 1

    exit_code = 0
    try:
        while source.read_frame() is not None:
            pass
    except RemoteYoloError as error:
        print(error, file=sys.stderr)
        exit_code = 1

    for line in format_latency_table(source.latencies, source.failure_count):
        print(line)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
