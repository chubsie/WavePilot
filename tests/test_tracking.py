"""PERC-02 — 프레임 → Track 목록 집계 검증."""

from dataclasses import replace
from pathlib import Path

import pytest

from interfaces.detection import COCO_CLASS_IDS, Detection, Frame
from interfaces.geometry import project_ned_to_pixel
from perception.tracking import TrackAggregator

BOX_HALF_WIDTH_PX = 10.0
BOX_HEIGHT_PX = 30.0


def make_detection(camera, telemetry, track_id, class_name, north_m, east_m, confidence=0.8):
    """지면 점 (north_m, east_m) 에 하단 중앙이 닿는 박스를 만든다."""
    pixel = project_ned_to_pixel((north_m, east_m, 0.0), camera, telemetry)
    assert pixel is not None
    u, v = pixel

    return Detection(
        class_id=COCO_CLASS_IDS[class_name],
        class_name=class_name,
        confidence=confidence,
        xyxy=(u - BOX_HALF_WIDTH_PX, v - BOX_HEIGHT_PX, u + BOX_HALF_WIDTH_PX, v),
        track_id=track_id,
    )


def make_frame(camera, frame_id, timestamp_sec, detections):
    return Frame(
        frame_id=frame_id,
        timestamp_sec=timestamp_sec,
        image_width=camera.width_px,
        image_height=camera.height_px,
        detections=tuple(detections),
    )


@pytest.fixture
def aggregator(default_camera):
    return TrackAggregator(default_camera)


def test_two_frames_two_tracks_sorted_with_times(aggregator, default_camera, hover_telemetry):
    for frame_id, timestamp_sec in ((0, 1.0), (1, 1.5)):
        detections = [
            make_detection(default_camera, hover_telemetry, 2, "person", 25.0, 3.0),
            make_detection(default_camera, hover_telemetry, 1, "car", 30.0, -4.0),
        ]
        aggregator.update(make_frame(default_camera, frame_id, timestamp_sec, detections), hover_telemetry)

    tracks = aggregator.snapshot(now_sec=1.5)

    assert [track.track_id for track in tracks] == [1, 2]
    assert [track.class_name for track in tracks] == ["car", "person"]
    assert all(track.observation_count == 2 for track in tracks)
    assert all(track.first_seen_sec == 1.0 and track.last_seen_sec == 1.5 for track in tracks)
    assert tracks[0].north_m == pytest.approx(30.0, abs=1e-6)
    assert tracks[0].east_m == pytest.approx(-4.0, abs=1e-6)


def test_mean_confidence_is_arithmetic_mean(aggregator, default_camera, hover_telemetry):
    for frame_id, confidence in ((0, 0.8), (1, 0.6)):
        detection = make_detection(default_camera, hover_telemetry, 1, "car", 30.0, 0.0, confidence)
        aggregator.update(make_frame(default_camera, frame_id, float(frame_id), [detection]), hover_telemetry)

    (track,) = aggregator.snapshot(now_sec=1.0)

    assert track.mean_confidence == pytest.approx(0.7)


def test_class_majority_vote(aggregator, default_camera, hover_telemetry):
    class_sequence = ("car", "truck", "car", "car")
    for frame_id, class_name in enumerate(class_sequence):
        detection = make_detection(default_camera, hover_telemetry, 5, class_name, 30.0, 0.0)
        aggregator.update(make_frame(default_camera, frame_id, float(frame_id), [detection]), hover_telemetry)

    (track,) = aggregator.snapshot(now_sec=3.0)

    assert track.class_name == "car"


def test_class_vote_tie_breaks_by_confidence_sum(aggregator, default_camera, hover_telemetry):
    # truck 을 먼저 넣어 삽입 순서로는 truck 이 이기도록 — 신뢰도 합으로 car 가 이겨야 한다
    observations = (("truck", 0.5), ("truck", 0.5), ("car", 0.9), ("car", 0.9))
    for frame_id, (class_name, confidence) in enumerate(observations):
        detection = make_detection(default_camera, hover_telemetry, 5, class_name, 30.0, 0.0, confidence)
        aggregator.update(make_frame(default_camera, frame_id, float(frame_id), [detection]), hover_telemetry)

    (track,) = aggregator.snapshot(now_sec=3.0)

    assert track.class_name == "car"


def test_position_is_mean_of_last_window(aggregator, default_camera, hover_telemetry):
    east_list = [float(step) for step in range(12)]
    for frame_id, east_m in enumerate(east_list):
        detection = make_detection(default_camera, hover_telemetry, 1, "car", 30.0, east_m)
        aggregator.update(make_frame(default_camera, frame_id, frame_id * 0.1, [detection]), hover_telemetry)

    (track,) = aggregator.snapshot(now_sec=1.1)

    assert track.observation_count == 12
    assert track.east_m == pytest.approx(sum(east_list[-10:]) / 10, abs=1e-6)
    assert track.north_m == pytest.approx(30.0, abs=1e-6)


def test_untracked_detection_is_skipped_and_counted(aggregator, default_camera, hover_telemetry):
    untracked = make_detection(default_camera, hover_telemetry, None, "car", 30.0, 0.0)
    tracked = make_detection(default_camera, hover_telemetry, 1, "car", 30.0, 0.0)

    aggregator.update(make_frame(default_camera, 0, 0.0, [untracked, tracked]), hover_telemetry)

    assert aggregator.dropped_untracked_count == 1
    assert [track.track_id for track in aggregator.snapshot(now_sec=0.0)] == [1]


def test_horizon_detection_is_skipped_and_counted(aggregator, default_camera, hover_telemetry, caplog):
    telemetry = replace(hover_telemetry, gimbal_pitch_deg=-5.0)
    sky_detection = Detection(
        class_id=COCO_CLASS_IDS["car"],
        class_name="car",
        confidence=0.8,
        xyxy=(630.0, 0.0, 650.0, 0.0),
        track_id=9,
    )

    with caplog.at_level("INFO", logger="perception.tracking"):
        aggregator.update(make_frame(default_camera, 0, 0.0, [sky_detection]), telemetry)
        aggregator.update(make_frame(default_camera, 1, 0.1, [sky_detection]), telemetry)

    assert aggregator.dropped_horizon_count == 2
    assert aggregator.snapshot(now_sec=0.1) == []
    # 첫 수평선 드롭만 한 번 로그한다 — 프레임마다 찍지 않는다
    assert sum("horizon" in record.getMessage() for record in caplog.records) == 1


def test_is_active_flips_after_lost_timeout(aggregator, default_camera, hover_telemetry):
    detection = make_detection(default_camera, hover_telemetry, 1, "car", 30.0, 0.0)
    aggregator.update(make_frame(default_camera, 0, 10.0, [detection]), hover_telemetry)

    assert aggregator.snapshot(now_sec=12.0)[0].is_active is True

    lost_tracks = aggregator.snapshot(now_sec=12.5)
    assert len(lost_tracks) == 1
    assert lost_tracks[0].is_active is False


def test_tracking_uses_frame_clock_not_wall_clock():
    source = (Path(__file__).resolve().parents[1] / "perception" / "tracking.py").read_text(encoding="utf-8")

    assert "time.time(" not in source
    assert "time.monotonic(" not in source
