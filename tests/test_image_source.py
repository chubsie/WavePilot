"""디렉터리·비디오 이미지 소스 테스트 (DET-01, 계약 C3).

cv2 가 yolo 그룹에만 있어서 마커 yolo 로 기본 실행에서 빠진다.
이미지·비디오는 tmp_path 에 합성해서 쓰므로 바이너리를 커밋하지 않는다.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.yolo

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from yolo_detection.image_source import DirectoryImageSource, VideoImageSource

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720
PIXEL_TOLERANCE = 3  # jpg 손실 압축 오차


def _write_uniform_image(path: Path, value: int, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> None:
    image = np.full((height, width, 3), value, dtype=np.uint8)
    assert cv2.imwrite(str(path), image)


def _write_video(path: Path, frame_count: int, width: int, height: int) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (width, height))
    if not writer.isOpened():
        pytest.skip("cv2.VideoWriter MJPG is not available")
    for index in range(frame_count):
        writer.write(np.full((height, width, 3), index * 20, dtype=np.uint8))
    writer.release()


def _write_three_images(directory: Path) -> None:
    _write_uniform_image(directory / "frame_002.png", 30)
    _write_uniform_image(directory / "frame_000.png", 10)
    _write_uniform_image(directory / "frame_001.jpg", 20)


# --- DirectoryImageSource ---


def test_directory_source_yields_images_in_name_order(tmp_path) -> None:
    _write_three_images(tmp_path)
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    source = DirectoryImageSource(tmp_path, fps=10.0)

    items = list(source)

    assert len(source) == 3
    assert [timestamp for timestamp, _ in items] == pytest.approx([0.0, 0.1, 0.2])
    assert all(image.shape == (IMAGE_HEIGHT, IMAGE_WIDTH, 3) for _, image in items)
    assert [float(image.mean()) for _, image in items] == pytest.approx([10, 20, 30], abs=PIXEL_TOLERANCE)


def test_directory_source_timestamps_follow_fps(tmp_path) -> None:
    _write_three_images(tmp_path)

    items = list(DirectoryImageSource(tmp_path, fps=2.0))

    assert [timestamp for timestamp, _ in items] == pytest.approx([0.0, 0.5, 1.0])


def test_directory_source_accepts_uppercase_suffix(tmp_path) -> None:
    _write_uniform_image(tmp_path / "FRAME.PNG", 50)

    source = DirectoryImageSource(tmp_path)

    assert len(source) == 1


def test_directory_source_can_be_iterated_twice(tmp_path) -> None:
    _write_three_images(tmp_path)
    source = DirectoryImageSource(tmp_path)

    first_timestamps = [timestamp for timestamp, _ in source]
    second_timestamps = [timestamp for timestamp, _ in source]

    assert first_timestamps == second_timestamps
    assert len(second_timestamps) == 3


@pytest.mark.parametrize("fps", [0, -1])
def test_directory_source_rejects_non_positive_fps(tmp_path, fps) -> None:
    _write_three_images(tmp_path)

    with pytest.raises(ValueError, match="fps"):
        DirectoryImageSource(tmp_path, fps=fps)


def test_directory_source_rejects_missing_directory(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        DirectoryImageSource(tmp_path / "missing")


def test_directory_source_rejects_directory_without_images(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("no images", encoding="utf-8")

    with pytest.raises(ValueError, match=str(tmp_path)):
        DirectoryImageSource(tmp_path)


def test_directory_source_names_corrupt_file_on_iteration(tmp_path) -> None:
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    source = DirectoryImageSource(tmp_path)

    with pytest.raises(ValueError, match="bad.jpg"):
        list(source)


# --- VideoImageSource ---


def test_video_source_yields_frames_with_increasing_timestamps(tmp_path) -> None:
    video_path = tmp_path / "clip.avi"
    _write_video(video_path, frame_count=5, width=320, height=240)

    items = list(VideoImageSource(video_path))

    timestamps = [timestamp for timestamp, _ in items]
    assert len(items) == 5
    assert all(image.shape == (240, 320, 3) for _, image in items)
    assert timestamps == sorted(timestamps)
    assert timestamps[0] <= 0.1
    assert timestamps[-1] < 1.0


def test_video_source_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        VideoImageSource(tmp_path / "missing.mp4")
