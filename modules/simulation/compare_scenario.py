"""비교 시나리오 — TOML 한 장을 읽어 검증한 CompareScenario 와 브라우저 배치 dict·정답 dict 를 만든다 (SIM-04).

TOML 모양 (모르는 키는 거부):
    name         파일 stem 과 같은 id
    description  한 문장 설명
    mission      WavePilot 루트 기준 상대 경로 (예 "missions/compare.toml")
    battery_pct  선택, 시작 배터리 0 < x <= 100
    [[actors]]   id, model ("soldier" | "car"), north_m, east_m, heading_deg, walk_m(선택, 기본 0),
                 shirt ("red" | "other"), shirt_rgb ([r, g, b]) — shirt·shirt_rgb 는 soldier 만

축 규약: 북·동 m 는 홈 기준이고, 브라우저는 dx = east, dz = −north 다 (drone_camera.html 규약).
기존 modules/simulation/scenarios/*.toml(mock 월드)과는 다른 형식이다.
표준 라이브러리만 import 한다 (import 경계).
"""

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"  # WavePilot/scenarios
SCENARIO_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
ACTOR_MODELS = ("soldier", "car")
SHIRT_COLORS = ("red", "other")

_ACTOR_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")

_TOP_KEYS_REQUIRED = ("name", "description", "mission", "actors")
_TOP_KEYS_OPTIONAL = ("battery_pct",)

# 모델별 배우 허용 키 — 필수 키 검사와 모르는 키 거부를 이 표 하나로 한다
_ACTOR_KEYS_COMMON = ("id", "model", "north_m", "east_m", "heading_deg")
_ACTOR_KEYS_REQUIRED: dict[str, tuple[str, ...]] = {
    "soldier": _ACTOR_KEYS_COMMON + ("shirt", "shirt_rgb"),
    "car": _ACTOR_KEYS_COMMON,
}
_ACTOR_KEYS_OPTIONAL = ("walk_m",)

_BROWSER_MODEL_FILES = {"soldier": "Soldier.glb", "car": "ferrari.glb"}
_TRUTH_KINDS = {"soldier": "person", "car": "car"}


@dataclass(frozen=True, slots=True)
class ScenarioActor:
    actor_id: str  # 시나리오 안에서 유일, ^[a-z0-9_-]{1,32}$
    model: str  # "soldier" | "car"
    north_m: float  # 홈 기준 (finite)
    east_m: float
    heading_deg: float
    walk_m: float  # >= 0, car 는 0
    shirt: str | None  # soldier 필수 "red" | "other", car 는 None
    shirt_rgb: tuple[int, int, int] | None  # soldier 필수 0..255 정수 3개, car 는 None


@dataclass(frozen=True, slots=True)
class CompareScenario:
    scenario_id: str  # 파일 stem 과 같다 (name 키)
    description: str
    mission_path: str  # WavePilot 루트 기준 상대 경로
    battery_pct: float | None  # 선택, 0 < x <= 100
    actors: tuple[ScenarioActor, ...]  # 1개 이상


def parse_compare_scenario(data: dict, scenario_id: str) -> CompareScenario:
    if not isinstance(data, Mapping):
        raise ValueError(f"scenario must be a table, got {data!r}")

    _validate_keys("", data, _TOP_KEYS_REQUIRED, _TOP_KEYS_OPTIONAL)

    name = data["name"]
    if not isinstance(name, str) or name != scenario_id:
        raise ValueError(f"field 'name' must be {scenario_id!r} (file stem), got {name!r}")

    description = data["description"]
    if not isinstance(description, str):
        raise ValueError(f"field 'description' must be a string, got {description!r}")

    mission_path = _read_mission_path(data["mission"])

    battery_pct = None
    if "battery_pct" in data:
        battery_pct = _require_finite_number("battery_pct", data["battery_pct"])
        if not 0 < battery_pct <= 100:
            raise ValueError(f"field 'battery_pct' must be in (0, 100], got {battery_pct!r}")

    actors = _read_actors(data["actors"])

    return CompareScenario(
        scenario_id=scenario_id,
        description=description,
        mission_path=mission_path,
        battery_pct=battery_pct,
        actors=actors,
    )


