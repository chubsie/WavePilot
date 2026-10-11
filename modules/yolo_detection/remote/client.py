"""Orin YOLO 서버에 JPEG 를 보내 Frame 을 받아 오는 DetectionSource (SIM-05, D-03, D-04).

`(timestamp_sec, jpeg_bytes)` 항목마다 POST /v1/detect 를 보내고, 서버가 만든 Frame 을 그대로 돌려준다.
YoloFactory 모양이라 `functools.partial(create_remote_yolo_source, base_url=...)` 를 BrowserDetectionSource 에 넘기면 된다.

실패 정책 (D-03):
- 생성 시 POST /v1/reset 을 한 번 보낸다. 서버에 닿지 않으면 첫 프레임이 아니라 생성 시점에 RemoteYoloError.
- 프레임 하나가 실패하면(timeout·연결 거부·HTTP 400/422/5xx·응답 모양 이상·timestamp 불일치) WARNING 을 남기고 건너뛴다.
  연속 max_consecutive_failures 번 실패하면 RemoteYoloError. 성공 한 번이면 연속 횟수는 0 으로 돌아간다. 로컬 대체는 없다.

지연 (D-04): 성공한 프레임마다 FrameLatency(total, orin, net) 를 쌓고 DEBUG 한 줄을 남긴다.
분포 요약(INFO)은 스트림이 끝나거나 close() 가 불릴 때 중 먼저 오는 쪽에서 한 번만 남긴다 —
BrowserDetectionSource.close() 는 안쪽 소스의 close() 를 부르지 않기 때문이다.

표준 라이브러리 urllib 만 쓴다 — Mac 은 yolo 그룹 없이 Orin 모드를 쓸 수 있다.
HTTP 패턴은 modules/decision/systemone_client.py 를 따랐다(import 경계 때문에 import 하지 않는다).
"""

import http.client
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable

from modules.yolo_detection.interface import Frame
from modules.yolo_detection.remote.latency import FrameLatency, format_latency_table
from modules.yolo_detection.remote.protocol import (
    DETECT_PATH,
    JPEG_CONTENT_TYPE,
    MAX_JPEG_BYTES,
    RESET_PATH,
    TIMESTAMP_HEADER,
    format_timestamp_header,
    parse_detect_response,
)

DEFAULT_TIMEOUT_SEC = 2.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10
MAX_RESPONSE_BYTES = 1_048_576
ERROR_DETAIL_MAX_CHARS = 200
ALLOWED_URL_SCHEMES = ("http", "https")
TIMESTAMP_DECIMALS = 3  # 서버가 round(sent, 3) 을 돌려준다
MS_PER_SEC = 1000.0

logger = logging.getLogger(__name__)


class RemoteYoloError(RuntimeError):
    """Orin YOLO 서버를 쓸 수 없다 — 생성 시 닿지 않거나, 프레임이 연속으로 실패했다."""


class FrameRequestError(Exception):
    """프레임 하나의 실패 — 건너뛰고 연속 실패 횟수만 센다(모듈 안에서만 쓴다)."""


