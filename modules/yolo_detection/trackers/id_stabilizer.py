"""트래커 id 안정화 — ByteTrack 이 같은 대상에 새 id 를 줘도 Frame 밖으로는 같은 id 가 나가게 한다.

작은 박스(h 15~60 px)는 트래커 설정을 조정해도 가끔 끊긴다. 끊긴 직후 같은 클래스의 새 id 가 직전에 사라진 트랙
근처에 나타나면 그 트랙의 안정 id 를 이어 준다. 시계는 frame.timestamp_sec 만 쓴다(재생 결정성).

YoloDetectionSource 안에서 Frame 을 내기 직전에 적용한다 — 브라우저 오버레이(publish_detections)와
TrackAggregator·FireHeart 가 모두 같은 Frame 을 받으므로 위아래가 같은 id 를 본다.
그 단계에는 텔레메트리가 없어 지면 좌표 대신 픽셀 거리(박스 높이로 정규화)와 시간 간격으로 판정한다.
"""

import dataclasses
import math
from dataclasses import dataclass

from modules.yolo_detection.interface.detection import Detection, Frame

# 끊긴 트랙을 이어 줄 최대 시간 간격 — 실측 id 교체 주기(0.4~1 s)보다 길고 rule_mode grace(1.1 s) 와 비슷하게
RELINK_MAX_GAP_SEC = 1.5
# 이어 붙일 반경 = max(최소 픽셀, 박스 높이 배수) + 간격 동안 화면이 흘러갈 수 있는 거리
RELINK_MIN_RADIUS_PX = 24.0
RELINK_HEIGHT_RATIO = 1.5
RELINK_DRIFT_PX_PER_SEC = 60.0
# 박스 높이가 이 배수 넘게 달라지면 다른 대상으로 본다
RELINK_MAX_HEIGHT_RATIO = 2.0
# 이 시간 넘게 안 보인 안정 트랙은 잊는다 — 메모리 상한
FORGET_AFTER_SEC = 10.0


@dataclass(slots=True)
class _StableTrack:
    class_name: str
    center: tuple[float, float]
    height: float
    last_seen_sec: float


@dataclass(frozen=True, slots=True)
class _RelinkCandidate:
    score: float
    detection_index: int
    stable_id: int


