"""MANUAL/AI/RETURN 상태 기계 (C4) — 전환 규칙(D-04, D-06, D-07, D-09, D-10)과 검증."""

import math

import pytest

from modules.mission.mode_machine import (
    MODE_AI,
    MODE_MANUAL,
    MODE_RETURN,
    MODES,
    REASON_OPERATOR,
    ModeStateMachine,
    ModeTransition,
)
from modules.mission.safety import REASON_BATTERY, REASON_GEOFENCE, SAFETY_REASONS, SafetyVerdict

_BATTERY_VERDICT = SafetyVerdict(REASON_BATTERY, "battery=20.0 required=23.8 drain=0.250")
_GEOFENCE_VERDICT = SafetyVerdict(REASON_GEOFENCE, "north=150.0 east=0.0")


def _machine_in(mode: str) -> ModeStateMachine:
    return ModeStateMachine(initial_mode=mode)


def test_modes_constant():
    assert MODES == ("MANUAL", "AI", "RETURN")


def test_manual_to_ai_on_request():
    machine = ModeStateMachine()

    transition = machine.on_operator(1.0, True)

    assert transition == ModeTransition(1.0, "MANUAL", "AI", "operator", "")
    assert machine.mode == MODE_AI
    assert machine.on_operator(2.0, True) is None


def test_manual_ignores_no_request_and_safety():
    machine = ModeStateMachine()

    assert machine.on_operator(1.0, False) is None
    assert machine.on_safety(1.0, _BATTERY_VERDICT) is None
    assert machine.mode == MODE_MANUAL


def test_ai_to_manual_when_request_drops():
    transition = _machine_in(MODE_AI).on_operator(3.0, False)

    assert transition == ModeTransition(3.0, MODE_AI, MODE_MANUAL, REASON_OPERATOR)


def test_ai_to_return_on_verdict():
    machine = _machine_in(MODE_AI)

    assert machine.on_safety(4.0, None) is None

    transition = machine.on_safety(5.0, _BATTERY_VERDICT)

    assert transition == ModeTransition(5.0, MODE_AI, MODE_RETURN, REASON_BATTERY, _BATTERY_VERDICT.detail)
    assert machine.mode == MODE_RETURN


def test_return_holds_on_new_verdict_and_kept_request():
    machine = _machine_in(MODE_RETURN)

    assert machine.on_safety(1.0, _GEOFENCE_VERDICT) is None
    assert machine.on_operator(1.0, True) is None
    assert machine.mode == MODE_RETURN


def test_return_to_manual_then_resume_rechecks_safety():
    machine = _machine_in(MODE_RETURN)

    assert machine.on_operator(1.0, False) == ModeTransition(1.0, MODE_RETURN, MODE_MANUAL, REASON_OPERATOR)

    # D-10: 재개 틱에 남아 있는 사유가 곧바로 다시 RETURN 으로 보낸다
    assert machine.on_operator(2.0, True) == ModeTransition(2.0, MODE_MANUAL, MODE_AI, REASON_OPERATOR)
    assert machine.on_safety(2.0, _BATTERY_VERDICT).to_mode == MODE_RETURN


@pytest.mark.parametrize("reason", SAFETY_REASONS)
def test_every_safety_reason_drives_return(reason):
    transition = _machine_in(MODE_AI).on_safety(1.0, SafetyVerdict(reason, "x"))

    assert (transition.to_mode, transition.reason, transition.detail) == (MODE_RETURN, reason, "x")


def test_full_scenario_transition_list():
    machine = ModeStateMachine()
    transitions = [
        machine.on_operator(1.0, True),
        machine.on_operator(2.0, False),
        machine.on_operator(3.0, True),
        machine.on_safety(4.0, _GEOFENCE_VERDICT),
        machine.on_operator(5.0, False),
    ]

    assert [(item.from_mode, item.to_mode, item.reason) for item in transitions] == [
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_MANUAL, REASON_OPERATOR),
        (MODE_MANUAL, MODE_AI, REASON_OPERATOR),
        (MODE_AI, MODE_RETURN, REASON_GEOFENCE),
        (MODE_RETURN, MODE_MANUAL, REASON_OPERATOR),
    ]


def test_initial_mode_validation():
    assert ModeStateMachine(initial_mode="AI").mode == MODE_AI

    with pytest.raises(ValueError, match="field 'initial_mode' must be one of"):
        ModeStateMachine(initial_mode="FLY")


@pytest.mark.parametrize(
    ("args", "field_name"),
    [
        ((1.0, "FLY", MODE_AI, REASON_OPERATOR), "from_mode"),
        ((1.0, MODE_MANUAL, "FLY", REASON_OPERATOR), "to_mode"),
        ((1.0, MODE_MANUAL, MODE_AI, "whim"), "reason"),
        ((math.nan, MODE_MANUAL, MODE_AI, REASON_OPERATOR), "time_sec"),
    ],
)
def test_mode_transition_validation(args, field_name):
    with pytest.raises(ValueError, match=f"field '{field_name}' must be"):
        ModeTransition(*args)
