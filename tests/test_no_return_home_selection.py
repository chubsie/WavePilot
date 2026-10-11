"""선택기 return_home 끄기 — 임무 파일 [selector] allow_return_home (기본 true).

끄면 보기에서 return_home 이 빠지고(최소 보기 search·hold), 규칙 5 는 search 를 다시 고르고,
d1 이 return_home 을 내면 out_of_menu 로 규칙 대체된다. 안전 감시의 강제 복귀는 그대로다.
"""

from pathlib import Path

import pytest

from modules.decision.d1_selector import D1SelectionError, D1Selector
from modules.decision.factory import SELECTOR_D1, SELECTOR_RULE, SelectorConfig, create_decision_model
from modules.decision.fallback_selector import FallbackSelector
from modules.decision.interface import FALLBACK_OUT_OF_MENU
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_PERIODIC_CHECK,
    DroneState,
    MissionSummary,
    ModeResult,
    SelectionEvent,
    SelectionState,
)
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.decision.systemone_client import ChoiceReply
from modules.drone.mission_loop import build_rule_mode_settings
from modules.drone.run_mission import apply_mission_spec, apply_selector_spec, build_safety_config
from modules.flight_control.interface import Telemetry, ned_to_gps
from modules.mission.mission_file import load_mission_file, parse_mission
from modules.mission.operator import LinkStatus
from modules.mission.safety import REASON_GEOFENCE, REASON_MAX_FLIGHT_TIME, SafetyMonitor

URBAN_DEMO_PATH = Path(__file__).resolve().parent.parent / "missions" / "urban_demo.toml"
STOP_DISTANCE_M = ParamDefaults().approach_stop_distance_m


# --- 상태 빌더 ---


def _make_state() -> SelectionState:
    drone = DroneState(
        north_m=0.0, east_m=0.0, down_m=-10.0, altitude_m=10.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0,
        battery_pct=90.0, flight_mode="hover",
    )
    mission = MissionSummary(target_class="person", target_upper_color=None, hearted_tracks=())
    return SelectionState(time_sec=5.0, drone=drone, tracks=(), mission=mission)


def _make_search_failed_event(outcome: str = "failure") -> SelectionEvent:
    return SelectionEvent(kind=EVENT_MODE_ENDED, mode_result=ModeResult("search", outcome, None))


def _make_mission_data(selector: dict | None = None) -> dict:
    data = {
        "target": {"class": "person", "upper_color": "red", "action": "heart"},
        "search_area": {"north_m": [-10.0, 10.0], "east_m": [-5.0, 5.0]},
        "safety": {"geofence_margin_m": 20.0, "battery_reserve_pct": 15.0, "max_flight_sec": 300.0},
    }
    if selector is not None:
        data["selector"] = selector
    return data


class _StubClient:
    def __init__(self, reply: ChoiceReply) -> None:
        self.reply = reply
        self.asked_criteria: list[dict[str, str]] = []

    def probe_rtt_ms(self) -> float:
        return 1.0

    def ask_choice(self, state_text: str, question_id: str, instructions: str, criteria: dict[str, str]) -> ChoiceReply:
        self.asked_criteria.append(dict(criteria))
        return self.reply


def _build_reply(choice: str) -> ChoiceReply:
    return ChoiceReply(choice=choice, probabilities={choice: 1.0}, confidence=0.9, input_tokens=1, total_ms=1.0)


# --- 보기 필터 ---


def test_filter_options_keeps_return_home_by_default():
    assert filter_options(_make_state(), SelectionEvent(EVENT_PERIODIC_CHECK, None), STOP_DISTANCE_M) == (
        "search", "hold", "return_home",
    )


def test_filter_options_drops_return_home_when_disabled():
    options = filter_options(_make_state(), SelectionEvent(EVENT_PERIODIC_CHECK, None), STOP_DISTANCE_M, False)

    assert options == ("search", "hold")


# --- 규칙 선택기 (표 2 의 5줄) ---


@pytest.mark.parametrize("outcome", ["failure", "timeout"])
def test_rule_5_picks_return_home_by_default(outcome: str):
    result = RuleSelector(ParamDefaults()).select(_make_state(), _make_search_failed_event(outcome))

    assert result.rule_mode == "return_home"


@pytest.mark.parametrize("outcome", ["failure", "timeout"])
def test_rule_5_restarts_search_when_return_home_disabled(outcome: str):
    result = RuleSelector(ParamDefaults(), allow_return_home=False).select(_make_state(), _make_search_failed_event(outcome))

    assert result.rule_mode == "search"
    assert result.options == ("search", "hold")
    assert "return_home" not in result.probabilities


# --- d1 선택기 ---


def test_d1_asks_without_return_home_when_disabled():
    client = _StubClient(_build_reply("search"))
    selector = D1Selector(client, ParamDefaults(), 0.3, allow_return_home=False)

    result = selector.select(_make_state(), _make_search_failed_event())

    assert result.rule_mode == "search"
    assert result.options == ("search", "hold")
    assert "return_home" not in client.asked_criteria[0]


