"""보고 LLM 프롬프트 — 고정 system 규칙과 상태 JSON 만 담은 user 메시지.

system 은 매 호출 바이트 단위로 같아야 서버 프롬프트 캐시가 접두부를 재사용한다.
그래서 시각처럼 매번 바뀌는 값을 넣지 않는다. user 메시지에는 지시문을 섞지 않는다 —
상태 안의 문자열이 지시로 읽히지 않게(프롬프트 주입) 지시는 system 에만 둔다.
예시 ID(EX-1, EX-2)는 실제 형식(T3)과 일부러 다르게 써서, 모델이 베끼면 스키마 enum 과
parse_report 가 이중으로 잡게 한다.
"""

import json

from llm_module.state import StateJson

SYSTEM_PROMPT = (
    "You write a short situation report for a drone operator from a JSON state.\n"
    "Rules:\n"
    "1. Output only the JSON object required by the schema.\n"
    "2. Every sentence must cite the track_ids it is based on. Cite only IDs present in the input.\n"
    "3. State only what the input shows. Do not invent positions, counts, or intent.\n"
    "4. Do not judge threat level and do not recommend force. A 'suggestion' may only propose "
    "that the operator look closer, with the evidence.\n"
    "5. If the input is uncertain (low confidence, few observations), say so.\n"
    "6. Positions are meters north/east of the mission origin (position_ned_m). Track IDs look like T3.\n"
    "7. A track with is_active false is not currently seen; describe it as last seen at last_seen_sec. "
    "If omitted_track_count is above 0, say that many more tracks were not included.\n"
    "Example (IDs are placeholders):\n"
    '{"sentences":[{"kind":"observation","text":"Two vehicles are moving north together.",'
    '"track_ids":["EX-1","EX-2"]}]}\n'
)


def build_user_prompt(state: StateJson) -> str:
    # sort_keys + 고정 구분자: 같은 상태는 같은 바이트가 되어 프롬프트 캐시와 재현성에 유리하다
    return json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
