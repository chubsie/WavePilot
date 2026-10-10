"""PERC-02 — 프레임 → Track 목록 집계 검증."""

from dataclasses import replace
from pathlib import Path

import pytest

from modules.yolo_detection.interface.detection import COCO_CLASS_IDS, Detection, Frame
from modules.perception.interface.geometry import project_ned_to_pixel
from modules.perception.interface.track import Track
from modules.perception.tracking import COLOR_MIN_RATIO, COLOR_MIN_VOTES, COLOR_WINDOW, TrackAggregator

BOX_HALF_WIDTH_PX = 10.0
BOX_HEIGHT_PX = 30.0


def make_detection(camera, telemetry, track_id, class_name, north_m, east_m, confidence=0.8, upper_color=None):
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
        upper_color=upper_color,
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

    with caplog.at_level("INFO", logger="modules.perception.tracking"):
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
    source = (Path(__file__).resolve().parents[1] / "modules" / "perception" / "tracking.py").read_text(encoding="utf-8")

    assert "time.time(" not in source
    assert "time.monotonic(" not in source


# --- 상체 색 다수결 (D-04) ---


def _feed_colors(aggregator, camera, telemetry, colors, track_id=1, start_frame_id=0):
    for offset, color in enumerate(colors):
        frame_id = start_frame_id + offset
        detection = make_detection(camera, telemetry, track_id, "person", 25.0, 3.0, upper_color=color)
        aggregator.update(make_frame(camera, frame_id, frame_id * 0.1, [detection]), telemetry)


def _get_color_fields(aggregator, track_id=1):
    track = next(track for track in aggregator.snapshot(now_sec=100.0) if track.track_id == track_id)
    return (track.upper_color, track.upper_color_ratio, track.upper_color_vote_count)


def test_color_defaults_match_decision_memo():
    assert (COLOR_WINDOW, COLOR_MIN_VOTES, COLOR_MIN_RATIO) == (15, 5, 0.6)


@pytest.mark.parametrize(
    "colors, expected",
    [
        (["red"] * 5, ("red", 1.0, 5)),
        (["red"] * 4 + [None] * 6, (None, 1.0, 4)),
        (["red"] * 3 + ["other"] * 2, ("red", 0.6, 5)),
        (["red"] * 3 + ["other"] * 3, (None, 0.5, 6)),
        ([None] * 8, (None, None, 0)),
    ],
    ids=["five-red", "four-votes", "ratio-boundary", "tie", "no-votes"],
)
def test_color_majority_boundaries(aggregator, default_camera, hover_telemetry, colors, expected):
    _feed_colors(aggregator, default_camera, hover_telemetry, colors)

    color, ratio, vote_count = _get_color_fields(aggregator)

    assert color == expected[0]
    assert ratio == (None if expected[1] is None else pytest.approx(expected[1]))
    assert vote_count == expected[2]


def test_color_window_slides_and_confirmed_color_can_flip(aggregator, default_camera, hover_telemetry):
    _feed_colors(aggregator, default_camera, hover_telemetry, ["other"] * 15)
    assert _get_color_fields(aggregator) == ("other", 1.0, 15)

    _feed_colors(aggregator, default_camera, hover_telemetry, ["red"] * 15, start_frame_id=15)

    assert _get_color_fields(aggregator) == ("red", 1.0, 15)


def test_colors_of_different_tracks_do_not_mix(aggregator, default_camera, hover_telemetry):
    _feed_colors(aggregator, default_camera, hover_telemetry, ["red"] * 5, track_id=1)
    _feed_colors(aggregator, default_camera, hover_telemetry, ["other"] * 5, track_id=2, start_frame_id=5)

    assert _get_color_fields(aggregator, 1) == ("red", 1.0, 5)
    assert _get_color_fields(aggregator, 2) == ("other", 1.0, 5)


def test_color_settings_can_be_overridden(default_camera, hover_telemetry):
    aggregator = TrackAggregator(default_camera, color_window=3, color_min_votes=2, color_min_ratio=1.0)

    _feed_colors(aggregator, default_camera, hover_telemetry, ["other", "red", "red", "red"])

    assert _get_color_fields(aggregator) == ("red", 1.0, 3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"color_window": 0},
        {"color_min_votes": 0},
        {"color_window": 4, "color_min_votes": 5},
        {"color_min_ratio": 0.0},
        {"color_min_ratio": 1.1},
    ],
)
def test_color_settings_are_validated(default_camera, kwargs):
    with pytest.raises(ValueError):
        TrackAggregator(default_camera, **kwargs)


# --- Track 직렬화: 상체 색 필드 ---


def _make_track(**overrides) -> Track:
    fields = dict(
        track_id=3, class_name="person", north_m=10.0, east_m=-2.0, first_seen_sec=1.0, last_seen_sec=2.0,
        observation_count=6, mean_confidence=0.8, is_active=True,
    )
    fields.update(overrides)
    return Track(**fields)


def test_track_round_trip_keeps_color_fields():
    track = _make_track(upper_color="red", upper_color_ratio=0.8, upper_color_vote_count=5)

    assert Track.from_dict(track.to_dict()) == track


def test_track_from_old_dict_without_color_keys_uses_defaults():
    data = _make_track().to_dict()
    for key in ("upper_color", "upper_color_ratio", "upper_color_vote_count"):
        del data[key]

    track = Track.from_dict(data)

    assert (track.upper_color, track.upper_color_ratio, track.upper_color_vote_count) == (None, None, 0)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("upper_color", "blue"),
        ("upper_color", 1),
        ("upper_color_ratio", 1.5),
        ("upper_color_ratio", True),
        ("upper_color_vote_count", -1),
        ("upper_color_vote_count", True),
    ],
)
def test_track_from_dict_rejects_bad_color_fields(field, bad_value):
    data = _make_track().to_dict()
    data[field] = bad_value

    with pytest.raises(ValueError, match=field):
        Track.from_dict(data)
