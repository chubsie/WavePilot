"""Orin YOLO HTTP 서버 — 요청 하나의 JPEG 를 Frame 하나로 바꾼다 (SIM-05, D-02).

실행 (저장소 루트에서, Jetson 컨테이너 안):
    python -m modules.yolo_detection.remote.server --weights models/yolo11n.engine --host 0.0.0.0 --port 8082 --device 0 --imgsz 640

와이어 프로토콜은 protocol.py 에 있다 (/v1/detect, /v1/reset, /health).
일부러 단일 스레드 HTTPServer 다 — ByteTrack 상태가 모델 객체에 붙어 있어 요청을 순서대로 처리해야 한다.
모델은 로드·워밍업을 마친 뒤에만 리슨하므로 /health 가 200 이면 바로 추론할 수 있다.
인증은 없다 — 노출 범위는 바인딩 주소와 ufw 로 제한한다 (D-06, hackathon 정책).
ultralytics·numpy 는 함수 안에서만 지연 import 한다 — base 환경에서도 이 모듈을 import 할 수 있다.
"""

import argparse
import json
import logging
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from modules.yolo_detection.interface.detection import Frame
from modules.yolo_detection.jpeg_source import JpegDecoder, decode_jpeg_bgr
from modules.yolo_detection.remote.protocol import (
    DEFAULT_PORT,
    DETECT_PATH,
    HEALTH_PATH,
    JSON_CONTENT_TYPE,
    MAX_JPEG_BYTES,
    RESET_PATH,
    TIMESTAMP_HEADER,
    build_detect_response,
    build_error_response,
    parse_timestamp_header,
)
from modules.yolo_detection.run_yolo import parse_device, resolve_auto_device
from modules.yolo_detection.source import DEFAULT_IMAGE_SIZE, YoloDetectionSource

DEFAULT_WEIGHTS = "models/yolo11n.engine"  # Orin 은 TensorRT 엔진 (YOLO-RUNBOOK 6단계에서 export)
DEFAULT_HOST = "0.0.0.0"
MS_PER_SEC = 1000.0

logger = logging.getLogger(__name__)


class ImageSlot:
    """한 칸짜리 push 이터레이터 — 요청마다 이미지 하나를 넣고 YoloDetectionSource.read_frame 이 꺼낸다.

    read_frame 은 매번 next(it, None) 을 부르므로, 비었다가 다시 채워져도 계속 쓸 수 있다.
    """

    def __init__(self) -> None:
        self._item: tuple[float, Any] | None = None

    def put(self, item: tuple[float, Any]) -> None:
        self._item = item

    def __iter__(self) -> "ImageSlot":
        return self

    def __next__(self) -> tuple[float, Any]:
        if self._item is None:
            raise StopIteration
        item, self._item = self._item, None
        return item


class InvalidImageError(ValueError):
    """JPEG 를 디코드하지 못했다 — 클라이언트 잘못이라 422 로 답한다."""


class YoloDetectionService:
    """모델 하나를 모든 요청이 공유한다 — persist=True 트래킹이 요청 사이로 이어진다."""

    def __init__(
        self,
        model: Any,
        decode: JpegDecoder = decode_jpeg_bgr,
        weights: str = DEFAULT_WEIGHTS,
        device: str | int | None = None,
        imgsz: int = DEFAULT_IMAGE_SIZE,
    ) -> None:
        self._model = model
        self._decode = decode
        self._weights = weights
        self._device = device
        self._imgsz = imgsz
        self._slot = ImageSlot()
        self._source = self._create_source()
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        """워밍업을 뺀, 프로세스 시작 이후 처리한 detect 요청 수 (reset 해도 유지)."""
        return self._frame_count

    def warmup(self, image: Any) -> None:
        # 첫 추론은 엔진 로드·CUDA 초기화로 느리다 — 리슨 전에 한 번 돌리고 트래커는 비운다
        self._read_image(0.0, image)
        self.reset()

    def detect(self, timestamp_sec: float, jpeg_bytes: bytes) -> dict:
        started_at = time.perf_counter()

        image = self._decode_image(jpeg_bytes)
        decoded_at = time.perf_counter()

        frame = self._read_image(timestamp_sec, image)
        inferred_at = time.perf_counter()
        self._frame_count += 1

        return build_detect_response(
            frame.to_dict(),
            decode_ms=(decoded_at - started_at) * MS_PER_SEC,
            infer_ms=(inferred_at - decoded_at) * MS_PER_SEC,
            total_ms=(time.perf_counter() - started_at) * MS_PER_SEC,
        )

    def reset(self) -> None:
        # ultralytics 는 predictor.trackers 에 BYTETracker 를 둔다 — 모델을 새로 만들지 않고 상태만 비운다
        predictor = getattr(self._model, "predictor", None)
        for tracker in getattr(predictor, "trackers", None) or []:
            tracker.reset()
        self._source = self._create_source()

    def describe_health(self) -> dict:
        return {
            "status": "ok",
            "weights": self._weights,
            "device": str(self._device),
            "frames": self._frame_count,
        }

    def _create_source(self) -> YoloDetectionSource:
        return YoloDetectionSource(self._slot, model=self._model, device=self._device, imgsz=self._imgsz)

    def _decode_image(self, jpeg_bytes: bytes) -> Any:
        try:
            image = self._decode(jpeg_bytes)
        except Exception as error:
            raise InvalidImageError(f"cannot decode JPEG: {error}") from error

        if image is None:
            raise InvalidImageError("cannot decode JPEG")
        return image

    def _read_image(self, timestamp_sec: float, image: Any) -> Frame:
        self._slot.put((timestamp_sec, image))
        frame = self._source.read_frame()
        if frame is None:
            raise RuntimeError("detection source returned no frame for a filled slot")
        return frame


