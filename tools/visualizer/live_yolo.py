"""serve_camera.py --live 용: 브라우저가 보낸 드론 카메라 프레임을 받는 즉시 yolo11n 으로 분석한다.

녹화 세션 하나 = YOLO 스트림 하나다. 세션마다 스레드 하나가 YoloDetectionSource 를 돌린다 —
추적(ByteTrack) 상태가 세션 사이에 섞이지 않게 모델도 세션마다 새로 만든다.
입력 어댑터는 `(timestamp_sec, BGR ndarray)` 를 내는 반복자라서 YOLO 쪽 코드는 그대로다.

결과는 터미널에 프레임마다 한 줄로 바로 찍고, 세션 폴더에도 남긴다:
    <session>/yolo/detections.jsonl   Frame JSON lines (run_yolo.py 와 같은 형식)
    <session>/yolo/detections.log     사람이 읽는 줄 + 끝에 track 요약
"""

import json
import queue
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.yolo_detection.run_yolo import parse_device, resolve_auto_device
from modules.yolo_detection.show_detections import format_frame_line, format_summary_lines
from modules.yolo_detection.source import YoloDetectionSource

DEFAULT_WEIGHTS = REPO_ROOT / "models" / "yolo11n.pt"
END_OF_SESSION = None  # 반복자 종료 신호


class SessionStream:
    """HTTP 요청은 순서가 뒤섞여 도착할 수 있다 — index 순서로 다시 세워 YOLO 에 넣는다 (추적이 순서에 의존)."""

    def __init__(self, fps: float) -> None:
        self._fps = fps
        self._ordered: queue.Queue = queue.Queue()
        self._waiting: dict[int, bytes] = {}
        self._next_index = 0
        self._lock = threading.Lock()

    def put(self, index: int, jpeg_bytes: bytes) -> None:
        with self._lock:
            self._waiting[index] = jpeg_bytes
            while self._next_index in self._waiting:
                self._ordered.put((self._next_index, self._waiting.pop(self._next_index)))
                self._next_index += 1

    def close(self) -> None:
        # 업로드에 실패해 빠진 index 가 있어도 남은 프레임은 순서대로 흘려보낸다
        with self._lock:
            for index in sorted(self._waiting):
                self._ordered.put((index, self._waiting.pop(index)))
            self._ordered.put(END_OF_SESSION)

    def __iter__(self):
        # cv2·numpy 는 yolo 그룹에만 있다 — --live 일 때만 이 모듈을 import 한다
        import cv2
        import numpy as np

        while (item := self._ordered.get()) is not END_OF_SESSION:
            index, jpeg_bytes = item
            image = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                yield index / self._fps, image


class LiveYoloDetector:
    def __init__(self, fps: float, weights: Path = DEFAULT_WEIGHTS, device: str = "auto") -> None:
        self._fps = fps
        self._weights = str(weights)
        parsed_device = parse_device(device)
        self._device = resolve_auto_device() if parsed_device is None else parsed_device
        self._streams: dict[Path, SessionStream] = {}
        self._lock = threading.Lock()
        print(f"live YOLO: {self._weights} on {self._device}", flush=True)

    def submit_frame(self, session_dir: Path, index: int, jpeg_bytes: bytes) -> None:
        with self._lock:
            stream = self._streams.get(session_dir)
            if stream is None:
                stream = self._streams[session_dir] = SessionStream(self._fps)
                threading.Thread(target=self._run_session, args=(session_dir, stream), daemon=True).start()
        stream.put(index, jpeg_bytes)

    def finish_session(self, session_dir: Path) -> None:
        with self._lock:
            stream = self._streams.pop(session_dir, None)
        if stream is not None:
            stream.close()

    def _run_session(self, session_dir: Path, stream: SessionStream) -> None:
        out_dir = session_dir / "yolo"
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = f"[{session_dir.name}]"
        detection_source = YoloDetectionSource(stream, weights=self._weights, device=self._device)

        frames = []
        with open(out_dir / "detections.jsonl", "w", encoding="utf-8") as jsonl_file, \
                open(out_dir / "detections.log", "w", encoding="utf-8") as log_file:
            while (frame := detection_source.read_frame()) is not None:
                frames.append(frame)
                line = format_frame_line(frame)
                print(f"{tag} {line}", flush=True)
                jsonl_file.write(json.dumps(frame.to_dict()) + "\n")
                log_file.write(line + "\n")
                log_file.flush()

            summary_lines = format_summary_lines(frames)
            log_file.write("\n" + "\n".join(summary_lines) + "\n")

        for line in summary_lines:
            print(f"{tag} {line}", flush=True)
        print(f"{tag} log: {out_dir / 'detections.log'}", flush=True)
