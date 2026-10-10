"""d1-3B 질문 문구·상태 v2 직렬화 테스트 (D-03, D-08, D-09). 네트워크를 쓰지 않는다."""

import json

import pytest

from modules.decision.interface import (
    EVENT_MODE_ENDED,
    EVENT_PERIODIC_CHECK,
    RULE_MODE_DEFINITIONS,
    RULE_MODES,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
    build_request_state,
)
from modules.decision.prompt import (
    QUESTION_ID_RULE_MODE,
    RULE_MODE_INSTRUCTIONS,
    build_rule_mode_question,
    serialize_request_state,
)

REQUEST_KEY_ORDER = ["schema_version", "time_sec", "event", "drone", "tracks", "mission"]
FEW_SHOT_MARKERS = ("example", "e.g.", "for instance")


def make_selection_state() -> SelectionState:
    # drone 1개, person track 1개, 임무 대상 person — 01-01 실제 생성자 그대로
    drone = DroneState(
        north_m=1.23,
        east_m=-2.04,
        down_m=-40.0,
        altitude_m=40.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=90.0,
        gimbal_pitch_deg=-55.0,
        gimbal_yaw_deg=0.0,
        battery_pct=91.26,
        flight_mode="hover",
    )
    track = TrackState(
        track_id="T1",
        class_name="person",
        north_m=30.0,
        east_m=-6.0,
        first_seen_sec=7.5,
        last_seen_sec=12.0,
        observation_count=9,
        mean_confidence=0.714,
        is_active=True,
        upper_color=None,
    )
    mission = MissionSummary(target_class="person", target_upper_color=None, hearted_tracks=())

    return SelectionState(time_sec=12.04, drone=drone, tracks=(track,), mission=mission)


PERIODIC_EVENT = SelectionEvent(EVENT_PERIODIC_CHECK, None)
MODE_ENDED_EVENT = SelectionEvent(EVENT_MODE_ENDED, ModeResult("search", "success", None))


# --- build_rule_mode_question ---


def test_full_menu_criteria_follow_rule_modes_and_definitions() -> None:
    # 전체 보기: criteria 키 순서는 RULE_MODES, 값은 정의 상수 그대로
    _, criteria = build_rule_mode_question(RULE_MODES)

    assert list(criteria) == list(RULE_MODES)
    for mode in RULE_MODES:
        assert criteria[mode] == RULE_MODE_DEFINITIONS[mode]


def test_partial_options_send_only_given_modes() -> None:
    # 규칙이 걸러 준 보기만 d1 에 간다 (D-02)
    _, criteria = build_rule_mode_question(("hold", "return_home"))

    assert list(criteria) == ["hold", "return_home"]


def test_option_order_is_preserved_not_sorted() -> None:
    _, criteria = build_rule_mode_question(("return_home", "hold", "approach"))

    assert list(criteria) == ["return_home", "hold", "approach"]


def test_instructions_are_one_constant_without_few_shot() -> None:
    # 보기와 무관한 상수 한 문장, 퓨샷 표현 없음 (D-09)
    full_instructions, _ = build_rule_mode_question(RULE_MODES)
    partial_instructions, _ = build_rule_mode_question(("hold",))

    assert full_instructions == RULE_MODE_INSTRUCTIONS
    assert partial_instructions == RULE_MODE_INSTRUCTIONS
    assert RULE_MODE_INSTRUCTIONS.strip()
    lowered = RULE_MODE_INSTRUCTIONS.lower()
    for marker in FEW_SHOT_MARKERS:
        assert marker not in lowered


def test_instructions_do_not_list_mode_names() -> None:
    # 정의·이름은 criteria 에만 둔다 (RESEARCH §2.1)
    for mode in RULE_MODES:
        assert mode not in RULE_MODE_INSTRUCTIONS


def test_question_id_is_rule_mode() -> None:
    assert QUESTION_ID_RULE_MODE == "rule_mode"


@pytest.mark.parametrize(
    "options",
    [(), ("fly_away",), ("hold", "fly_away"), ("hold", "hold")],
    ids=["empty", "unknown", "mixed_unknown", "duplicate"],
)
def test_invalid_options_raise_value_error(options: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        build_rule_mode_question(options)


def test_returned_criteria_is_a_new_dict() -> None:
    # 반환값을 바꿔도 정의 상수는 그대로여야 한다
    original_definition = RULE_MODE_DEFINITIONS["hold"]
    _, criteria = build_rule_mode_question(("hold",))

    criteria["hold"] = "changed"
    criteria["search"] = "added"

    assert RULE_MODE_DEFINITIONS["hold"] == original_definition
    assert set(RULE_MODE_DEFINITIONS) == set(RULE_MODES)


# --- serialize_request_state ---


def test_serialized_state_is_compact_json_of_request_state() -> None:
    state = make_selection_state()

    text = serialize_request_state(state, PERIODIC_EVENT)

    assert text == json.dumps(build_request_state(state, PERIODIC_EVENT), separators=(",", ":"))
    assert ", " not in text
    assert ": " not in text


def test_serialized_state_keeps_fixed_key_order() -> None:
    # sort_keys 를 쓰지 않아 01-01 고정 키 순서가 유지된다
    text = serialize_request_state(make_selection_state(), PERIODIC_EVENT)

    assert list(json.loads(text)) == REQUEST_KEY_ORDER


def test_serialized_state_is_deterministic() -> None:
    first = serialize_request_state(make_selection_state(), MODE_ENDED_EVENT)
    second = serialize_request_state(make_selection_state(), MODE_ENDED_EVENT)

    assert first.encode("utf-8") == second.encode("utf-8")


@pytest.mark.parametrize("event", [MODE_ENDED_EVENT, PERIODIC_EVENT], ids=["mode_ended", "periodic_check"])
def test_both_event_kinds_serialize(event: SelectionEvent) -> None:
    decoded = json.loads(serialize_request_state(make_selection_state(), event))

    assert decoded["event"] == event.to_dict()