def create_blank_image(imgsz: int) -> Any:
    # 모듈 상단에서 import 하면 yolo 그룹이 없는 base 환경에서 이 모듈 import 가 깨진다
    import numpy as np

    return np.zeros((imgsz, imgsz, 3), np.uint8)


class YoloRequestHandler(BaseHTTPRequestHandler):
    server: "YoloHTTPServer"

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == HEALTH_PATH:
            self._send_json(HTTPStatus.OK, self.server.yolo_service.describe_health())
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown path {path}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == DETECT_PATH:
            self._handle_detect()
        elif path == RESET_PATH:
            self.server.yolo_service.reset()
            self._send_json(HTTPStatus.OK, {"status": "reset"})
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown path {path}")

    def log_message(self, format: str, *args: Any) -> None:
        # 프레임마다 stderr 한 줄은 너무 많다 — 필요할 때 DEBUG 로 켠다
        logger.debug("%s - %s", self.address_string(), format % args)

    def _handle_detect(self) -> None:
        try:
            timestamp_sec = parse_timestamp_header(self.headers.get(TIMESTAMP_HEADER))
            content_length = self._parse_content_length()
        except ValueError as error:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
            return

        jpeg_bytes = self.rfile.read(content_length)

        try:
            response = self.server.yolo_service.detect(timestamp_sec, jpeg_bytes)
        except InvalidImageError as error:
            self._send_error_json(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))
            return
        except Exception as error:
            logger.exception("detect failed")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"detect failed: {error}")
            return

        self._send_json(HTTPStatus.OK, response)

    def _parse_content_length(self) -> int:
        # 본문은 길이를 먼저 검증한 뒤에만 읽는다 — 너무 큰 본문은 아예 읽지 않는다
        text = self.headers.get("Content-Length")
        if text is None or not text.isdigit():
            raise ValueError("missing or invalid Content-Length")

        content_length = int(text)
        if content_length == 0:
            raise ValueError("empty body")
        if content_length > MAX_JPEG_BYTES:
            raise ValueError(f"body too large: {content_length} > {MAX_JPEG_BYTES} bytes")
        return content_length

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, build_error_response(message))

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class YoloHTTPServer(HTTPServer):
    yolo_service: YoloDetectionService


def create_yolo_server(service: YoloDetectionService, host: str, port: int) -> HTTPServer:
    """바인딩만 한다(port 0 허용). 워밍업은 호출자 몫 — 테스트가 순서를 정할 수 있게."""
    server = YoloHTTPServer((host, port), YoloRequestHandler)
    server.yolo_service = service
    return server


def create_model(weights: str) -> Any:
    # 모듈 상단에서 import 하면 ultralytics 가 없는 base 환경에서 이 모듈 import 가 깨진다
    from ultralytics import YOLO

    return YOLO(weights)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m modules.yolo_detection.remote.server", description="Serve yolo11n + ByteTrack over HTTP")
    parser.add_argument("--weights", default=DEFAULT_WEIGHTS, help=f"model weights (default {DEFAULT_WEIGHTS})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--device", default="auto", help="auto | mps | cpu | 0 (default auto)")
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMAGE_SIZE, help=f"inference size (default {DEFAULT_IMAGE_SIZE})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    device = parse_device(args.device)
    if device is None:
        device = resolve_auto_device()

    service = YoloDetectionService(create_model(args.weights), weights=args.weights, device=device, imgsz=args.imgsz)
    service.warmup(create_blank_image(args.imgsz))

    # 워밍업이 끝난 뒤에 리슨한다 — /health 가 200 이면 준비 완료
    server = create_yolo_server(service, args.host, args.port)
    logger.info("yolo server listening on http://%s:%d (weights=%s, device=%s)", args.host, args.port, args.weights, device)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