def test_d1_return_home_reply_is_out_of_menu_when_disabled():
    selector = D1Selector(_StubClient(_build_reply("return_home")), ParamDefaults(), 0.3, allow_return_home=False)

    with pytest.raises(D1SelectionError) as raised:
        selector.select(_make_state(), _make_search_failed_event())

    assert raised.value.reason == FALLBACK_OUT_OF_MENU


def test_fallback_after_out_of_menu_restarts_search_when_disabled():
    defaults = ParamDefaults()
    primary = D1Selector(_StubClient(_build_reply("return_home")), defaults, 0.3, allow_return_home=False)
    selector = FallbackSelector(primary, RuleSelector(defaults, allow_return_home=False), 0.0, param_defaults=defaults)

    result = selector.select(_make_state(), _make_search_failed_event())

    assert result.fallback_reason == FALLBACK_OUT_OF_MENU
    assert result.rule_mode == "search"


# --- 팩토리 ---


def test_selector_config_allows_return_home_by_default():
    assert SelectorConfig.from_env({}).allow_return_home is True


def test_factory_passes_allow_return_home_to_rule_selector():
    config = SelectorConfig.from_env({"WAVEPILOT_SELECTOR": SELECTOR_RULE})
    model = create_decision_model(apply_selector_spec(config, parse_mission(_make_mission_data({"allow_return_home": False}))))

    assert model.select(_make_state(), _make_search_failed_event()).rule_mode == "search"


def test_factory_passes_allow_return_home_to_d1_and_rule():
    env = {"WAVEPILOT_SELECTOR": SELECTOR_D1, "D1_SERVER_URL": "http://127.0.0.1:1"}
    config = apply_selector_spec(SelectorConfig.from_env(env), parse_mission(_make_mission_data({"allow_return_home": False})))
    model = create_decision_model(config)

    assert isinstance(model, FallbackSelector)
    assert model._primary._allow_return_home is False
    assert model._fallback._allow_return_home is False


# --- 임무 파일 ---


def test_mission_selector_section_is_optional_and_defaults_true():
    assert parse_mission(_make_mission_data()).allow_return_home is True
    assert parse_mission(_make_mission_data({})).allow_return_home is True


def test_mission_selector_allow_return_home_false():
    assert parse_mission(_make_mission_data({"allow_return_home": False})).allow_return_home is False


def test_mission_selector_rejects_non_bool():
    with pytest.raises(ValueError, match="field 'selector.allow_return_home' must be a bool"):
        parse_mission(_make_mission_data({"allow_return_home": "no"}))


def test_mission_selector_rejects_unknown_key():
    with pytest.raises(ValueError, match="field 'selector.allow_rtl' is not allowed"):
        parse_mission(_make_mission_data({"allow_rtl": False}))


def test_mission_selector_rejects_non_table():
    data = _make_mission_data()
    data["selector"] = "off"

    with pytest.raises(ValueError, match="field 'selector' must be a table"):
        parse_mission(data)


def test_apply_selector_spec_keeps_config_without_mission():
    config = SelectorConfig.from_env({})

    assert apply_selector_spec(config, None) is config


def test_urban_demo_disables_return_home_selection():
    spec = load_mission_file(URBAN_DEMO_PATH)

    assert spec.allow_return_home is False
    assert apply_selector_spec(SelectorConfig.from_env({}), spec).allow_return_home is False


# --- 강제 복귀(안전 감시)는 그대로 ---


def _telemetry(t: float, north: float, east: float = 0.0) -> Telemetry:
    return Telemetry(
        time_sec=t, north_m=north, east_m=east, down_m=-8.0,
        roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-90.0, gimbal_yaw_deg=0.0,
        battery_pct=90.0, flight_mode="hover", gps=ned_to_gps(north, east, -8.0),
    )


def _build_urban_demo_monitor() -> SafetyMonitor:
    spec = load_mission_file(URBAN_DEMO_PATH)
    defaults = apply_mission_spec(ParamDefaults(), spec)
    settings = build_rule_mode_settings(defaults, target_class=spec.target_class, target_upper_color=spec.target_upper_color)
    return SafetyMonitor(build_safety_config(spec, settings, defaults))


def test_forced_return_on_geofence_still_works_with_selection_disabled():
    verdict = _build_urban_demo_monitor().check(1.0, _telemetry(1.0, north=66.0), 1.0, LinkStatus(True, 1.0))

    assert verdict is not None
    assert verdict.reason == REASON_GEOFENCE


def test_forced_return_on_max_flight_time_still_works_with_selection_disabled():
    verdict = _build_urban_demo_monitor().check(300.0, _telemetry(300.0, north=0.0), 300.0, LinkStatus(True, 300.0))

    assert verdict is not None
    assert verdict.reason == REASON_MAX_FLIGHT_TIME
