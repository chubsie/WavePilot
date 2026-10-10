"""브라우저 시뮬레이터 HTTP 서버 — 고정 와이어 프로토콜을 SimBridge 호출로 바꾼다 (SIM-02, X-01).

실행 (저장소 루트에서):
    uv run python -m modules.simulation.browser.server
    → 브라우저(Chrome/Edge)로 http://127.0.0.1:8765/drone_camera.html 열기

와이어 프로토콜:
1. POST /sim/frame?seq=<0..2^31>  — body image/jpeg, 헤더 X-Telemetry(Telemetry JSON) → 204
2. POST /sim/telemetry            — body JSON {telemetry, ai_mode, events} → 200 {commands, detections, status}
3. GET  /<file>                   — tools/visualizer/ 정적 파일 (페이지 /drone_camera.html)

잘못된 요청은 400 으로 거절하고 bridge 를 건드리지 않는다. 본문은 길이를 먼저 검증한 뒤에만 읽는다.
표준 라이브러리만 쓴다. 기본은 루프백 바인딩이고 인증은 없다(로컬 시뮬레이터 전용).
실시간 YOLO 조립은 컴포지션 루트(modules/drone/sim_demo.py --live) 몫이라 여기에는 없다.
"""

import argparse
import functools
import json
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import BrowserFrame, SimBridge

VISUALIZER_DIR = Path(__file__).resolve().parents[3] / "tools" / "visualizer"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MAX_FRAME_BYTES = 10 * 1024 * 1024  # 1280x720 jpg 는 수백 KB — 이보다 크면 잘못된 요청
MAX_TELEMETRY_BYTES = 64 * 1024
MAX_TELEMETRY_HEADER_BYTES = 8 * 1024
MAX_FRAME_SEQ = 2**31
MAX_EVENTS_PER_POST = 32

FRAME_PATH = "/sim/frame"
TELEMETRY_PATH = "/sim/telemetry"
GOAL_REACHED_EVENT = "goal_reached"


class SimRequestHandler(SimpleHTTPRequestHandler):
    """POST 는 와이어 프로토콜, GET 은 SimpleHTTPRequestHandler 그대로(directory= 안으로 한정)."""

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == FRAME_PATH:
            self._handle_frame()
        elif path == TELEMETRY_PATH:
            self._handle_telemetry()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_frame(self) -> None:
        seq_text = _get_single(parse_qs(urlparse(self.path).query), "seq")
        if seq_text is None or not seq_text.isdigit() or int(seq_text) > MAX_FRAME_SEQ:
            self.send_error(HTTPStatus.BAD_REQUEST, f"seq must be 0..{MAX_FRAME_SEQ}")
            return

        telemetry = self._parse_telemetry_header()
        if telemetry is None:
            return

        body = self._read_body(MAX_FRAME_BYTES, "image/jpeg")
        if body is None:
            return

        frame = BrowserFrame(
            seq=int(seq_text),
            timestamp_sec=telemetry.time_sec,
            jpeg_bytes=body,
            telemetry=telemetry,
            received_at_sec=time.monotonic(),  # SimBridge 기본 clock 과 같은 시계
        )
        self.server.sim_bridge.put_frame(frame)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _parse_telemetry_header(self) -> Telemetry | None:
        header = self.headers.get("X-Telemetry")
        if header is None or len(header) > MAX_TELEMETRY_HEADER_BYTES:
            self.send_error(HTTPStatus.BAD_REQUEST, f"X-Telemetry must be 1..{MAX_TELEMETRY_HEADER_BYTES} bytes")
            return None

        try:
            return Telemetry.from_dict(json.loads(header))
        except (ValueError, TypeError) as error:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid X-Telemetry: {error}")
            return None

    def _handle_telemetry(self) -> None:
        body = self._read_body(MAX_TELEMETRY_BYTES, "application/json")
        if body is None:
            return

        try:
            telemetry, ai_mode, events = parse_telemetry_post(json.loads(body))
        except ValueError as error:  # JSONDecodeError·UnicodeDecodeError 도 ValueError
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid telemetry post: {error}")
            return

        bridge = self.server.sim_bridge
        bridge.update_from_browser(telemetry, ai_mode, events)
        response = {
            "commands": bridge.drain_commands(),
            "detections": bridge.latest_detections(),
            "status": bridge.status(),
        }

        payload = json.dumps(response).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self, max_bytes: int, content_type: str) -> bytes | None:
        """길이·타입을 먼저 검증하고 정확히 length 바이트만 읽는다. 실패하면 400 을 보내고 None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= max_bytes:
            self.send_error(HTTPStatus.BAD_REQUEST, f"body must be 1..{max_bytes} bytes")
            return None

        received_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if received_type != content_type:
            self.send_error(HTTPStatus.BAD_REQUEST, f"Content-Type must be {content_type}")
            return None

        return self.rfile.read(length)

    def log_message(self, format: str, *args) -> None:
        # 프레임·텔레메트리 POST 가 초당 20줄씩 찍히지 않게, 오류 응답만 남긴다
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)


def parse_telemetry_post(payload: object) -> tuple[Telemetry, bool, list[dict]]:
    """POST /sim/telemetry 본문을 검증한다. 잘못되면 ValueError — 호출자는 bridge 를 건드리지 않는다."""
    if not isinstance(payload, dict):
        raise ValueError(f"body must be a JSON object, got {type(payload).__name__}")

    if "telemetry" not in payload:
        raise ValueError("missing field 'telemetry'")
    telemetry = Telemetry.from_dict(payload["telemetry"])

    ai_mode = payload.get("ai_mode")
    if not isinstance(ai_mode, bool):
        raise ValueError(f"field 'ai_mode' must be a bool, got {ai_mode!r}")

    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list) or len(raw_events) > MAX_EVENTS_PER_POST:
        raise ValueError(f"field 'events' must be a list of at most {MAX_EVENTS_PER_POST}")
    events = [_parse_event(event) for event in raw_events]

    return telemetry, ai_mode, events


def _parse_event(event: object) -> dict:
    if not isinstance(event, dict) or event.get("kind") != GOAL_REACHED_EVENT:
        raise ValueError(f"event must be {{'kind': '{GOAL_REACHED_EVENT}', ...}}, got {event!r}")

    command_id = event.get("command_id")
    if not isinstance(command_id, int) or isinstance(command_id, bool):
        raise ValueError(f"event 'command_id' must be an int, got {command_id!r}")

    time_sec = event.get("time_sec")
    if not isinstance(time_sec, (int, float)) or isinstance(time_sec, bool):
        raise ValueError(f"event 'time_sec' must be a number, got {time_sec!r}")

    # 화이트리스트한 키만 남긴다
    return {"kind": GOAL_REACHED_EVENT, "command_id": command_id, "time_sec": float(time_sec)}


def _get_single(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def start_sim_server(bridge: SimBridge, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """백그라운드 데몬 스레드에서 서버를 띄운다. port=0 이면 OS 가 빈 포트를 고른다(server.server_address)."""
    handler = functools.partial(SimRequestHandler, directory=str(VISUALIZER_DIR))

    server = ThreadingHTTPServer((host, port), handler)
    server.sim_bridge = bridge  # 핸들러는 self.server.sim_bridge 로 읽는다 — 클래스 상태 없음
    server.daemon_threads = True

    threading.Thread(target=server.serve_forever, daemon=True, name="sim-http").start()
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the browser drone simulator and its wire protocol")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    bridge = SimBridge()
    server = start_sim_server(bridge, args.host, args.port)
    print(f"open http://{args.host}:{args.port}/drone_camera.html (Chrome/Edge)", flush=True)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
