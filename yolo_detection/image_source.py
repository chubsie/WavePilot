"""녹화 프레임 디렉터리·비디오 파일을 (timestamp_sec, BGR ndarray) 로 내는 이미지 소스 (DET-01).

YoloDetectionSource 의 입력 타입 `Iterable[tuple[float, ndarray]]` 를 만든다.
yolo_detection 내부 타입이다 — interfaces/ 는 numpy 에 의존하면 안 되므로 거기에 두지 않는다.
시뮬레이터 라이브 스트림은 이 모듈의 범위 밖이다. 나중에 같은 iterable 규약으로 추가하면 된다.
박스는 원본 이미지 픽셀 좌표로 나오므로 DEFAULT_CAMERA 와 맞추려면 1280x720 을 넣는다 (RESEARCH Pitfall 3).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")  # suffix.lower() 로 비교한다
DEFAULT_FPS = 10.0  # mock 탐지 소스의 기본 fps 와 같다
MS_PER_SEC = 1000.0


class DirectoryImageSource:
    def __init__(self, directory: str | Path, fps: float = DEFAULT_FPS) -> None:
        if fps <= 0:
            raise ValueError(f"fps must be > 0, got {fps}")

        path = Path(directory)
        if not path.is_dir():
            raise FileNotFoundError(f"image directory not found: {path}")

        image_paths = sorted(
            candidate
            for candidate in path.iterdir()
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES
        )
        if not image_paths:
            raise ValueError(f"no .jpg/.jpeg/.png images in {path}")

        self._image_paths = image_paths
        self._fps = fps

    def __len__(self) -> int:
        return len(self._image_paths)

    def __iter__(self) -> Iterator[tuple[float, Any]]:
        # 모듈 상단에서 import 하면 ultralytics 미설치 환경에서 import yolo_detection.image_source 가 깨진다 (SC3)
        import cv2

        for index, image_path in enumerate(self._image_paths):
            image = cv2.imread(str(image_path))
            if image is None:
                raise ValueError(f"cannot read image {image_path}")
            yield index / self._fps, image


class VideoImageSource:
    def __init__(self, path: str | Path) -> None:
        video_path = Path(path)
        if not video_path.is_file():
            raise FileNotFoundError(f"video file not found: {video_path}")

        self._path = video_path

    def __iter__(self) -> Iterator[tuple[float, Any]]:
        # 모듈 상단에서 import 하면 ultralytics 미설치 환경에서 import yolo_detection.image_source 가 깨진다 (SC3)
        import cv2

        capture = cv2.VideoCapture(str(self._path))
        if not capture.isOpened():
            raise ValueError(f"cannot open video {self._path}")

        try:
            while True:
                ok, image = capture.read()
                if not ok:
                    break
                yield capture.get(cv2.CAP_PROP_POS_MSEC) / MS_PER_SEC, image
        finally:
            capture.release()
