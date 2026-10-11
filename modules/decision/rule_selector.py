"""규칙 선택기 — 상태에서 rule_mode 를 결정적으로 고른다 (SEL-03 규칙 쪽, SEL-04).

RuleSelector 는 DecisionModel 구현이다. 단독으로 쓰거나(WAVEPILOT_SELECTOR=rule) d1 실패 때 대체 선택기로 쓴다(SEL-03).
결정적이고 상태를 저장하지 않는다. 보기 필터·파라미터는 d1 선택기와 같은 함수를 쓴다(D-02, D-04).
결과의 fallback_reason 은 항상 "none" 이다 — d1 실패 사유를 덮어쓰는 일은 01-05 FallbackSelector 몫이다.

표 2 — 모드 선택 사다리 (위에서부터 처음 맞는 줄. 고른 모드가 보기에 없으면 다음 줄로 내려간다):
대상은 options.get_target_tracks 의 모드별 대상(방금 실패한 (모드, track) 을 뺀 것)이고, 고른 모드의 params 도 같은 대상을 쓴다.
| 순서 | 조건                                                                                          | rule_mode    |
|------|-----------------------------------------------------------------------------------------------|--------------|
| 1    | aim_and_fire 대상이 있고 가장 가까운 것의 거리 <= 조준 가능 거리(approach_stop_distance_m + AIM_READY_MARGIN_M) | aim_and_fire |
| 2    | aim_and_fire 대상이 있고 가장 가까운 approach 대상이 곧 가장 가까운 aim_and_fire 대상         | approach     |
| 3    | observe 대상이 있고 가장 가까운 observe 대상 거리 <= OBSERVE_READY_DISTANCE_M                  | observe      |
| 4    | approach 대상이 있음                                                                          | approach     |
| 5    | event 가 mode_ended, 직전 모드 search, 결과 failure/timeout (구역을 다 돌았는데 후보 없음)    | return_home  |
|      | (allow_return_home 이 거짓이면 이 줄을 건너뛰어 6줄 search 로 탐색을 다시 시작한다)            |              |
| 6    | 그 밖                                                                                         | search       |
| 마지막 | 위에서 고른 모드가 보기에 없을 때(방어)                                                     | hold         |

2줄 조건: approach 는 가장 가까운 후보로 다가가므로, 더 가까운 색 미상 후보가 있으면 확정 track 이 아니라 그쪽으로
간다. 그 경우는 2줄을 건너뛰어 3·4줄(지켜보기·다가가기)이 그 후보를 다룬다 (01-REVIEW CR-01).

재시도 방지: 보기 필터가 방금 실패한 (모드, track) 을 그 모드의 대상에서 뺀다. 그래서 사다리는 같은 대상에 같은 모드를
곧바로 다시 고르지 않고 다음 대상이나 다음 줄로 자연히 내려간다. 기억은 직전 한 번뿐이라
aim→실패→observe→aim 같은 긴 순환은 막지 못한다.

확률표: 고른 모드에 RULE_CONFIDENCE, 나머지 보기에 (1 - RULE_CONFIDENCE) / (n - 1).
RULE_CONFIDENCE 가 1.0 이라 one-hot 이고, 보기가 1개면 {고른 모드: 1.0}.
"""

import time
from collections.abc import Iterator, Sequence

from modules.decision.interface.selector import FALLBACK_NONE, SOURCE_RULE, Latency, SelectionResult
from modules.decision.interface.state import EVENT_MODE_ENDED, SelectionEvent, SelectionState
from modules.decision.options import (
    FAILED_OUTCOMES,
    filter_options,
    get_aim_ready_distance_m,
    get_horizontal_distance_m,
    get_nearest_track,
    get_target_tracks,
)
from modules.decision.params import ParamDefaults, fill_params

OBSERVE_READY_DISTANCE_M = 15.0  # 이 안의 색 미확정 후보는 다가가지 않고 지켜본다 (ASSUMED, 실측 후 조정)
RULE_CONFIDENCE = 1.0  # 규칙은 결정적이라 고른 모드에 확률 전부를 준다
SAFE_MODE = "hold"  # 필터가 늘 남기는 모드 — 사다리 끝의 방어 선택