class RemoteYoloDetectionSource:
    def __init__(
        self,
        jpeg_items: Iterable[tuple[float, bytes]],
        base_url: str,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_base_url(base_url)
        if timeout_sec <= 0:
            raise ValueError(f"timeout_sec must be > 0, got {timeout_sec}")
        if max_consecutive_failures < 1:
            raise ValueError(f"max_consecutive_failures must be >= 1, got {max_consecutive_failures}")

        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._max_consecutive_failures = max_consecutive_failures
        self._clock = clock
        # 빈 프록시 표: Orin 은 LAN 이라 프록시가 필요 없고, HTTP_PROXY 가 프레임을 밖으로 내보내지 않게 한다
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        self._latencies: list[FrameLatency] = []
        self._failure_count = 0
        self._consecutive_failures = 0
        self._is_summary_logged = False

        self._reset_server()
        self._items = iter(jpeg_items)

    @property
    def latencies(self) -> tuple[FrameLatency, ...]:
        return tuple(self._latencies)

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def read_frame(self) -> Frame | None:
        for timestamp_sec, jpeg_bytes in self._items:
            try:
                return self._detect(timestamp_sec, jpeg_bytes)
            except FrameRequestError as error:
                self._record_failure(timestamp_sec, str(error))

        self._log_summary()
        return None

    def close(self) -> None:
        self._log_summary()

    def _reset_server(self) -> None:
        request = urllib.request.Request(f"{self._base_url}{RESET_PATH}", data=b"", method="POST")

        try:
            self._send(request)
        except FrameRequestError as error:
            raise RemoteYoloError(f"cannot reset YOLO server at {self._base_url}: {error}") from error

    def _detect(self, timestamp_sec: float, jpeg_bytes: bytes) -> Frame:
        # 서버가 400 으로 거절할 크기는 보내지 않는다
        if len(jpeg_bytes) > MAX_JPEG_BYTES:
            raise FrameRequestError(f"jpeg too large: {len(jpeg_bytes)} > {MAX_JPEG_BYTES} bytes (not sent)")

        headers = {"Content-Type": JPEG_CONTENT_TYPE, TIMESTAMP_HEADER: format_timestamp_header(timestamp_sec)}
        request = urllib.request.Request(f"{self._base_url}{DETECT_PATH}", data=jpeg_bytes, headers=headers, method="POST")

        started_at = self._clock()
        body = self._send(request)
        total_ms = (self._clock() - started_at) * MS_PER_SEC

        try:
            frame, timing = parse_detect_response(body)
        except ValueError as error:
            raise FrameRequestError(f"bad response: {error}") from error
        # 서버는 round(sent, 3) 을 돌려준다 — 반올림하지 않은 값과 비교하면 정상 프레임도 틀린다
        if round(frame.timestamp_sec, TIMESTAMP_DECIMALS) != round(timestamp_sec, TIMESTAMP_DECIMALS):
            raise FrameRequestError(f"timestamp mismatch: sent {timestamp_sec}, got {frame.timestamp_sec}")

        latency = FrameLatency.from_timing(timestamp_sec, total_ms, timing.total_ms)
        self._latencies.append(latency)
        self._consecutive_failures = 0
        logger.debug(
            "remote yolo t=%.3f total=%.1fms orin=%.1fms net=%.1fms",
            timestamp_sec, latency.total_ms, latency.orin_ms, latency.network_ms,
        )
        return frame

    def _send(self, request: urllib.request.Request) -> bytes:
        """요청을 보내 응답 본문을 돌려준다. 모든 전송 실패를 FrameRequestError 로 바꾼다.

        except 순서가 의미를 가진다 — HTTPError 는 URLError 의 하위 클래스라 먼저 잡는다.
        """
        try:
            with self._opener.open(request, timeout=self._timeout_sec) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise FrameRequestError(f"HTTP {error.code}: {_read_error_detail(error)}") from error
        except urllib.error.URLError as error:
            raise FrameRequestError(f"cannot reach {self._base_url}: {error.reason}") from error
        # 본문을 읽는 도중의 timeout·연결 끊김은 URLError 로 감싸지지 않는다
        except (OSError, http.client.HTTPException) as error:
            raise FrameRequestError(f"{type(error).__name__}: {error}") from error

        if len(body) > MAX_RESPONSE_BYTES:
            raise FrameRequestError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes")
        return body

    def _record_failure(self, timestamp_sec: float, reason: str) -> None:
        self._failure_count += 1
        self._consecutive_failures += 1
        logger.warning("remote yolo frame t=%.3f skipped: %s", timestamp_sec, reason)

        if self._consecutive_failures >= self._max_consecutive_failures:
            self._log_summary()
            raise RemoteYoloError(
                f"{self._consecutive_failures} consecutive frames failed at {self._base_url} (last: {reason})"
            )

    def _log_summary(self) -> None:
        if self._is_summary_logged:
            return

        self._is_summary_logged = True
        table = "\n".join(format_latency_table(self._latencies, self._failure_count))
        logger.info("remote yolo latency (%s)\n%s", self._base_url, table)


def create_remote_yolo_source(
    jpeg_items: Iterable[tuple[float, bytes]],
    base_url: str,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> RemoteYoloDetectionSource:
    """YoloFactory 모양 — `functools.partial(create_remote_yolo_source, base_url=...)` 로 넘긴다 (D-05)."""
    return RemoteYoloDetectionSource(
        jpeg_items, base_url, timeout_sec=timeout_sec, max_consecutive_failures=max_consecutive_failures
    )


def _validate_base_url(base_url: str) -> None:
    # urllib 은 file:// 등도 열 수 있으므로 http/https + 호스트가 있는 URL 만 허용한다
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ALLOWED_URL_SCHEMES or not parts.hostname:
        raise ValueError(f"base_url must be an http(s) URL with a host, got {base_url!r}")


def _read_error_detail(error: urllib.error.HTTPError) -> str:
    """오류 응답 본문 앞부분만 읽는다. 읽기 자체가 실패해도 원래 오류는 그대로 남긴다."""
    try:
        raw_detail = error.read(ERROR_DETAIL_MAX_CHARS * 4)
    except (OSError, http.client.HTTPException):
        return "<unreadable body>"

    return raw_detail.decode("utf-8", errors="replace")[:ERROR_DETAIL_MAX_CHARS]
