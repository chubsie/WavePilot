"""track 집계 (PERC-02, D-06) — 프레임별 YOLO 탐지를 track_id 별 월드 좌표 Track 으로 모은다.

시계는 frame.timestamp_sec 만 쓴다 — 벽시계를 쓰면 시나리오 재생이 결정적이지 않다.
반올림은 하지 않는다. 상태 JSON 을 만드는 llm_report 가 표시 정밀도를 정한다.
상체 색은 최근 창 다수결로 확정한다(D-04) — 확정 뒤에도 창 기준으로 다시 계산하므로 뒤집힐 수 있다.
"""

import logging
from collections import Counter, deque
from dataclasses import dataclass, field

from modules.yolo_detection.interface.detection import Detection, Frame
from modules.flight_control.interface.flight_control import Telemetry
from modules.perception.interface.geometry import CameraIntrinsics, get_frame_camera
from modules.perception.interface.track import Track
from modules.perception.projection import ground_anchor, pixel_to_ground

logger = logging.getLogger(__name__)

# 2 초 ≈ 10 fps 기준 20 프레임 — 몇 프레임 놓친 정도로는 track 을 잃은 것으로 보지 않는다
LOST_AFTER_SEC = 2.0
# 최근 10 개 지면 위치의 이동 평균 — 박스 흔들림이 월드 좌표로 그대로 튀는 것을 줄인다
POSITION_WINDOW = 10
# 상체 색 다수결 (D-04, ASSUMED — 실측 후 조정): 최근 15 프레임 중 unknown 아닌 표 5개 이상, 1위 비율 60% 이상이면 확정
COLOR_WINDOW = 15
COLOR_MIN_VOTES = 5
COLOR_MIN_RATIO = 0.6


@dataclass(slots=True)
class _TrackState:
    first_seen_sec: float
    recent_positions: deque
    recent_colors: deque
    last_seen_sec: float = 0.0
    observation_count: int = 0
    confidence_sum: float = 0.0
    class_votes: Counter = field(default_factory=Counter)
    class_confidence_sum: dict[str, float] = field(default_factory=dict)

    def record(self, detection: Detection, ground: tuple[float, float], timestamp_sec: float) -> None:
        self.recent_positions.append(ground)
        self.recent_colors.append(detection.upper_color)
        # 프레임이 늦게(더 이른 timestamp 로) 도착할 수 있다 — 관측은 버리지 않고 구간만 넓혀 first <= last 를 지킨다
        self.first_seen_sec = min(self.first_seen_sec, timestamp_sec)
        self.last_seen_sec = max(self.last_seen_sec, timestamp_sec)
        self.observation_count += 1
        self.confidence_sum += detection.confidence

        self.class_votes[detection.class_name] += 1
        previous_sum = self.class_confidence_sum.get(detection.class_name, 0.0)
        self.class_confidence_sum[detection.class_name] = previous_sum + detection.confidence

    def get_majority_class(self) -> str:
        # 득표 수가 같으면 신뢰도 합이 큰 클래스 — 삽입 순서에 결과가 좌우되지 않게
        return max(
            self.class_votes,
            key=lambda class_name: (self.class_votes[class_name], self.class_confidence_sum[class_name]),
        )

    def get_color_summary(self, min_votes: int, min_ratio: float) -> tuple[str | None, float | None, int]:
        """(확정 색 또는 None, 1위 비율 또는 None, unknown 아닌 표 수). 비율·표수는 확정 여부와 무관하게 채운다."""
        color_votes = Counter(color for color in self.recent_colors if color is not None)
        vote_count = sum(color_votes.values())
        if vote_count == 0:
            return (None, None, 0)

        # 동점이면 색 이름으로 결정적으로 고른다 — 동점은 비율 <= 0.5 라 어차피 확정되지 않는다
        top_color = max(color_votes, key=lambda color: (color_votes[color], color))
        ratio = color_votes[top_color] / vote_count
        is_confirmed = vote_count >= min_votes and ratio >= min_ratio
        return (top_color if is_confirmed else None, ratio, vote_count)

    def get_mean_position(self) -> tuple[float, float]:
        position_count = len(self.recent_positions)
        north_m = sum(position[0] for position in self.recent_positions) / position_count
        east_m = sum(position[1] for position in self.recent_positions) / position_count
        return (north_m, east_m)


