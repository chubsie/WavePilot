#!/usr/bin/env bash
# 녹화된 드론 카메라 프레임에 yolo11n 을 돌리고 읽기 쉬운 로그·박스 이미지를 만든다. 사용: scripts/check_yolo_camera.sh <프레임 디렉터리> [device]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ $# -lt 1 ]; then
  echo "usage: scripts/check_yolo_camera.sh <frames_dir> [auto|mps|cpu]" >&2
  echo "프레임 녹화: ../llm-research/scripts/record_camera.sh" >&2
  exit 2
fi

FRAMES_DIR="$(cd "$1" && pwd)"
DEVICE="${2:-auto}"
OUT_DIR="$FRAMES_DIR/yolo"
mkdir -p "$OUT_DIR"

echo "[1/2] yolo11n on $FRAMES_DIR (device=$DEVICE)"
uv run --group yolo python modules/yolo_detection/run_yolo.py \
  --frames "$FRAMES_DIR" --device "$DEVICE" --output "$OUT_DIR/detections.jsonl" 2>"$OUT_DIR/run_yolo.stderr.log"

echo "[2/2] log + annotated images"
uv run --group yolo python modules/yolo_detection/show_detections.py "$OUT_DIR/detections.jsonl" \
  --frames "$FRAMES_DIR" --annotate-dir "$OUT_DIR/annotated" | tee "$OUT_DIR/detections.log"

echo
echo "raw JSON lines : $OUT_DIR/detections.jsonl"
echo "readable log   : $OUT_DIR/detections.log"
echo "boxed images   : $OUT_DIR/annotated/"
