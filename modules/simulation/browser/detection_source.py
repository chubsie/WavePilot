"""브라우저 드론 카메라 프레임 -> YOLO -> Frame, 결과는 bridge 로 되돌린다 (SIM-01, D-02, D-11, D-12).

- BrowserFrameStream: SimBridge 의 최신 프레임 한 칸을 `(timestamp_sec, jpeg_bytes)` 로 흘린다. YOLO 가 밀리는 동안
  덮어써진 프레임은 건너뛴다(큐에 쌓지 않는다). 프레임마다 붙어 온 텔레메트리를 최근 100 개까지 기억한다 (D-11).
- BrowserDetectionSource: 주입받은 yolo_factory 로 스트림을 DetectionSource 로 바꾸고, 낸 Frame 과 지연을
  bridge.publish_detections 로 되돌려 브라우저 오버레이가 박스를 그리게 한다 (D-02).

JPEG 디코드·YOLO 생성은 여기서 하지 않는다 — cv2/numpy 는 yolo 어댑터 밖에서 import 할 수 없고(test_yolo_isolation),
simulation 은 yolo_detection 의 interface 만 알아야 한다(test_import_boundaries). 그래서 팩토리는 반드시 주입받고,
modules/drone/ 컴포지션 루트가 `functools.partial(create_jpeg_yolo_source, device=...)` 를 넘긴다.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator

from modules.flight_control.interface import Telemetry
from modules.simulation.browser.bridge import BrowserFrame, SimBridge
from modules.yolo_detection.interface import DetectionSource, Frame

TELEMETRY_HISTORY_SIZE = 100
TIMESTAMP_DECIMALS = 3  # YoloDetectionSource 가 timestamp_sec 를 반올림하는 자릿수와 같아야 조회가 맞는다
DEFAULT_POLL_TIMEOUT_SEC = 1.0
MS_PER_SEC = 1000.0

YoloFactory = Callable[[Iterable[tuple[float, bytes]]], DetectionSource]


class BrowserFrameStream:
    def __init__(self, bridge: SimBridge, poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC) -> None:
        if poll_timeout_sec <= 0:
            raise ValueError(f"poll_timeout_sec must be > 0, got {poll_timeout_sec}")

        self._bridge = bridge
        self._poll_timeout_sec = poll_timeout_sec
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._history: OrderedDict[float, BrowserFrame] = OrderedDict()
        self._last_frame: BrowserFrame | None = None

    def __iter__(self) -> Iterator[tuple[float, bytes]]:
        last_seq: int | None = None

        while not self._closed.is_set():
            started_at_sec = time.monotonic()
            frame = self._bridge.wait_frame(last_seq, self._poll_timeout_sec)

            if frame is None:
                if self._closed.is_set() or self._is_bridge_closed(started_at_sec):
                    return
                # 페이지를 새로 고치면 seq 가 0 부터 다시 시작한다 — 기준을 풀어야 그 프레임을 받는다
                last_seq = None
                continue

            last_seq = frame.seq
            if self._is_same_as_last(frame):
                continue

            self._remember(frame)
            yield frame.timestamp_sec, frame.jpeg_bytes

    @property
    def last_frame(self) -> BrowserFrame | None:
        with self._lock:
            return self._last_frame

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None:
        with self._lock:
            frame = self._history.get(round(timestamp_sec, TIMESTAMP_DECIMALS))

        return None if frame is None else frame.telemetry

    def close(self) -> None:
        self._closed.set()

    def _is_bridge_closed(self, started_at_sec: float) -> bool:
        # SimBridge.wait_frame 은 타임아웃을 다 채우거나 close 됐을 때만 None 을 낸다.
        # 일찍 돌아온 None 은 bridge 가 밖에서 닫혔다는 뜻이다 — 계속 돌면 바쁜 루프가 된다
        return time.monotonic() - started_at_sec < self._poll_timeout_sec

    def _is_same_as_last(self, frame: BrowserFrame) -> bool:
        last_frame = self.last_frame
        if last_frame is None:
            return False
        return frame.seq == last_frame.seq and frame.received_at_sec == last_frame.received_at_sec

    def _remember(self, frame: BrowserFrame) -> None:
        with self._lock:
            self._last_frame = frame
            self._history[round(frame.timestamp_sec, TIMESTAMP_DECIMALS)] = frame
            while len(self._history) > TELEMETRY_HISTORY_SIZE:
                self._history.popitem(last=False)


class BrowserDetectionSource:
    def __init__(
        self,
        bridge: SimBridge,
        yolo_factory: YoloFactory,
        clock: Callable[[], float] = time.monotonic,
        poll_timeout_sec: float = DEFAULT_POLL_TIMEOUT_SEC,
    ) -> None:
        self._bridge = bridge
        self._clock = clock
        self._stream = BrowserFrameStream(bridge, poll_timeout_sec)
        self._source = yolo_factory(self._stream)

    def read_frame(self) -> Frame | None:
        frame = self._source.read_frame()
        if frame is None:
            return None

        # YoloDetectionSource 는 read_frame 한 번에 디코드되는 항목 하나만 꺼내므로, 마지막으로 흘린 프레임이 방금 추론한 프레임이다
        browser_frame = self._stream.last_frame
        latency_ms = (self._clock() - browser_frame.received_at_sec) * MS_PER_SEC
        self._bridge.publish_detections(frame.to_dict(), latency_ms)
        return frame

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None:
        return self._stream.telemetry_for(timestamp_sec)

    def close(self) -> None:
        # 스트림을 먼저 닫고 bridge 를 닫아 wait_frame 에서 자고 있는 read_frame 을 깨운다
        self._stream.close()
        self._bridge.close()


def describe_frame(frame: Frame) -> str:
    header = f"frame {frame.frame_id} t={frame.timestamp_sec:.2f}s {len(frame.detections)} det"
    if not frame.detections:
        return header

    detection_texts = [
        f"{detection.class_name}#{detection.track_id} {detection.confidence:.2f}" for detection in frame.detections
    ]
    return f"{header} | {'; '.join(detection_texts)}"


def run_live_yolo(bridge: SimBridge, yolo_factory: YoloFactory, on_frame: Callable[[str], None] = print) -> None:
    source = BrowserDetectionSource(bridge, yolo_factory)

    try:
        while (frame := source.read_frame()) is not None:
            on_frame(describe_frame(frame))
    finally:
        source.close()
