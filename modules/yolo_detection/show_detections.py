"""YOLO Frame JSON lines 를 사람이 읽는 로그로 보여준다 — 프레임별 탐지 줄 + track 요약 (DET-01 확인용).

실행 (저장소 루트에서):
    python modules/yolo_detection/show_detections.py detections.jsonl
    uv run --group yolo python modules/yolo_detection/show_detections.py detections.jsonl \
        --frames <녹화 디렉터리> --annotate-dir <출력 디렉터리>

로그 출력은 표준 라이브러리만 쓴다. --annotate-dir 를 줄 때만 opencv(yolo 그룹)로 박스를 그린 이미지를 저장한다.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # PYTHONPATH 없이 python modules/yolo_detection/show_detections.py 로 실행해도 modules 패키지를 찾게
    sys.path.insert(0, str(REPO_ROOT))

from modules.yolo_detection.image_source import IMAGE_SUFFIXES
from modules.yolo_detection.interface.detection import Detection, Frame

BOX_COLOR_BGR = (0, 255, 0)
BOX_THICKNESS_PX = 2
LABEL_FONT_SCALE = 0.6
LABEL_OFFSET_PX = 6


@dataclass
class TrackSummary:
    class_name: str
    first_seen_sec: float
    last_seen_sec: float
    frame_count: int
    confidence_sum: float

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.frame_count


def read_frames(lines: list[str]) -> list[Frame]:
    return [Frame.from_dict(json.loads(line)) for line in lines if line.strip()]


def format_detection(detection: Detection) -> str:
    x1, y1, x2, y2 = detection.xyxy
    track = "-" if detection.track_id is None else f"#{detection.track_id}"
    return (
        f"{track} {detection.class_name} {detection.confidence:.2f} "
        f"box=({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f})"
    )


def format_frame_line(frame: Frame) -> str:
    header = f"frame {frame.frame_id:>4} t={frame.timestamp_sec:6.2f}s  {len(frame.detections)} det"
    if not frame.detections:
        return header
    return header + "  | " + "; ".join(format_detection(d) for d in frame.detections)


def summarize_tracks(frames: list[Frame]) -> dict[int, TrackSummary]:
    tracks: dict[int, TrackSummary] = {}
    for frame in frames:
        for detection in frame.detections:
            if detection.track_id is None:
                continue
            track = tracks.get(detection.track_id)
            if track is None:
                tracks[detection.track_id] = TrackSummary(
                    class_name=detection.class_name,
                    first_seen_sec=frame.timestamp_sec,
                    last_seen_sec=frame.timestamp_sec,
                    frame_count=1,
                    confidence_sum=detection.confidence,
                )
                continue
            track.last_seen_sec = frame.timestamp_sec
            track.frame_count += 1
            track.confidence_sum += detection.confidence
    return tracks


def format_summary_lines(frames: list[Frame]) -> list[str]:
    frames_with_detections = sum(1 for frame in frames if frame.detections)
    lines = [f"summary: {len(frames)} frames, {frames_with_detections} with detections"]

    tracks = summarize_tracks(frames)
    for track_id, track in sorted(tracks.items()):
        lines.append(
            f"  track #{track_id} {track.class_name}: {track.frame_count}/{len(frames)} frames, "
            f"t={track.first_seen_sec:.2f}-{track.last_seen_sec:.2f}s, mean conf {track.mean_confidence:.2f}"
        )
    return lines


def save_annotated_images(frames: list[Frame], frames_dir: Path, annotate_dir: Path) -> int:
    # opencv 는 yolo 그룹에만 있다 — 로그만 볼 때는 import 하지 않는다
    import cv2

    # run_yolo.py --frames 와 같은 정렬 규칙이라 frame_id 가 곧 파일 순번이다
    image_paths = sorted(p for p in frames_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    annotate_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for frame in frames:
        if frame.frame_id >= len(image_paths):
            break
        image_path = image_paths[frame.frame_id]
        image = cv2.imread(str(image_path))
        for detection in frame.detections:
            _draw_detection(cv2, image, detection)
        cv2.imwrite(str(annotate_dir / image_path.name), image)
        saved_count += 1
    return saved_count


def _draw_detection(cv2, image, detection: Detection) -> None:
    x1, y1, x2, y2 = (round(value) for value in detection.xyxy)
    cv2.rectangle(image, (x1, y1), (x2, y2), BOX_COLOR_BGR, BOX_THICKNESS_PX)

    label = f"#{detection.track_id} {detection.class_name} {detection.confidence:.2f}"
    label_y = max(y1 - LABEL_OFFSET_PX, LABEL_OFFSET_PX * 2)
    cv2.putText(image, label, (x1, label_y), cv2.FONT_HERSHEY_SIMPLEX, LABEL_FONT_SCALE, BOX_COLOR_BGR, BOX_THICKNESS_PX)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python modules/yolo_detection/show_detections.py", description="Show YOLO Frame JSON lines as a readable log")
    parser.add_argument("detections", help="Frame JSON lines file from run_yolo.py (- for stdin)")
    parser.add_argument("--frames", help="source image directory (needed for --annotate-dir)")
    parser.add_argument("--annotate-dir", help="write images with boxes drawn to this directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.annotate_dir and not args.frames:
        sys.exit("--annotate-dir needs --frames")

    if args.detections == "-":
        lines = sys.stdin.readlines()
    else:
        lines = Path(args.detections).read_text(encoding="utf-8").splitlines()
    frames = read_frames(lines)

    for frame in frames:
        print(format_frame_line(frame))
    print()
    for line in format_summary_lines(frames):
        print(line)

    if args.annotate_dir:
        saved_count = save_annotated_images(frames, Path(args.frames), Path(args.annotate_dir))
        print(f"annotated {saved_count} images -> {args.annotate_dir}")


if __name__ == "__main__":
    main()
