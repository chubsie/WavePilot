"""BrowserFlightControl(링크·AI 모드 게이트, 텔레메트리) + 명령 데모 스크립트 (SIM-02, SIM-03, D-05)."""

import pytest

from modules.drone.sim_demo import build_demo_steps, format_telemetry_line, run_demo
from modules.flight_control.interface import (
    CommandAck,
    FlightControl,
    Land,
    SetYaw,
    Telemetry,
    ned_to_gps,
)
from modules.simulation.browser.bridge import LINK_TIMEOUT_SEC, SimBridge
from modules.simulation.browser.flight_control import BrowserFlightControl


def make_telemetry(gps_enabled: bool = True) -> Telemetry:
    return Telemetry(
        time_sec=2.0,
        north_m=20.0,
        east_m=3.0,
        down_m=-15.0,
        roll_deg=0.5,
        pitch_deg=-1.0,
        yaw_deg=90.0,
        gimbal_pitch_deg=-45.0,
        gimbal_yaw_deg=30.0,
        battery_pct=91.0,
        flight_mode="moving",
        gps=ned_to_gps(20.0, 3.0, -15.0) if gps_enabled else None,
    )


@pytest.fixture
def now() -> list[float]:
    return [0.0]


@pytest.fixture
def bridge(now: list[float]) -> SimBridge:
    return SimBridge(clock=lambda: now[0])


@pytest.fixture
def control(bridge: SimBridge) -> BrowserFlightControl:
    return BrowserFlightControl(bridge)


# --- FlightControl 계약 ---


def test_satisfies_flight_control_protocol(control: BrowserFlightControl) -> None:
    assert isinstance(control, FlightControl)


def test_get_telemetry_before_browser_raises(control: BrowserFlightControl) -> None:
    with pytest.raises(RuntimeError, match="simulator not connected"):
        control.get_telemetry()


def test_get_telemetry_returns_browser_telemetry_with_gps(bridge: SimBridge, control: BrowserFlightControl) -> None:
    telemetry = make_telemetry()
    bridge.update_from_browser(telemetry, True, [])

    result = control.get_telemetry()

    assert result == telemetry
    assert result.gps == telemetry.gps


# --- send_command 게이트 (D-05) ---


def test_send_command_without_telemetry_is_refused(bridge: SimBridge, control: BrowserFlightControl) -> None:
    assert control.send_command(Land()) == CommandAck(False, "simulator not connected")
    assert bridge.drain_commands() == []


def test_send_command_on_stale_link_is_refused(
    bridge: SimBridge, control: BrowserFlightControl, now: list[float]
) -> None:
    bridge.update_from_browser(make_telemetry(), True, [])
    now[0] = LINK_TIMEOUT_SEC + 0.1

    ack = control.send_command(Land())

    assert ack == CommandAck(False, "simulator not connected")
    assert bridge.drain_commands() == []


def test_send_command_in_manual_mode_is_refused(bridge: SimBridge, control: BrowserFlightControl) -> None:
    bridge.update_from_browser(make_telemetry(), False, [])

    ack = control.send_command(Land())

    assert ack == CommandAck(False, "manual mode")
    assert bridge.drain_commands() == []


def test_send_command_in_ai_mode_is_queued(bridge: SimBridge, control: BrowserFlightControl) -> None:
    bridge.update_from_browser(make_telemetry(), True, [])

    ack = control.send_command(SetYaw(90.0))

    assert ack == CommandAck(True, "queued set_yaw id=1")
    assert bridge.drain_commands() == [{"id": 1, **SetYaw(90.0).to_dict()}]


# --- 데모 스크립트 ---


def test_demo_steps_cover_every_motion_command_in_order() -> None:
    kinds = [step.command.KIND for step in build_demo_steps()]

    assert kinds == ["go_to", "set_yaw", "set_gimbal", "set_velocity", "land", "fire_heart", "return_to_home"]


def test_format_telemetry_line_includes_gps() -> None:
    telemetry = make_telemetry()

    line = format_telemetry_line(telemetry)

    assert f"gps={telemetry.gps.latitude_deg:.6f},{telemetry.gps.longitude_deg:.6f}" in line


def test_format_telemetry_line_without_gps() -> None:
    assert "gps=none" in format_telemetry_line(make_telemetry(gps_enabled=False))


def test_run_demo_queues_all_steps_and_prints_gps(bridge: SimBridge, control: BrowserFlightControl) -> None:
    bridge.update_from_browser(make_telemetry(), True, [])
    lines: list[str] = []

    run_demo(control, build_demo_steps(), sleep=lambda _sec: None, print_fn=lines.append)

    drained_kinds = [command["kind"] for command in bridge.drain_commands()]
    assert drained_kinds == [step.command.KIND for step in build_demo_steps()]
    assert any("gps=" in line for line in lines)


def test_run_demo_retries_until_ai_mode(bridge: SimBridge, control: BrowserFlightControl) -> None:
    bridge.update_from_browser(make_telemetry(), False, [])
    lines: list[str] = []

    def switch_to_ai_mode(_sec: float) -> None:
        bridge.update_from_browser(make_telemetry(), True, [])

    run_demo(control, build_demo_steps()[:1], sleep=switch_to_ai_mode, print_fn=lines.append)

    assert lines[0] == "-> go_to: manual mode"
    assert "-> go_to: queued go_to id=1" in lines
    assert [command["kind"] for command in bridge.drain_commands()] == ["go_to"]
