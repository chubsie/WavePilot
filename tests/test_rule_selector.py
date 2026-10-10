"""규칙 선택기 테스트 (표 2, SEL-03 규칙 쪽, SEL-04)."""

from modules.decision.interface.menu import RULE_MODES, TARGET_MODES
from modules.decision.interface.selector import FALLBACK_NONE, SOURCE_RULE, DecisionModel, SelectionResult
from modules.decision.interface.state import (
    EVENT_KINDS,
    EVENT_MODE_ENDED,
    EVENT_PERIODIC_CHECK,
    MODE_OUTCOMES,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
    TrackState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults, fill_params
from modules.decision.rule_selector import RULE_CONFIDENCE, RuleSelector


# --- 상태 빌더 ---


def _make_drone(north_m: float = 0.0, east_m: float = 0.0) -> DroneState:
    return DroneState(
        north_m=north_m, east_m=east_m, down_m=-10.0, altitude_m=10.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0,
        battery_pct=90.0, flight_mode="hover",
    )


def _make_track(
    track_id: str = "T1",
    class_name: str = "person",
    north_m: float = 10.0,
    east_m: float = 0.0,
    is_active: bool = True,
    upper_color: str | None = None,
) -> TrackState:
    return TrackState(
        track_id=track_id, class_name=class_name, north_m=north_m, east_m=east_m,
        first_seen_sec=1.0, last_seen_sec=2.0, observation_count=3, mean_confidence=0.8,
        is_active=is_active, upper_color=upper_color,
    )


def _make_state(
    tracks: tuple[TrackState, ...] = (),
    target_class: str = "person",
    target_upper_color: str | None = None,
    hearted: tuple[str, ...] = (),
) -> SelectionState:
    mission = MissionSummary(target_class=target_class, target_upper_color=target_upper_color, hearted_tracks=hearted)
    return SelectionState(time_sec=5.0, drone=_make_drone(), tracks=tracks, mission=mission)


def _make_event(kind: str = EVENT_PERIODIC_CHECK, mode_result: ModeResult | None = None) -> SelectionEvent:
    return SelectionEvent(kind=kind, mode_result=mode_result)


def _select(state: SelectionState, event: SelectionEvent | None = None) -> SelectionResult:
    return RuleSelector(ParamDefaults()).select(state, event or _make_event())


# --- Protocol (SEL-04) ---


def test_rule_selector_is_decision_model():
    assert isinstance(RuleSelector(ParamDefaults()), DecisionModel)


def test_caller_using_only_protocol_gets_result():
    def _run(model: DecisionModel, state: SelectionState, event: SelectionEvent) -> str:
        return model.select(state, event).rule_mode

    assert _run(RuleSelector(ParamDefaults()), _make_state(), _make_event()) == "search"


# --- 표 2 사다리 ---


def test_no_candidate_periodic_check_searches():
    state = _make_state()
    event = _make_event()

    result = _select(state, event)

    assert result.rule_mode == "search"
    assert result.params == fill_params("search", state, event, ParamDefaults())


def test_near_confirmed_track_aims_and_fires():
    result = _select(_make_state(tracks=(_make_track(track_id="T3", north_m=6.0),)))

    assert result.rule_mode == "aim_and_fire"
    assert result.params["target_track"] == "T3"


def test_far_confirmed_track_approaches():
    result = _select(_make_state(tracks=(_make_track(north_m=40.0),)))

    assert result.rule_mode == "approach"


def test_near_color_unknown_candidate_observes():
    result = _select(_make_state(tracks=(_make_track(north_m=10.0),), target_upper_color="red"))

    assert result.rule_mode == "observe"
    assert "aim_and_fire" not in result.options


def test_far_color_unknown_candidate_approaches():
    result = _select(_make_state(tracks=(_make_track(north_m=40.0),), target_upper_color="red"))

    assert result.rule_mode == "approach"


def test_search_timeout_without_candidate_returns_home():
    event = _make_event(EVENT_MODE_ENDED, ModeResult("search", "timeout", None))

    assert _select(_make_state(), event).rule_mode == "return_home"


def test_search_success_without_candidate_keeps_searching():
    event = _make_event(EVENT_MODE_ENDED, ModeResult("search", "success", None))

    assert _select(_make_state(), event).rule_mode == "search"


# --- 재시도 방지 ---


def test_repeated_failure_on_same_target_holds():
    state = _make_state(tracks=(_make_track(track_id="T2", north_m=40.0),))
    event = _make_event(EVENT_MODE_ENDED, ModeResult("approach", "failure", "T2"))

    result = _select(state, event)

    assert result.rule_mode == "hold"
    assert result.params == fill_params("hold", state, event, ParamDefaults())


def test_failure_with_new_nearer_target_keeps_approaching():
    tracks = (_make_track(track_id="T2", north_m=40.0), _make_track(track_id="T5", north_m=30.0))
    event = _make_event(EVENT_MODE_ENDED, ModeResult("approach", "failure", "T2"))

    result = _select(_make_state(tracks=tracks), event)

    assert result.rule_mode == "approach"
    assert result.params["target_track"] == "T5"


# --- 결과 필드·결정성 ---


def test_result_fields_are_rule_one_hot_and_local():
    state = _make_state(tracks=(_make_track(north_m=40.0),))
    event = _make_event()

    result = _select(state, event)

    assert result.source == SOURCE_RULE
    assert result.fallback_reason == FALLBACK_NONE
    assert result.confidence == RULE_CONFIDENCE == 1.0
    assert result.options == filter_options(state, event)
    assert tuple(result.probabilities) == result.options
    assert result.probabilities == {mode: (1.0 if mode == result.rule_mode else 0.0) for mode in result.options}
    assert result.latency.total_ms >= 0
    assert result.latency.orin_processing_ms is None
    assert result.latency.network_rtt_ms is None
    assert result.input_tokens is None


def test_same_input_gives_same_result():
    state = _make_state(tracks=(_make_track(track_id="T2", north_m=6.0), _make_track(track_id="T5", north_m=-6.0)))
    event = _make_event()

    first = _select(state, event).to_dict()
    second = _select(state, event).to_dict()
    first.pop("latency")
    second.pop("latency")

    assert first == second


# --- 스윕 (SEL-03: 유효 입력에서 예외 없음) ---


def _get_sweep_states() -> list[SelectionState]:
    return [
        _make_state(),
        _make_state(tracks=(_make_track(track_id="T1", north_m=4.0),)),
        _make_state(tracks=(_make_track(track_id="T1", north_m=40.0),), target_upper_color="red"),
        _make_state(tracks=(_make_track(track_id="T1", north_m=4.0),), hearted=("T1",)),
    ]


def _get_sweep_events() -> list[SelectionEvent]:
    events = [_make_event(kind) for kind in EVENT_KINDS if kind != EVENT_MODE_ENDED]
    for rule_mode in RULE_MODES:
        target_track = "T1" if rule_mode in TARGET_MODES else None
        for outcome in MODE_OUTCOMES:
            events.append(_make_event(EVENT_MODE_ENDED, ModeResult(rule_mode, outcome, target_track)))
    return events


def test_every_state_and_event_combination_yields_result_in_options():
    selector = RuleSelector(ParamDefaults())

    for state in _get_sweep_states():
        for event in _get_sweep_events():
            result = selector.select(state, event)

            assert isinstance(result, SelectionResult)
            assert result.rule_mode in result.options
