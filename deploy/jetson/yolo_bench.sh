#!/usr/bin/env bash
# Orin 에서 실행: YOLO 컨테이너 기동 → TensorRT 엔진 변환 → 벤치 1회 → 정리.
# 사용: cd ~/wavepilot && bash yolo_bench.sh   (출력 전체를 복사해 전달)
set -euo pipefail

cd ~/wavepilot
mkdir -p logs

echo "=== [1] 보드 정보 ==="
cat /etc/nv_tegra_release
if grep -q "R35" /etc/nv_tegra_release; then
  IMAGE=ultralytics/ultralytics:latest-jetson-jetpack5
else
  IMAGE=ultralytics/ultralytics:latest-jetson-jetpack6
fi
echo "image: $IMAGE"
free -h

echo "=== [2] 컨테이너 기동 (첫 pull 은 수 분) ==="
docker rm -f yolo-bench >/dev/null 2>&1 || true
docker run -d --name yolo-bench --runtime nvidia --ipc=host \
  -v ~/wavepilot:/workspace/wavepilot -w /workspace/wavepilot -e PYTHONPATH=. \
  "$IMAGE" sleep infinity
docker exec yolo-bench python3 --version
docker exec yolo-bench python -c "import ultralytics, torch; print('ultralytics', ultralytics.__version__, 'torch', torch.__version__, 'cuda', torch.cuda.is_available())"
docker exec yolo-bench python -c "import lap, yolo_detection; print('lap + repo ok')"

echo "=== [3] 테스트 이미지 300장 ==="
docker exec yolo-bench python -c "import shutil, pathlib; from ultralytics.utils import ASSETS; d = pathlib.Path('bench_frames'); d.mkdir(exist_ok=True); [shutil.copy(ASSETS / 'bus.jpg', d / f'{i:04d}.jpg') for i in range(300)]"

echo "=== [4] TensorRT 엔진 변환 (수 분) ==="
if [ ! -f models/yolo11n.engine ]; then
  time docker exec yolo-bench yolo export model=models/yolo11n.pt format=engine half=True imgsz=640 device=0
fi
ls -la models/yolo11n.engine

echo "=== [5] 벤치 ==="
docker exec yolo-bench python tools/run_yolo.py --frames bench_frames --weights models/yolo11n.engine --device 0 --bench --warmup 10 | tee logs/bench_A.json
free -h

echo "=== [6] 정리 ==="
docker rm -f yolo-bench
echo "orin yolo ok"
