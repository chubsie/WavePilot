"""모드별 파라미터 채움 — 규칙·d1 두 선택기가 공유한다 (D-03, D-04).

d1 은 모드 이름만 고르고 파라미터는 규칙 코드가 채운다(D-03) — 메뉴 밖 값이 들어올 틈이 없다.
규칙·d1 두 선택기가 이 함수 하나를 공유해 차이가 모드 선택에만 있다(D-04, Phase 6 비교 공정성).
기본값은 ASSUMED 이며 Phase 3~5 실측·임무 파일(Phase 5)로 조정한다.

표 3 — 모드별 파라미터 (키는 이 순서, 정확히 이 집합):
| rule_mode    | 키                                                                    | 값 출처                                                    | 쓰는 phase |
|--------------|-----------------------------------------------------------------------|------------------------------------------------------------|------------|
| search       | timeout_sec                                                           | search_timeout_sec                                         | Phase 3 RM-01 (구역 기본값은 ParamDefaults.search_area_*, Phase 5 임무 파일이 덮어씀) |
| approach     | target_track, stop_distance_m, timeout_sec                            | 가장 가까운 approach 대상, approach_stop_distance_m, approach_timeout_sec | Phase 3 RM-03 |
| aim_and_fire | target_track, center_tolerance_ratio, stable_frame_count, timeout_sec | 가장 가까운 aim_and_fire 대상, aim_center_tolerance_ratio, aim_stable_frame_count, aim_timeout_sec | Phase 4 RM-06, HRT-01 |
| observe      | target_track, timeout_sec                                             | 가장 가까운 observe 대상, observe_timeout_sec              | Phase 4 RM-02 |
| hold         | duration_sec                                                          | hold_duration_sec                                          | Phase 3 RM-04 |
| return_home  | home_north_m, home_east_m, return_altitude_m                          | home_north_m, home_east_m, return_altitude_m               | Phase 3 RM-05 (Phase 5 임무 파일이 덮어씀) |

search 구역·고도·레인 간격(search_area_*, search_altitude_m, search_lane_spacing_m)은 fill_params 키가 아니라
임무 루프가 RuleModeSettings 로 옮기는 기본값이다 (D-05, Phase 5 임무 파일이 덮어씀).

모드별 대상(get_target_tracks — 후보·확정에서 방금 실패한 (모드, track) 을 뺀 것)과 가장 가까운 track 판정은
options.py 와 같은 함수를 쓴다 — 보기 필터와 대상 선택이 어긋나지 않는다.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

from modules.decision.interface.menu import RULE_MODES
from modules.decision.interface.state import SelectionEvent, SelectionState
from modules.decision.options import get_nearest_track, get_target_tracks

MAX_CENTER_TOLERANCE_RATIO = 0.5  # 프레임 폭의 절반을 넘으면 "가운데" 판정이 의미 없다

Params = dict[str, str | int | float | None]


@dataclass(frozen=True, slots=True)
class ParamDefaults:
    search_timeout_sec: float = 120.0
    # search 구역 — 홈 기준 북쪽 사각형, NED m. Phase 5 임무 파일이 덮어씀 (D-05)
    search_area_north_min_m: float = 20.0
    search_area_north_max_m: float = 120.0
    search_area_east_min_m: float = -50.0  # NED 라 음수 허용
    search_area_east_max_m: float = 50.0
    search_altitude_m: float = 15.0
    # 20 m 면 기본 구역 순찰이 125 s 로 search_timeout_sec 120 s 를 넘는다 — 25 m 는 517.5 m / 5 m/s = 103.5 s
    search_lane_spacing_m: float = 25.0
    approach_stop_distance_m: float = 5.0
    approach_timeout_sec: float = 30.0
    aim_center_tolerance_ratio: float = 0.05  # 프레임 폭 대비
    aim_stable_frame_count: int = 5
    aim_timeout_sec: float = 15.0
    observe_timeout_sec: float = 10.0
    hold_duration_sec: float = 5.0
    home_north_m: float = 0.0  # NED 좌표라 음수·0 허용
    home_east_m: float = 0.0
    return_altitude_m: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "search_timeout_sec", "approach_stop_distance_m", "approach_timeout_sec", "aim_timeout_sec",
            "observe_timeout_sec", "hold_duration_sec", "return_altitude_m", "search_altitude_m",
            "search_lane_spacing_m",
        ):
            _require_positive(name, getattr(self, name))

        _require_finite("home_north_m", self.home_north_m)
        _require_finite("home_east_m", self.home_east_m)

        for name in ("search_area_north_min_m", "search_area_north_max_m", "search_area_east_min_m", "search_area_east_max_m"):
            _require_finite(name, getattr(self, name))
        if self.search_area_north_min_m >= self.search_area_north_max_m:
            raise ValueError(
                f"field 'search_area_north_min_m' must be < search_area_north_max_m, "
                f"got {self.search_area_north_min_m!r} >= {self.search_area_north_max_m!r}"
            )
        if self.search_area_east_min_m >= self.search_area_east_max_m:
            raise ValueError(
                f"field 'search_area_east_min_m' must be < search_area_east_max_m, "
                f"got {self.search_area_east_min_m!r} >= {self.search_area_east_max_m!r}"
            )

        frame_count = self.aim_stable_frame_count
        if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count < 1:
            raise ValueError(f"field 'aim_stable_frame_count' must be int >= 1, got {frame_count!r}")

        _require_finite("aim_center_tolerance_ratio", self.aim_center_tolerance_ratio)
        if not 0.0 < self.aim_center_tolerance_ratio <= MAX_CENTER_TOLERANCE_RATIO:
            raise ValueError(
                f"field 'aim_center_tolerance_ratio' must be in (0, {MAX_CENTER_TOLERANCE_RATIO}], "
                f"got {self.aim_center_tolerance_ratio!r}"
            )


PARAM_KEYS_BY_MODE: dict[str, tuple[str, ...]] = {
    "search": ("timeout_sec",),
    "approach": ("target_track", "stop_distance_m", "timeout_sec"),
    "aim_and_fire": ("target_track", "center_tolerance_ratio", "stable_frame_count", "timeout_sec"),
    "observe": ("target_track", "timeout_sec"),
    "hold": ("duration_sec",),
    "return_home": ("home_north_m", "home_east_m", "return_altitude_m"),
}


def fill_params(rule_mode: str, state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    if rule_mode not in RULE_MODES:
        raise ValueError(f"rule_mode must be one of {RULE_MODES}, got {rule_mode!r}")

    builder = _PARAM_BUILDERS_BY_MODE[rule_mode]
    params = builder(state, event, defaults)

    # 키 순서를 표 3 에 맞춘다
    return {key: params[key] for key in PARAM_KEYS_BY_MODE[rule_mode]}


# --- 모듈 내부 도우미 ---


def _build_search_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {"timeout_sec": defaults.search_timeout_sec}


def _build_approach_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {
        "target_track": _get_target_label("approach", state, event),
        "stop_distance_m": defaults.approach_stop_distance_m,
        "timeout_sec": defaults.approach_timeout_sec,
    }


def _build_aim_and_fire_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {
        "target_track": _get_target_label("aim_and_fire", state, event),
        "center_tolerance_ratio": defaults.aim_center_tolerance_ratio,
        "stable_frame_count": defaults.aim_stable_frame_count,
        "timeout_sec": defaults.aim_timeout_sec,
    }


def _build_observe_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {
        "target_track": _get_target_label("observe", state, event),
        "timeout_sec": defaults.observe_timeout_sec,
    }


def _build_hold_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {"duration_sec": defaults.hold_duration_sec}


def _build_return_home_params(state: SelectionState, event: SelectionEvent, defaults: ParamDefaults) -> Params:
    return {
        "home_north_m": defaults.home_north_m,
        "home_east_m": defaults.home_east_m,
        "return_altitude_m": defaults.return_altitude_m,
    }


_PARAM_BUILDERS_BY_MODE: dict[str, Callable[[SelectionState, SelectionEvent, ParamDefaults], Params]] = {
    "search": _build_search_params,
    "approach": _build_approach_params,
    "aim_and_fire": _build_aim_and_fire_params,
    "observe": _build_observe_params,
    "hold": _build_hold_params,
    "return_home": _build_return_home_params,
}


def _get_target_label(rule_mode: str, state: SelectionState, event: SelectionEvent) -> str:
    # 정상 흐름에서는 filter_options 를 거친 모드만 들어오므로 대상이 없을 수 없다
    nearest_track = get_nearest_track(state.drone, get_target_tracks(rule_mode, state, event))
    if nearest_track is None:
        raise ValueError(f"rule_mode {rule_mode!r} needs a target track but none is eligible")

    return nearest_track.track_id


def _require_finite(name: str, value: object) -> None:
    # bool 은 int 의 하위 타입이라 따로 막는다
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"field '{name}' must be a finite number, got {value!r}")


def _require_positive(name: str, value: object) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise ValueError(f"field '{name}' must be > 0, got {value!r}")
