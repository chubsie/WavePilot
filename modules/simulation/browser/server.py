"""브라우저 시뮬레이터 HTTP 서버 — 고정 와이어 프로토콜을 SimBridge 호출로 바꾼다 (SIM-02, X-01).

실행 (저장소 루트에서):
    uv run python -m modules.simulation.browser.server
    → 브라우저(Chrome/Edge)로 http://127.0.0.1:8765/drone_camera.html 열기

와이어 프로토콜:
1. POST /sim/frame?seq=<0..2^31>  — body image/jpeg, 헤더 X-Telemetry(Telemetry JSON) → 204
2. POST /sim/telemetry            — body JSON {telemetry, ai_mode, events, operator_link?} → 200 {commands, detections, status, mission, pipeline}
                                     detections 에는 추론한 프레임의 seq 가, pipeline 에는 최신 {state, report} 가 실린다(--operator browser)
                                     events 의 kind 는 goal_reached | heart_shot (heart_shot 은 Phase 6 D-10 발사 판정)
3. GET  /sim/scenario?id=<id>     — 200 브라우저 시나리오 dict (Phase 6 C1) / 400 잘못된 id·파일 / 404 파일 없음
4. GET  /<file>                   — tools/visualizer/ 정적 파일 (페이지 /drone_camera.html)
5. GET  /mission_demo/<file>      — tools/mission_demo/ 정적 파일, /visualizer/<file> — tools/visualizer/ (mission_demo 가 ../visualizer/ 를 읽는다)

잘못된 요청은 400 으로 거절하고 bridge 를 건드리지 않는다. 본문은 길이를 먼저 검증한 뒤에만 읽는다.
표준 라이브러리만 쓴다. 기본은 루프백 바인딩이고 인증은 없다(로컬 시뮬레이터 전용).
실시간 YOLO 조립은 컴포지션 루트(modules/drone/sim_demo.py --live) 몫이라 여기에는 없다.
"""

import argparse
import functools
import json
import posixpath
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import HEART_SHOT_EVENT, BrowserFrame, SimBridge

TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"
VISUALIZER_DIR = TOOLS_DIR / "visualizer"
MISSION_DEMO_DIR = TOOLS_DIR / "mission_demo"
# URL 접두사 → 디렉터리. 접두사가 없으면 기존처럼 VISUALIZER_DIR 루트다
STATIC_MOUNTS = (
    ("/mission_demo", MISSION_DEMO_DIR),
    ("/visualizer", VISUALIZER_DIR),
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MAX_FRAME_BYTES = 10 * 1024 * 1024  # 1280x720 jpg 는 수백 KB — 이보다 크면 잘못된 요청
MAX_TELEMETRY_BYTES = 64 * 1024
MAX_TELEMETRY_HEADER_BYTES = 8 * 1024
MAX_FRAME_SEQ = 2**31
MAX_EVENTS_PER_POST = 32  # sim_link.js MAX_EVENTS_PER_POST 와 같은 값
MAX_ACTOR_ID_CHARS = 32

FRAME_PATH = "/sim/frame"
TELEMETRY_PATH = "/sim/telemetry"
SCENARIO_PATH = "/sim/scenario"
GOAL_REACHED_EVENT = "goal_reached"


class SimRequestHandler(SimpleHTTPRequestHandler):
    """POST 와 GET /sim/scenario 는 와이어 프로토콜, 나머지 GET 은 SimpleHTTPRequestHandler 그대로(마운트 디렉터리 안으로 한정)."""

    # mimetypes 가 플랫폼마다 달라 module 스크립트·GLB 가 octet-stream 으로 나가지 않게 고정한다
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
    }

    def do_GET(self) -> None:
        if urlparse(self.path).path == SCENARIO_PATH:
            self._handle_scenario()
        else:
            super().do_GET()

    def end_headers(self) -> None:
        # 데모 파일을 고친 뒤 새로고침만으로 반영되게 한다 — 브라우저가 옛 app.js 를 휴리스틱 캐시로 쓰지 않게
        if self.command == "GET" and not self.path.startswith("/sim/"):  # /sim/* 응답은 _send_json 이 no-store 를 붙인다
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        """정규화한 경로가 STATIC_MOUNTS 접두사로 시작하면 그 디렉터리를 루트로 삼는다.

        정규화(unquote → normpath)를 먼저 하므로 %2e%2e 나 /mission_demo/../.. 로는 마운트를 바꿔 빠져나갈 수 없고,
        남은 경로는 표준 translate_path 가 다시 '..' 를 버리며 directory 안으로 가둔다.
        """
        url_path = unquote(urlsplit(path).path)
        normalized = posixpath.normpath(url_path)

        for prefix, directory in STATIC_MOUNTS:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                remainder = normalized.removeprefix(prefix) or "/"
                if url_path.endswith("/") and not remainder.endswith("/"):
                    remainder += "/"  # 디렉터리 index 판정이 끝 슬래시를 본다
                self.directory = str(directory)
                return super().translate_path(quote(remainder))

        return super().translate_path(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == FRAME_PATH:
            self._handle_frame()
        elif path == TELEMETRY_PATH:
            self._handle_telemetry()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_scenario(self) -> None:
        scenario_id = _get_single(parse_qs(urlparse(self.path).query), "id")
        if scenario_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "id is required")
            return

        # 지연 import — compare_scenario(06-01)가 없어도 서버·기존 테스트가 import 되게
        from modules.simulation.compare_scenario import (
            SCENARIOS_DIR,
            load_compare_scenario,
            scenario_to_browser_dict,
        )

        scenarios_dir = self.server.scenarios_dir or SCENARIOS_DIR
        try:
            scenario = load_compare_scenario(scenario_id, scenarios_dir)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, f"scenario not found: {scenario_id}")
            return
        except ValueError as error:  # id 패턴 불일치(경로 조작 방지)·TOML 문법·필드 오류
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid scenario: {error}")
            return

        self._send_json(scenario_to_browser_dict(scenario))

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
            telemetry, ai_mode, events, operator_link = parse_telemetry_post(json.loads(body))
        except ValueError as error:  # JSONDecodeError·UnicodeDecodeError 도 ValueError
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid telemetry post: {error}")
            return

        bridge = self.server.sim_bridge
        bridge.update_from_browser(telemetry, ai_mode, events, operator_link)
        response = {
            "commands": bridge.drain_commands(),
            "detections": bridge.latest_browser_detections(),
            "status": bridge.status(),
            "mission": bridge.mission_status(),
            "pipeline": bridge.pipeline_messages(),
            "detection_log": bridge.recent_detections(),
        }
        self._send_json(response)

    def _send_json(self, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
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


def parse_telemetry_post(payload: object) -> tuple[Telemetry, bool, list[dict], bool]:
    """POST /sim/telemetry 본문을 검증한다. 잘못되면 ValueError — 호출자는 bridge 를 건드리지 않는다.

    네 번째 값은 운용자 링크 heartbeat(operator_link, 선택 필드 — 없으면 True, Phase 5 D-01).
    """
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

    operator_link = payload.get("operator_link", True)
    if not isinstance(operator_link, bool):
        raise ValueError(f"field 'operator_link' must be a bool, got {operator_link!r}")

    return telemetry, ai_mode, events, operator_link


def _parse_event(event: object) -> dict:
    kind = event.get("kind") if isinstance(event, dict) else None

    if kind == GOAL_REACHED_EVENT:
        return _parse_goal_reached(event)
    if kind == HEART_SHOT_EVENT:
        return _parse_heart_shot(event)
    raise ValueError(f"event kind must be '{GOAL_REACHED_EVENT}' or '{HEART_SHOT_EVENT}', got {event!r}")


def _parse_goal_reached(event: dict) -> dict:
    command_id = event.get("command_id")
    if not isinstance(command_id, int) or isinstance(command_id, bool):
        raise ValueError(f"event 'command_id' must be an int, got {command_id!r}")

    time_sec = event.get("time_sec")
    if not isinstance(time_sec, (int, float)) or isinstance(time_sec, bool):
        raise ValueError(f"event 'time_sec' must be a number, got {time_sec!r}")

    # 화이트리스트한 키만 남긴다
    return {"kind": GOAL_REACHED_EVENT, "command_id": command_id, "time_sec": float(time_sec)}


def _parse_heart_shot(event: dict) -> dict:
    """발 1개 = 이벤트 1개. actor_id 는 화면 중앙 광선에 맞은 배우 id, 빗나가면 None (Phase 6 D-10)."""
    track_id = event.get("track_id")
    if not isinstance(track_id, int) or isinstance(track_id, bool):
        raise ValueError(f"event 'track_id' must be an int, got {track_id!r}")

    time_sec = event.get("time_sec")
    if not isinstance(time_sec, (int, float)) or isinstance(time_sec, bool):
        raise ValueError(f"event 'time_sec' must be a number, got {time_sec!r}")

    actor_id = event.get("actor_id")
    is_valid_actor_id = isinstance(actor_id, str) and 1 <= len(actor_id) <= MAX_ACTOR_ID_CHARS
    if actor_id is not None and not is_valid_actor_id:
        raise ValueError(f"event 'actor_id' must be null or a 1..{MAX_ACTOR_ID_CHARS} char string, got {actor_id!r}")

    # 화이트리스트한 키만 남긴다
    return {"kind": HEART_SHOT_EVENT, "track_id": track_id, "time_sec": float(time_sec), "actor_id": actor_id}


def _get_single(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def start_sim_server(
    bridge: SimBridge,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    scenarios_dir: Path | None = None,
) -> ThreadingHTTPServer:
    """백그라운드 데몬 스레드에서 서버를 띄운다. port=0 이면 OS 가 빈 포트를 고른다(server.server_address).

    scenarios_dir 이 None 이면 GET /sim/scenario 는 compare_scenario.SCENARIOS_DIR 를 쓴다(테스트만 tmp 경로를 넘긴다).
    """
    handler = functools.partial(SimRequestHandler, directory=str(VISUALIZER_DIR))

    server = ThreadingHTTPServer((host, port), handler)
    server.sim_bridge = bridge  # 핸들러는 self.server.sim_bridge 로 읽는다 — 클래스 상태 없음
    server.scenarios_dir = scenarios_dir
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