class TrackAggregator:
    def __init__(
        self,
        camera: CameraIntrinsics,
        lost_after_sec: float = LOST_AFTER_SEC,
        position_window: int = POSITION_WINDOW,
        color_window: int = COLOR_WINDOW,
        color_min_votes: int = COLOR_MIN_VOTES,
        color_min_ratio: float = COLOR_MIN_RATIO,
    ) -> None:
        if color_window < 1:
            raise ValueError(f"color_window must be >= 1, got {color_window}")
        if not 1 <= color_min_votes <= color_window:
            raise ValueError(f"color_min_votes must be in [1, color_window={color_window}], got {color_min_votes}")
        if not 0.0 < color_min_ratio <= 1.0:
            raise ValueError(f"color_min_ratio must be in (0, 1], got {color_min_ratio}")

        self._camera = camera
        self._lost_after_sec = lost_after_sec
        self._position_window = position_window
        self._color_window = color_window
        self._color_min_votes = color_min_votes
        self._color_min_ratio = color_min_ratio
        self._states: dict[int, _TrackState] = {}
        self._dropped_untracked_count = 0
        self._dropped_horizon_count = 0

    @property
    def dropped_untracked_count(self) -> int:
        return self._dropped_untracked_count

    @property
    def dropped_horizon_count(self) -> int:
        return self._dropped_horizon_count

    def update(self, frame: Frame, telemetry: Telemetry) -> None:
        # xyxy 는 프레임 픽셀이다 — 프레임이 보정 해상도와 다르면(브라우저 640x360) 같은 화각으로 스케일한다
        camera = get_frame_camera(self._camera, frame.image_width, frame.image_height)

        for detection in frame.detections:
            if detection.track_id is None:
                self._dropped_untracked_count += 1
                continue

            anchor_u, anchor_v = ground_anchor(detection.xyxy)
            ground = pixel_to_ground(anchor_u, anchor_v, camera, telemetry)
            if ground is None:
                self._handle_horizon_drop(detection, frame)
                continue

            state = self._get_or_create_state(detection.track_id, frame.timestamp_sec)
            state.record(detection, ground, frame.timestamp_sec)

    def snapshot(self, now_sec: float) -> list[Track]:
        """track_id 오름차순. 잃은 track 도 is_active=False 로 남긴다."""
        tracks: list[Track] = []
        for track_id in sorted(self._states):
            state = self._states[track_id]
            north_m, east_m = state.get_mean_position()
            upper_color, upper_color_ratio, upper_color_vote_count = state.get_color_summary(
                self._color_min_votes, self._color_min_ratio
            )
            tracks.append(
                Track(
                    track_id=track_id,
                    class_name=state.get_majority_class(),
                    north_m=north_m,
                    east_m=east_m,
                    first_seen_sec=state.first_seen_sec,
                    last_seen_sec=state.last_seen_sec,
                    observation_count=state.observation_count,
                    mean_confidence=state.confidence_sum / state.observation_count,
                    is_active=now_sec - state.last_seen_sec <= self._lost_after_sec,
                    upper_color=upper_color,
                    upper_color_ratio=upper_color_ratio,
                    upper_color_vote_count=upper_color_vote_count,
                )
            )
        return tracks

    def _get_or_create_state(self, track_id: int, timestamp_sec: float) -> _TrackState:
        state = self._states.get(track_id)
        if state is None:
            state = _TrackState(
                first_seen_sec=timestamp_sec,
                last_seen_sec=timestamp_sec,
                recent_positions=deque(maxlen=self._position_window),
                recent_colors=deque(maxlen=self._color_window),
            )
            self._states[track_id] = state
        return state

    def _handle_horizon_drop(self, detection: Detection, frame: Frame) -> None:
        # 첫 번째만 로그 — 짐벌이 들린 동안 프레임마다 찍히면 로그가 묻힌다
        if self._dropped_horizon_count == 0:
            logger.info(
                "dropping detections above horizon (first: track %s in frame %s); further drops are counted only",
                detection.track_id,
                frame.frame_id,
            )
        self._dropped_horizon_count += 1
