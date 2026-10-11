"""운용자 입력 계약 (C2) — LinkStatus 검증, ScriptedOperator heartbeat, OperatorInput Protocol."""

import math

import pytest

from modules.mission.operator import LinkStatus, OperatorInput, ScriptedOperator


def test_scripted_operator_defaults_to_ai_requested_and_connected():
    operator = ScriptedOperator()

    assert operator.is_ai_requested() is True
    assert operator.link_status(1.0) == LinkStatus(True, 1.0)


def test_scripted_operator_accepts_keyword_arguments():
    operator = ScriptedOperator(ai_requested=False, link_connected=False)

    assert operator.is_ai_requested() is False
    assert operator.link_status(1.0) == LinkStatus(False, None)


def test_link_status_keeps_last_heartbeat_after_disconnect():
    operator = ScriptedOperator()
    operator.link_status(1.0)

    assert operator.link_status(2.5) == LinkStatus(True, 2.5)

    operator.link_connected = False

    assert operator.link_status(4.0) == LinkStatus(False, 2.5)

    operator.link_connected = True

    assert operator.link_status(6.0) == LinkStatus(True, 6.0)


def test_scripted_operator_satisfies_operator_input_protocol():
    assert isinstance(ScriptedOperator(), OperatorInput)


@pytest.mark.parametrize("connected", [1, None, "yes"])
def test_link_status_rejects_non_bool_connected(connected):
    with pytest.raises(ValueError, match="field 'connected' must be"):
        LinkStatus(connected, 1.0)


@pytest.mark.parametrize("heartbeat", [math.nan, math.inf, True, "1.0"])
def test_link_status_rejects_invalid_heartbeat(heartbeat):
    with pytest.raises(ValueError, match="field 'last_heartbeat_sec' must be"):
        LinkStatus(True, heartbeat)


def test_link_status_accepts_none_and_numbers():
    assert LinkStatus(False, None).last_heartbeat_sec is None
    assert LinkStatus(True, 3).last_heartbeat_sec == 3
