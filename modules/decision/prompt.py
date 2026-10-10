"""d1-3B 에 보내는 질문 문구와 상태 문자열을 한 곳에서 만든다.

- 질문은 choice 1개다. d1-3B 는 rule_mode 이름만 고르고, 파라미터는 규칙이 채운다(D-03).
- 모드 정의는 interface/menu.py 의 RULE_MODE_DEFINITIONS 한 곳에서만 가져와 criteria 값에 넣는다(D-09).
  /v1/systemone 의 criteria 가 {보기: 설명} 형식이라 instructions 에 정의를 늘어놓지 않는다(RESEARCH §2.1).
- 퓨샷 예시는 넣지 않는다 — 토큰·지연이 늘고 Phase 6 비교 평가를 오염시킬 수 있다.
- state 는 dict 가 아닌 문자열로 보낸다. 서버 직렬화 순서에 기대지 않고 01-01 의 고정 키 순서와
  "같은 상태는 같은 바이트" 규약을 지키기 위해서다(RESEARCH Pitfall 4).
"""

import json

from modules.decision.interface import (
    RULE_MODE_DEFINITIONS,
    RULE_MODES,
    SelectionEvent,
    SelectionState,
    build_request_state,
)

QUESTION_ID_RULE_MODE = "rule_mode"
# 보기와 무관한 상수 한 문장. 예시·모드 이름을 넣지 않는다 (D-09)
RULE_MODE_INSTRUCTIONS = "Choose the next rule_mode for the drone from the options, given the current state and the last event."
STATE_JSON_SEPARATORS = (",", ":")


def build_rule_mode_question(options: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """보기 튜플로 (instructions, criteria) 를 만든다. criteria 는 options 순서를 그대로 따른다."""
    if not options:
        raise ValueError("options must not be empty")
    unknown_modes = [mode for mode in options if mode not in RULE_MODES]
    if unknown_modes:
        raise ValueError(f"options contain unknown rule_modes {unknown_modes}; allowed {RULE_MODES}")
    if len(set(options)) != len(options):
        raise ValueError(f"options must not contain duplicates, got {options}")

    criteria = {mode: RULE_MODE_DEFINITIONS[mode] for mode in options}

    return RULE_MODE_INSTRUCTIONS, criteria


def serialize_request_state(state: SelectionState, event: SelectionEvent) -> str:
    """요청 상태 v2 를 압축 JSON 문자열로 바꾼다.

    키 순서는 build_request_state 가 정하므로 정렬하지 않는다. 자유 텍스트 금지·반올림 검증도
    build_request_state 책임이라 여기서 다시 하지 않는다.
    """
    request = build_request_state(state, event)

    return json.dumps(request, separators=STATE_JSON_SEPARATORS, allow_nan=False)