class RuleSelector:
    def __init__(self, defaults: ParamDefaults, allow_return_home: bool = True) -> None:
        self._defaults = defaults
        self._allow_return_home = allow_return_home

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        started_at = time.perf_counter()

        options = filter_options(state, event, self._defaults.approach_stop_distance_m, self._allow_return_home)
        rule_mode = self._choose_mode(state, event, options)
        params = fill_params(rule_mode, state, event, self._defaults)

        probabilities = _build_probabilities(rule_mode, options, RULE_CONFIDENCE)
        total_ms = (time.perf_counter() - started_at) * 1000.0
        latency = Latency(total_ms=total_ms, orin_processing_ms=None, network_rtt_ms=None)

        return SelectionResult(
            rule_mode=rule_mode,
            params=params,
            confidence=RULE_CONFIDENCE,
            probabilities=probabilities,
            source=SOURCE_RULE,
            fallback_reason=FALLBACK_NONE,
            options=options,
            latency=latency,
            input_tokens=None,
        )

    def _choose_mode(self, state: SelectionState, event: SelectionEvent, options: Sequence[str]) -> str:
        for rule_mode in self._get_ladder_modes(state, event):
            if rule_mode in options:
                return rule_mode

        return SAFE_MODE

    def _get_ladder_modes(self, state: SelectionState, event: SelectionEvent) -> Iterator[str]:
        """표 2 의 1~6줄 중 조건이 맞는 줄의 모드를 위에서부터 낸다."""
        # TODO(phase-4): 실패 누적 기억 — (mode, target) 실패 횟수를 상태에 두고 N회 넘은 대상은 제외 (01-REVIEW WR-01)
        drone = state.drone
        nearest_aim_target = get_nearest_track(drone, get_target_tracks("aim_and_fire", state, event))
        nearest_approach_target = get_nearest_track(drone, get_target_tracks("approach", state, event))
        nearest_observe_target = get_nearest_track(drone, get_target_tracks("observe", state, event))

        if nearest_aim_target is not None:
            aim_ready_distance_m = get_aim_ready_distance_m(self._defaults.approach_stop_distance_m)
            if get_horizontal_distance_m(drone, nearest_aim_target) <= aim_ready_distance_m:
                yield "aim_and_fire"
            # approach 의 실제 대상이 확정 track 일 때만 — 더 가까운 색 미상 후보로 새지 않게 (CR-01)
            if nearest_approach_target is nearest_aim_target:
                yield "approach"

        is_observe_ready = (
            nearest_observe_target is not None
            and get_horizontal_distance_m(drone, nearest_observe_target) <= OBSERVE_READY_DISTANCE_M
        )
        if is_observe_ready:
            yield "observe"
        if nearest_approach_target is not None:
            yield "approach"

        # 복귀 선택을 끈 임무는 구역을 다 돌아도 search 로 다시 훑는다 — 강제 복귀는 안전 감시 몫
        if self._allow_return_home and _is_search_exhausted(event):
            yield "return_home"
        yield "search"


# --- 모듈 내부 도우미 ---


def _is_search_exhausted(event: SelectionEvent) -> bool:
    mode_result = event.mode_result
    return (
        event.kind == EVENT_MODE_ENDED
        and mode_result is not None
        and mode_result.rule_mode == "search"
        and mode_result.outcome in FAILED_OUTCOMES
    )


def _build_probabilities(chosen_key: str, keys: Sequence[str], confidence: float) -> dict[str, float]:
    # llm_report/decision.py 의 _build_probabilities 를 복사했다 — import 경계 때문에 import 하지 않는다
    if len(keys) == 1:
        return {chosen_key: 1.0}

    remainder = (1.0 - confidence) / (len(keys) - 1)
    return {key: (confidence if key == chosen_key else remainder) for key in keys}
