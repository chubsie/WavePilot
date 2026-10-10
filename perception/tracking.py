"""track 집계 (PERC-02, D-06) — 프레임별 YOLO 탐지를 track_id 별 월드 좌표 Track 으로 모은다.

시계는 frame.timestamp_sec 만 쓴다 — 벽시계를 쓰면 시나리오 재생이 결정적이지 않다.
반올림은 하지 않는다. 상태 JSON 을 만드는 llm_module 이 표시 정밀도를 정한다.
"""

import logging
from collections import Counter, deque
from dataclasses import dataclass, field

from interfaces.detection import Detection, Frame
from interfaces.flight_control import Telemetry
from interfaces.geometry import CameraIntrinsics
from interfaces.track import Track
from perception.projection import ground_anchor, pixel_to_ground

logger = logging.getLogger(__name__)

# 2 초 ≈ 10 fps 기준 20 프레임 — 몇 프레임 놓친 정도로는 track 을 잃은 것으로 보지 않는다
LOST_AFTER_SEC = 2.0
# 최근 10 개 지면 위치의 이동 평균 — 박스 흔들림이 월드 좌표로 그대로 튀는 것을 줄인다
POSITION_WINDOW = 10


@dataclass(slots=True)
class _TrackState:
    first_seen_sec: float
    recent_positions: deque
    last_seen_sec: float = 0.0
    observation_count: int = 0
    confidence_sum: float = 0.0
    class_votes: Counter = field(default_factory=Counter)
    class_confidence_sum: dict[str, float] = field(default_factory=dict)

    def record(self, detection: Detection, ground: tuple[float, float], timestamp_sec: float) -> None:
        self.recent_positions.append(ground)
        self.last_seen_sec = timestamp_sec
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
    ) -> None:
        self._camera = camera
        self._lost_after_sec = lost_after_sec
        self._position_window = position_window
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
        for detection in frame.detections:
            if detection.track_id is None:
                self._dropped_untracked_count += 1
                continue

            anchor_u, anchor_v = ground_anchor(detection.xyxy)
            ground = pixel_to_ground(anchor_u, anchor_v, self._camera, telemetry)
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
                )
            )
        return tracks

    def _get_or_create_state(self, track_id: int, timestamp_sec: float) -> _TrackState:
        state = self._states.get(track_id)
        if state is None:
            state = _TrackState(
                first_seen_sec=timestamp_sec,
                recent_positions=deque(maxlen=self._position_window),
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