class TrackIdStabilizer:
    def __init__(
        self,
        max_gap_sec: float = RELINK_MAX_GAP_SEC,
        min_radius_px: float = RELINK_MIN_RADIUS_PX,
        height_ratio: float = RELINK_HEIGHT_RATIO,
        drift_px_per_sec: float = RELINK_DRIFT_PX_PER_SEC,
        max_height_ratio: float = RELINK_MAX_HEIGHT_RATIO,
        forget_after_sec: float = FORGET_AFTER_SEC,
    ) -> None:
        if max_gap_sec < 0:
            raise ValueError(f"max_gap_sec must be >= 0, got {max_gap_sec}")
        if max_height_ratio < 1.0:
            raise ValueError(f"max_height_ratio must be >= 1, got {max_height_ratio}")
        if forget_after_sec < max_gap_sec:
            raise ValueError(f"forget_after_sec must be >= max_gap_sec={max_gap_sec}, got {forget_after_sec}")

        self._max_gap_sec = max_gap_sec
        self._min_radius_px = min_radius_px
        self._height_ratio = height_ratio
        self._drift_px_per_sec = drift_px_per_sec
        self._max_height_ratio = max_height_ratio
        self._forget_after_sec = forget_after_sec
        self._stable_id_by_raw_id: dict[int, int] = {}
        self._tracks: dict[int, _StableTrack] = {}
        self._issued_ids: set[int] = set()

    def stabilize(self, frame: Frame) -> Frame:
        """track_id 를 안정 id 로 바꾼 Frame. track_id 가 None 인 탐지는 그대로 둔다."""
        now_sec = frame.timestamp_sec
        self._forget_stale(now_sec)
        stable_ids: list[int | None] = [None] * len(frame.detections)
        claimed_ids: set[int] = set()

        # 1) 이미 아는 raw id 가 먼저 자기 안정 id 를 차지한다
        unknown_indices: list[int] = []
        for index, detection in enumerate(frame.detections):
            if detection.track_id is None:
                continue
            stable_id = self._stable_id_by_raw_id.get(detection.track_id)
            if stable_id is None or stable_id in claimed_ids:
                unknown_indices.append(index)
                continue
            stable_ids[index] = stable_id
            claimed_ids.add(stable_id)

        # 2) 처음 보는 raw id 는 근처에서 막 사라진 같은 클래스 트랙에 잇는다 (가까운 쌍부터)
        for candidate in self._find_relink_candidates(frame.detections, unknown_indices, claimed_ids, now_sec):
            if stable_ids[candidate.detection_index] is not None or candidate.stable_id in claimed_ids:
                continue
            stable_ids[candidate.detection_index] = candidate.stable_id
            claimed_ids.add(candidate.stable_id)

        # 3) 이을 곳이 없으면 새 안정 id
        for index in unknown_indices:
            if stable_ids[index] is None:
                stable_ids[index] = self._issue_id(frame.detections[index].track_id)

        detections = tuple(self._apply(detection, stable_id, now_sec) for detection, stable_id in zip(frame.detections, stable_ids))
        return dataclasses.replace(frame, detections=detections)

    def _find_relink_candidates(
        self, detections: tuple[Detection, ...], unknown_indices: list[int], claimed_ids: set[int], now_sec: float
    ) -> list[_RelinkCandidate]:
        candidates: list[_RelinkCandidate] = []
        for index in unknown_indices:
            detection = detections[index]
            center, height = _get_center_and_height(detection.xyxy)
            for stable_id, track in self._tracks.items():
                if stable_id in claimed_ids or track.class_name != detection.class_name:
                    continue

                gap_sec = now_sec - track.last_seen_sec
                if not 0.0 <= gap_sec <= self._max_gap_sec:
                    continue
                if max(height, track.height) > self._max_height_ratio * max(min(height, track.height), 1e-6):
                    continue

                radius = max(self._min_radius_px, self._height_ratio * max(height, track.height))
                radius += self._drift_px_per_sec * gap_sec
                distance = math.dist(center, track.center)
                if distance <= radius:
                    candidates.append(_RelinkCandidate(distance / radius, index, stable_id))

        return sorted(candidates, key=lambda candidate: (candidate.score, candidate.stable_id))

    def _issue_id(self, raw_id: int | None) -> int:
        # raw id 숫자를 그대로 쓰면 로그·화면의 T 번호가 YOLO 와 같아 읽기 쉽다. 이미 쓴 번호면(트래커 리셋) 새 번호
        stable_id = raw_id if raw_id is not None and raw_id not in self._issued_ids else max(self._issued_ids, default=0) + 1
        self._issued_ids.add(stable_id)
        return stable_id

    def _apply(self, detection: Detection, stable_id: int | None, now_sec: float) -> Detection:
        if detection.track_id is None or stable_id is None:
            return detection

        self._stable_id_by_raw_id[detection.track_id] = stable_id
        center, height = _get_center_and_height(detection.xyxy)
        track = self._tracks.get(stable_id)
        # 늦게 도착한 프레임이 마지막 위치를 과거로 되돌리지 않게 한다
        if track is None or now_sec >= track.last_seen_sec:
            self._tracks[stable_id] = _StableTrack(detection.class_name, center, height, now_sec)
        return dataclasses.replace(detection, track_id=stable_id)

    def _forget_stale(self, now_sec: float) -> None:
        stale_ids = {
            stable_id for stable_id, track in self._tracks.items() if now_sec - track.last_seen_sec > self._forget_after_sec
        }
        if not stale_ids:
            return

        for stable_id in stale_ids:
            del self._tracks[stable_id]
        self._stable_id_by_raw_id = {
            raw_id: stable_id for raw_id, stable_id in self._stable_id_by_raw_id.items() if stable_id not in stale_ids
        }


def _get_center_and_height(xyxy: tuple[float, float, float, float]) -> tuple[tuple[float, float], float]:
    x1, y1, x2, y2 = xyxy
    return ((x1 + x2) / 2, (y1 + y2) / 2), y2 - y1
