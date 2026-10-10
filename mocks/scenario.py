"""시나리오 파일(TOML) 로더 — mock 월드의 정답(ground truth).

파일 형식:
    name = "checkpoint"            # 문자열
    duration_sec = 40.0            # > 0
    description = "..."            # 선택
    [[objects]]
    id = "veh-1"                   # 파일 안에서 유일
    kind = "car"                   # COCO_CLASS_IDS 키만 (person, car, bus, truck)
    size_m = [4.5, 1.8, 1.5]       # [길이, 폭, 높이], 모두 > 0
    confidence = 0.9               # 선택, (0, 1], 기본 DEFAULT_CONFIDENCE
    waypoints = [ {t = 0.0, north = 30.0, east = -20.0}, ... ]   # 1개 이상, t >= 0, 비감소

박스는 축 정렬이다 — 길이는 north 축, 폭은 east 축에 놓는다. Phase 1 호버 yaw 가 0 이라
객체 진행 방향(heading)을 무시해도 화면 박스 크기 차이가 작아서 heading 필드를 두지 않는다.

파일은 사용자가 넣는 입력이므로 이 경계에서 모든 필드의 타입·범위를 검사하고,
틀리면 어느 필드인지 이름을 붙여 ValueError 를 낸다 (T-01-04). tomllib 은 코드를 실행하지 않고,
kind 를 COCO 키로 제한해 자유 문자열이 LLM 프롬프트까지 흘러가지 않게 한다.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from interfaces.detection import COCO_CLASS_IDS

DEFAULT_CONFIDENCE = 0.9  # 잘 보이는 대상에 대한 YOLO 의 전형적인 점수 수준


@dataclass(frozen=True, slots=True)
class Waypoint:
    t_sec: float
    north_m: float
    east_m: float


@dataclass(frozen=True, slots=True)
class GroundTruthObject:
    object_id: str
    kind: str
    track_id: int
    north_m: float
    east_m: float


@dataclass(frozen=True, slots=True)
class ScenarioObject:
    object_id: str
    kind: str
    size_m: tuple[float, float, float]
    confidence: float
    track_id: int
    waypoints: tuple[Waypoint, ...]

    def position_at(self, time_sec: float) -> tuple[float, float]:
        """(north, east). 웨이포인트 사이는 선형 보간, 처음 전·마지막 후에는 끝 점에 머문다."""
        first, last = self.waypoints[0], self.waypoints[-1]
        if time_sec <= first.t_sec:
            return (first.north_m, first.east_m)
        if time_sec >= last.t_sec:
            return (last.north_m, last.east_m)

        for start, end in zip(self.waypoints, self.waypoints[1:]):
            if start.t_sec <= time_sec <= end.t_sec:
                return _interpolate(start, end, time_sec)

        # 위 범위 검사로 여기에 오지 않는다 — 방어적으로 마지막 점
        return (last.north_m, last.east_m)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    duration_sec: float
    objects: tuple[ScenarioObject, ...]
    description: str = ""

    def ground_truth_at(self, time_sec: float) -> tuple[GroundTruthObject, ...]:
        """평가용 정답 — 객체마다 time_sec 시점의 월드 위치."""
        truth: list[GroundTruthObject] = []
        for obj in self.objects:
            north_m, east_m = obj.position_at(time_sec)
            truth.append(
                GroundTruthObject(
                    object_id=obj.object_id,
                    kind=obj.kind,
                    track_id=obj.track_id,
                    north_m=north_m,
                    east_m=east_m,
                )
            )
        return tuple(truth)


def load_scenario(path: str | Path) -> Scenario:
    """TOML 시나리오를 읽고 검증한다. track_id 는 파일 순서대로 1..N."""
    path = Path(path)
    with path.open("rb") as file:
        data = tomllib.load(file)

    name = data.get("name", path.stem)
    if not isinstance(name, str) or not name:
        raise ValueError(f"scenario {path.stem}: name must be a non-empty string")
    prefix = f"scenario {name}"

    duration_sec = _require_number(data.get("duration_sec"), f"{prefix}: duration_sec")
    if duration_sec <= 0:
        raise ValueError(f"{prefix}: duration_sec must be > 0, got {duration_sec}")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{prefix}: description must be a string")

    raw_objects = data.get("objects")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise ValueError(f"{prefix}: objects must be a non-empty array of tables")

    objects: list[ScenarioObject] = []
    seen_ids: set[str] = set()
    for index, raw_object in enumerate(raw_objects):
        obj = _parse_object(raw_object, track_id=index + 1, where=f"{prefix}: objects[{index}]")
        if obj.object_id in seen_ids:
            raise ValueError(f"{prefix}: objects[{index}].id duplicates {obj.object_id!r}")
        seen_ids.add(obj.object_id)
        objects.append(obj)

    return Scenario(name=name, duration_sec=duration_sec, objects=tuple(objects), description=description)


# --- 모듈 내부 도우미 ---


def _interpolate(start: Waypoint, end: Waypoint, time_sec: float) -> tuple[float, float]:
    span_sec = end.t_sec - start.t_sec
    if span_sec <= 0:
        # 같은 시각 웨이포인트 두 개 = 순간 이동, 뒤쪽을 따른다
        return (end.north_m, end.east_m)

    ratio = (time_sec - start.t_sec) / span_sec
    return (
        start.north_m + (end.north_m - start.north_m) * ratio,
        start.east_m + (end.east_m - start.east_m) * ratio,
    )


def _parse_object(raw: object, track_id: int, where: str) -> ScenarioObject:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a table")

    object_id = raw.get("id")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError(f"{where}.id must be a non-empty string")

    kind = raw.get("kind")
    if kind not in COCO_CLASS_IDS:
        raise ValueError(f"{where}.kind must be one of {sorted(COCO_CLASS_IDS)}, got {kind!r}")

    size_m = raw.get("size_m")
    is_size_valid = (
        isinstance(size_m, list)
        and len(size_m) == 3
        and all(_is_number(item) and item > 0 for item in size_m)
    )
    if not is_size_valid:
        raise ValueError(f"{where}.size_m must be [length, width, height] all > 0, got {size_m!r}")

    confidence = raw.get("confidence", DEFAULT_CONFIDENCE)
    if not _is_number(confidence) or not 0.0 < confidence <= 1.0:
        raise ValueError(f"{where}.confidence must be in (0, 1], got {confidence!r}")

    return ScenarioObject(
        object_id=object_id,
        kind=kind,
        size_m=tuple(float(item) for item in size_m),
        confidence=float(confidence),
        track_id=track_id,
        waypoints=_parse_waypoints(raw.get("waypoints"), f"{where}.waypoints"),
    )


def _parse_waypoints(raw: object, where: str) -> tuple[Waypoint, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where} must be a non-empty array of {{t, north, east}}")

    waypoints: list[Waypoint] = []
    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_where} must be an inline table {{t, north, east}}")
        waypoint = Waypoint(
            t_sec=_require_number(item.get("t"), f"{item_where}.t"),
            north_m=_require_number(item.get("north"), f"{item_where}.north"),
            east_m=_require_number(item.get("east"), f"{item_where}.east"),
        )
        if waypoint.t_sec < 0:
            raise ValueError(f"{item_where}.t must be >= 0, got {waypoint.t_sec}")
        if waypoints and waypoint.t_sec < waypoints[-1].t_sec:
            raise ValueError(
                f"{where} times must be non-decreasing, got {waypoint.t_sec} after {waypoints[-1].t_sec}"
            )
        waypoints.append(waypoint)

    return tuple(waypoints)


def _is_number(value: object) -> bool:
    # bool 은 int 의 하위 타입이라 따로 막는다
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_number(value: object, label: str) -> float:
    if not _is_number(value):
        raise ValueError(f"{label} must be a number, got {value!r}")
    return float(value)
