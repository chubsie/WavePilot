"""YOLO 어댑터 테스트용 가짜 객체 — Ultralytics Results/Boxes 의 numpy·torch 없는 대역.

어댑터가 boxes 에서 쓰는 API 는 `.tolist()` 하나뿐이라 평범한 리스트로 충분하다.
FakeYoloModel 은 `track()` 호출마다 인자를 기록하고, 미리 넣어 둔 결과를 큐 순서대로 돌려준다.
tests/fakes.py 는 Phase 1 소유라 일부러 건드리지 않고 이 파일을 따로 둔다.
"""

from dataclasses import dataclass, field

DEFAULT_ORIG_SHAPE = (720, 1280)  # DEFAULT_CAMERA 1280x720 과 맞춘 (h, w)


class FakeTensor(list):
    def tolist(self) -> list:
        return [list(row) if isinstance(row, list) else row for row in self]


@dataclass(frozen=True, slots=True)
class FakeBoxes:
    xyxy: FakeTensor
    cls: FakeTensor
    conf: FakeTensor
    id: FakeTensor | None


def make_boxes(rows: list[tuple[list[float], float, float, float | None]]) -> FakeBoxes:
    """rows 의 각 항목은 (xyxy, class_id, confidence, track_id). track_id 가 전부 None 이면 id 도 None."""
    track_ids = [row[3] for row in rows]
    has_track = any(track_id is not None for track_id in track_ids)
    return FakeBoxes(
        xyxy=FakeTensor([list(row[0]) for row in rows]),
        cls=FakeTensor([float(row[1]) for row in rows]),
        conf=FakeTensor([float(row[2]) for row in rows]),
        id=FakeTensor([float(track_id) for track_id in track_ids]) if has_track else None,
    )


@dataclass(frozen=True, slots=True)
class FakeResult:
    boxes: FakeBoxes | None
    orig_shape: tuple[int, int] = DEFAULT_ORIG_SHAPE  # Ultralytics 순서 (height, width)


@dataclass(frozen=True, slots=True)
class FakeTrackCall:
    image: object
    kwargs: dict


@dataclass
class FakeYoloModel:
    results: list[FakeResult]
    calls: list[FakeTrackCall] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def track(self, source, **kwargs) -> list[FakeResult]:
        self.calls.append(FakeTrackCall(source, dict(kwargs)))
        if not self.results:
            raise AssertionError("FakeYoloModel.track called more times than results were queued")
        return [self.results.pop(0)]
