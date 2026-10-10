"""드론 카메라 시뮬레이터 페이지를 띄우고, 페이지가 보낸 카메라 프레임을 받는 로컬 서버.

실행 (저장소 루트에서):
    실시간 YOLO:  uv run --group yolo python tools/visualizer/serve_camera.py --live
    녹화만:       python tools/visualizer/serve_camera.py
    → 브라우저(Chrome/Edge)로 http://127.0.0.1:8765/drone_camera.html 열기 → Record

프레임은 항상 recordings/<session>/frame_NNNNN.jpg 로 저장한다.
--live 이면 받는 즉시 yolo11n 으로 분석해 이 터미널에 프레임마다 탐지 줄을 찍고,
recordings/<session>/yolo/detections.log 에도 남긴다 (live_yolo.py).
--live 없이 녹화만 했다면 나중에 scripts/check_yolo_camera.sh recordings/<session> 로 같은 로그를 만든다.

녹화만 할 때는 표준 라이브러리만 쓴다. 루프백에만 바인딩한다 — 로컬 테스트 전용이고 인증이 없다.
glTF 모델은 페이지가 file:// 에서는 불러오지 못하므로(CORS) 이 서버가 페이지도 함께 서비스한다.
"""

import argparse
import functools
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VISUALIZER_DIR = Path(__file__).resolve().parent
REPO_ROOT = VISUALIZER_DIR.parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "recordings"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_FRAME_BYTES = 10 * 1024 * 1024  # 1280x720 jpg 는 수백 KB — 이보다 크면 잘못된 요청
MAX_FRAME_INDEX = 99_999  # frame_NNNNN 다섯 자리
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # 경로 탈출(../) 을 막는다
RECORD_FPS = 10.0  # drone_camera.html 의 RECORD_FPS 와 같아야 timestamp 가 맞는다


class CameraRequestHandler(SimpleHTTPRequestHandler):
    out_dir: Path = DEFAULT_OUT_DIR
    live_detector = None  # --live 일 때 LiveYoloDetector

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        session = _get_single(query, "session")
        if session is None or not SESSION_PATTERN.match(session):
            self.send_error(HTTPStatus.BAD_REQUEST, "session must match [A-Za-z0-9_-]{1,64}")
            return
        session_dir = self.out_dir / session

        if url.path == "/frames":
            self._save_frame(session_dir, query)
        elif url.path == "/frames/done":
            self._finish_session(session_dir)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _save_frame(self, session_dir: Path, query: dict) -> None:
        index_text = _get_single(query, "index")
        if index_text is None or not index_text.isdigit() or int(index_text) > MAX_FRAME_INDEX:
            self.send_error(HTTPStatus.BAD_REQUEST, f"index must be 0..{MAX_FRAME_INDEX}")
            return

        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_FRAME_BYTES:
            self.send_error(HTTPStatus.BAD_REQUEST, f"body must be 1..{MAX_FRAME_BYTES} bytes")
            return

        index = int(index_text)
        jpeg_bytes = self.rfile.read(length)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / f"frame_{index:05d}.jpg").write_bytes(jpeg_bytes)
        if self.live_detector is not None:
            self.live_detector.submit_frame(session_dir, index, jpeg_bytes)
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _finish_session(self, session_dir: Path) -> None:
        frame_count = len(list(session_dir.glob("frame_*.jpg"))) if session_dir.is_dir() else 0
        print(f"recording done: {frame_count} frames -> {session_dir}", flush=True)
        if self.live_detector is not None:
            self.live_detector.finish_session(session_dir)
        else:
            print(f"next: scripts/check_yolo_camera.sh {session_dir}", flush=True)
        self._send_empty(HTTPStatus.NO_CONTENT)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        # 프레임 POST 가 초당 10줄씩 찍혀 완료 메시지를 덮지 않게, 오류 응답만 남긴다
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)


def _get_single(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the drone camera page and save posted frames")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="directory for recording sessions")
    parser.add_argument("--live", action="store_true", help="run yolo11n on frames as they arrive (needs uv --group yolo)")
    parser.add_argument("--device", default="auto", help="--live device: auto | mps | cpu | 0 (default auto)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    CameraRequestHandler.out_dir = Path(args.out).resolve()
    if args.live:
        # ultralytics·cv2 는 yolo 그룹에만 있다 — 녹화만 할 때는 import 하지 않는다
        from live_yolo import LiveYoloDetector
        CameraRequestHandler.live_detector = LiveYoloDetector(fps=RECORD_FPS, device=args.device)
    handler = functools.partial(CameraRequestHandler, directory=str(VISUALIZER_DIR))

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"open http://{args.host}:{args.port}/drone_camera.html (Chrome/Edge)", flush=True)
    print(f"frames -> {CameraRequestHandler.out_dir}/<session>/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