def load_compare_scenario(scenario_id: str, scenarios_dir: Path = SCENARIOS_DIR) -> CompareScenario:
    # 파일을 열기 전에 id 를 본다 — '..'·'/' 로 scenarios/ 밖 파일을 읽지 못하게 (경로 조작 방지)
    if not isinstance(scenario_id, str) or not SCENARIO_ID_PATTERN.fullmatch(scenario_id):
        raise ValueError(f"scenario id must match {SCENARIO_ID_PATTERN.pattern}, got {scenario_id!r}")

    # 파일 없음(FileNotFoundError)은 잡지 않는다 — 호출 쪽이 그대로 받는다
    path = Path(scenarios_dir) / f"{scenario_id}.toml"
    with open(path, "rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"invalid scenario file {os.fspath(path)}: {error}") from error

    return parse_compare_scenario(data, scenario_id)


def list_scenario_ids(scenarios_dir: Path = SCENARIOS_DIR) -> tuple[str, ...]:
    scenarios_dir = Path(scenarios_dir)
    if not scenarios_dir.is_dir():
        return ()

    stem_list = [path.stem for path in scenarios_dir.glob("*.toml") if SCENARIO_ID_PATTERN.fullmatch(path.stem)]
    return tuple(sorted(stem_list))


def scenario_to_browser_dict(scenario: CompareScenario) -> dict:
    return {
        "id": scenario.scenario_id,
        "battery_pct": scenario.battery_pct,
        "actors": [_get_browser_actor(actor) for actor in scenario.actors],
    }


def build_ground_truth(scenario: CompareScenario) -> dict:
    return {
        "scenario": scenario.scenario_id,
        "mission": scenario.mission_path,
        "actors": [_get_truth_actor(actor) for actor in scenario.actors],
        "red_ids": [actor.actor_id for actor in scenario.actors if actor.shirt == "red"],
    }


# --- 모듈 내부 도우미 ---


def _validate_keys(
    prefix: str, table: Mapping, required_keys: tuple[str, ...], optional_keys: tuple[str, ...]
) -> None:
    # 모르는 키를 먼저 본다 — 오타가 "required" 보다 먼저 드러나게
    for key in table:
        if key not in required_keys and key not in optional_keys:
            raise ValueError(f"field '{prefix}{key}' is not allowed")

    for key in required_keys:
        if key not in table:
            raise ValueError(f"field '{prefix}{key}' is required")


def _read_mission_path(value: object) -> str:
    # 06-05 러너가 이 경로를 실행 인자로 쓴다 — 절대 경로·'..' 로 WavePilot 밖을 가리키지 못하게
    is_valid = isinstance(value, str) and bool(value)
    if is_valid:
        path = PurePosixPath(value)
        is_valid = not path.is_absolute() and ".." not in path.parts and "\\" not in value

    if not is_valid:
        raise ValueError(f"field 'mission' must be a relative path inside WavePilot, got {value!r}")

    return value


def _read_actors(value: object) -> tuple[ScenarioActor, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"field 'actors' must be a non-empty list of tables, got {value!r}")

    actor_list: list[ScenarioActor] = []
    seen_ids: set[str] = set()
    for index, table in enumerate(value):
        actor = _read_actor(index, table)
        if actor.actor_id in seen_ids:
            raise ValueError(f"field 'actors[{index}].id' must be unique, got {actor.actor_id!r}")

        seen_ids.add(actor.actor_id)
        actor_list.append(actor)

    return tuple(actor_list)


def _read_actor(index: int, table: object) -> ScenarioActor:
    prefix = f"actors[{index}]"
    if not isinstance(table, Mapping):
        raise ValueError(f"field '{prefix}' must be a table, got {table!r}")

    # model 을 먼저 읽는다 — 허용 키 표가 모델마다 다르다 (car 의 shirt 키는 여기서 걸린다)
    if "model" not in table:
        raise ValueError(f"field '{prefix}.model' is required")

    model = table["model"]
    if model not in ACTOR_MODELS:
        raise ValueError(f"field '{prefix}.model' must be one of {ACTOR_MODELS}, got {model!r}")

    _validate_keys(f"{prefix}.", table, _ACTOR_KEYS_REQUIRED[model], _ACTOR_KEYS_OPTIONAL)

    actor_id = table["id"]
    if not isinstance(actor_id, str) or not _ACTOR_ID_PATTERN.fullmatch(actor_id):
        raise ValueError(f"field '{prefix}.id' must be a string matching {_ACTOR_ID_PATTERN.pattern}, got {actor_id!r}")

    north_m = _require_finite_number(f"{prefix}.north_m", table["north_m"])
    east_m = _require_finite_number(f"{prefix}.east_m", table["east_m"])
    heading_deg = _require_finite_number(f"{prefix}.heading_deg", table["heading_deg"])

    walk_m = _require_finite_number(f"{prefix}.walk_m", table.get("walk_m", 0.0))
    if walk_m < 0:
        raise ValueError(f"field '{prefix}.walk_m' must be >= 0, got {walk_m!r}")
    if model == "car" and walk_m != 0:
        raise ValueError(f"field '{prefix}.walk_m' must be 0 for car, got {walk_m!r}")

    shirt = None
    shirt_rgb = None
    if model == "soldier":
        shirt = table["shirt"]
        if shirt not in SHIRT_COLORS:
            raise ValueError(f"field '{prefix}.shirt' must be one of {SHIRT_COLORS}, got {shirt!r}")
        shirt_rgb = _read_rgb(f"{prefix}.shirt_rgb", table["shirt_rgb"])

    return ScenarioActor(
        actor_id=actor_id,
        model=model,
        north_m=north_m,
        east_m=east_m,
        heading_deg=heading_deg,
        walk_m=walk_m,
        shirt=shirt,
        shirt_rgb=shirt_rgb,
    )


def _read_rgb(name: str, value: object) -> tuple[int, int, int]:
    is_valid = (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(channel, int) and not isinstance(channel, bool) and 0 <= channel <= 255 for channel in value)
    )
    if not is_valid:
        raise ValueError(f"field '{name}' must be [r, g, b] with integers 0..255, got {value!r}")

    red, green, blue = value
    return red, green, blue


def _require_finite_number(name: str, value: object) -> float:
    # bool 은 int 하위 타입이라 따로 막는다 (mission_file._require_finite_number 와 같은 규칙)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")

    return float(value)


def _get_browser_actor(actor: ScenarioActor) -> dict:
    shirt_color = None
    if actor.shirt_rgb is not None:
        red, green, blue = actor.shirt_rgb
        shirt_color = (red << 16) | (green << 8) | blue

    return {
        "id": actor.actor_id,
        "model": _BROWSER_MODEL_FILES[actor.model],
        "dx": actor.east_m,
        "dz": -actor.north_m,
        "headingDeg": actor.heading_deg,
        "walkM": actor.walk_m,
        "shirtColor": shirt_color,
    }


def _get_truth_actor(actor: ScenarioActor) -> dict:
    walk = None
    if actor.walk_m > 0:
        walk = {"heading_deg": actor.heading_deg, "length_m": actor.walk_m}

    return {
        "id": actor.actor_id,
        "kind": _TRUTH_KINDS[actor.model],
        "color": actor.shirt,
        "north_m": actor.north_m,
        "east_m": actor.east_m,
        "walk": walk,
    }
