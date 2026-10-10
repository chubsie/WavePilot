"""rule_mode 메뉴 — 선택기가 고를 수 있는 모드 이름과 한 줄 정의.

메뉴 계약은 Phase 1 에서 고정한다(D-01). Phase 3~6 에서 메뉴나 프롬프트를 바꾸지 않는다.
정의 한 줄은 프롬프트(01-04 의 criteria 값)와 문서가 이 상수 하나를 같이 쓴다(D-09).
퓨샷 예시는 두지 않는다 — 토큰·지연이 늘고 Phase 6 비교 평가를 오염시킬 수 있다.
"""

RULE_MODES: tuple[str, ...] = ("search", "approach", "aim_and_fire", "observe", "hold", "return_home")

# 영어 한 문장, 줄바꿈·비ASCII 없음. 키 순서는 RULE_MODES 와 같다
RULE_MODE_DEFINITIONS: dict[str, str] = {
    "search": "patrol the mission area in a zigzag until a candidate person appears.",
    "approach": "fly toward the chosen person track and close to observation distance.",
    "aim_and_fire": "point the gimbal at the chosen person track and send the heart marker.",
    "observe": "hover near the chosen person track and keep it in view to confirm its attributes.",
    "hold": "hover in place for a fixed time without moving.",
    "return_home": "fly back to the home point and land.",
}

# 대상 track 이 있어야 고를 수 있는 모드 — 01-03 보기 필터가 쓴다
TARGET_MODES: tuple[str, ...] = ("approach", "aim_and_fire", "observe")
