"""규칙 선택기 — 상태에서 rule_mode 를 결정적으로 고른다 (SEL-03 규칙 쪽, SEL-04).

RuleSelector 는 DecisionModel 구현이다. 단독으로 쓰거나(WAVEPILOT_SELECTOR=rule) d1 실패 때 대체 선택기로 쓴다(SEL-03).
결정적이고 상태를 저장하지 않는다. 보기 필터·파라미터는 d1 선택기와 같은 함수를 쓴다(D-02, D-04).
결과의 fallback_reason 은 항상 "none" 이다 — d1 실패 사유를 덮어쓰는 일은 01-05 FallbackSelector 몫이다.

표 2 — 모드 선택 사다리 (위에서부터 처음 맞는 줄. 고른 모드가 보기에 없으면 다음 줄로 내려간다):
| 순서 | 조건                                                                                    | rule_mode    |
|------|-----------------------------------------------------------------------------------------|--------------|
| 1    | 확정 track 이 있고 가장 가까운 확정 track 거리 <= approach_stop_distance_m + AIM_READY_MARGIN_M | aim_and_fire |
| 2    | 확정 track 이 있음 (더 멀리 있음)                                                        | approach     |
| 3    | 후보 track 이 있고 가장 가까운 후보 거리 <= OBSERVE_READY_DISTANCE_M                      | observe      |
| 4    | 후보 track 이 있음                                                                       | approach     |
| 5    | event 가 mode_ended, 직전 모드 search, 결과 failure/timeout (구역을 다 돌았는데 후보 없음) | return_home  |
| 6    | 그 밖                                                                                    | search       |
| 마지막 | 위에서 고른 모드가 보기에 없을 때(방어)                                               | hold         |

재시도 방지 규칙(사다리 뒤에 적용): event 가 mode_ended 이고 결과가 failure/timeout 이며, 사다리가 고른
(rule_mode, params["target_track"]) 이 직전 (rule_mode, target_track) 과 같으면 hold 로 바꾼다.
같은 대상에 같은 모드로 실패를 반복하지 않고, 다음 periodic_check 에서 다시 판단한다.

확률표: 고른 모드에 RULE_CONFIDENCE, 나머지 보기에 (1 - RULE_CONFIDENCE) / (n - 1).
RULE_CONFIDENCE 가 1.0 이라 one-hot 이고, 보기가 1개면 {고른 모드: 1.0}.
"""

import time
from collections.abc import Iterator, Sequence

from modules.decision.interface.menu import TARGET_MODES
from modules.decision.interface.selector import FALLBACK_NONE, SOURCE_RULE, Latency, SelectionResult
from modules.decision.interface.state import EVENT_MODE_ENDED, SelectionEvent, SelectionState
from modules.decision.options import (
    filter_options,
    get_candidate_tracks,
    get_confirmed_tracks,
    get_horizontal_distance_m,
    get_nearest_track,
)
from modules.decision.params import Params, ParamDefaults, fill_params

AIM_READY_MARGIN_M = 2.0  # 접근 정지 거리보다 이만큼 멀어도 바로 조준한다 (ASSUMED, 실측 후 조정)
OBSERVE_READY_DISTANCE_M = 15.0  # 이 안의 색 미확정 후보는 다가가지 않고 지켜본다 (ASSUMED, 실측 후 조정)
RULE_CONFIDENCE = 1.0  # 규칙은 결정적이라 고른 모드에 확률 전부를 준다

FAILED_OUTCOMES = ("failure", "timeout")
SAFE_MODE = "hold"  # 필터가 늘 남기는 모드 — 사다리 끝의 방어 선택이자 재시도 방지 대체


class RuleSelector:
    def __init__(self, defaults: ParamDefaults) -> None:
        self._defaults = defaults

    def select(self, state: SelectionState, event: SelectionEvent) -> SelectionResult:
        started_at = time.perf_counter()

        options = filter_options(state, event)
        rule_mode = self._choose_mode(state, event, options)
        params = fill_params(rule_mode, state, event, self._defaults)

        if _is_repeated_failure(event, rule_mode, params):
            rule_mode = SAFE_MODE
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
        nearest_confirmed = get_nearest_track(state.drone, get_confirmed_tracks(state, event))
        if nearest_confirmed is not None:
            aim_ready_distance_m = self._defaults.approach_stop_distance_m + AIM_READY_MARGIN_M
            if get_horizontal_distance_m(state.drone, nearest_confirmed) <= aim_ready_distance_m:
                yield "aim_and_fire"
            yield "approach"

        nearest_candidate = get_nearest_track(state.drone, get_candidate_tracks(state, event))
        if nearest_candidate is not None:
            if get_horizontal_distance_m(state.drone, nearest_candidate) <= OBSERVE_READY_DISTANCE_M:
                yield "observe"
            yield "approach"

        if _is_search_exhausted(event):
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


def _is_repeated_failure(event: SelectionEvent, rule_mode: str, params: Params) -> bool:
    mode_result = event.mode_result
    return (
        event.kind == EVENT_MODE_ENDED
        and mode_result is not None
        and mode_result.outcome in FAILED_OUTCOMES
        and rule_mode == mode_result.rule_mode
        and rule_mode in TARGET_MODES
        and params.get("target_track") == mode_result.target_track
    )


def _build_probabilities(chosen_key: str, keys: Sequence[str], confidence: float) -> dict[str, float]:
    # modules.llm_report.decision._build_probabilities 를 복사했다 — import 경계 때문에 import 하지 않는다
    if len(keys) == 1:
        return {chosen_key: 1.0}

    remainder = (1.0 - confidence) / (len(keys) - 1)
    return {key: (confidence if key == chosen_key else remainder) for key in keys}
