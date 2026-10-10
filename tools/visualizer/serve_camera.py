"""호환용 shim — 시뮬레이터 서버는 modules/simulation/browser/server.py 로 옮겼다 (X-10).

실행 (저장소 루트에서):
    uv run python -m modules.simulation.browser.server   (또는 이 파일)
    실시간 YOLO + 명령 데모: uv run --group yolo python -m modules.drone.sim_demo --live
    → 브라우저(Chrome/Edge)로 http://127.0.0.1:8765/drone_camera.html 열기

프레임 디스크 녹화(recordings/)와 live_yolo.py 는 없앴다 — 프레임은 메모리 최신 슬롯으로만 흐른다.
"""

import sys
from pathlib import Path

# 파일 경로로 실행해도 modules 패키지를 찾도록 저장소 루트를 넣는다
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.simulation.browser.server import main  # noqa: E402

if __name__ == "__main__":
    main()
