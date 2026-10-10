"""modules.flight_control.interface.flight_control / modules.perception.interface.track 계약: 텔레메트리, 명령 4종, track 직렬화."""

import pytest

from modules.flight_control.interface.flight_control import (
    FLIGHT_MODES,
    CommandAck,
    FlightControl,
    GoTo,
    Land,
    ReturnToHome,
    TakeOff,
    Telemetry,
    command_from_dict,
)
from modules.perception.interface.track import Track


def _make_track() -> Track:
    return Track(
        track_id=3,
        class_name="car",
        north_m=25.5,
        east_m=-4.0,
        first_seen_sec=1.0,
        last_seen_sec=6.5,
        observation_count=55,
        mean_confidence=0.81,
        is_active=True,
    )


def test_altitude_is_negative_down(hover_telemetry: Telemetry) -> None:
    assert hover_telemetry.altitude_m == 40.0


def test_telemetry_round_trip(hover_telemetry: Telemetry) -> None:
    assert Telemetry.from_dict(hover_telemetry.to_dict()) == hover_telemetry


def test_telemetry_rejects_unknown_flight_mode(hover_telemetry: Telemetry) -> None:
    data = hover_telemetry.to_dict()
    data["flight_mode"] = "flying"

    with pytest.raises(ValueError, match="flight_mode"):
        Telemetry.from_dict(data)


def test_telemetry_rejects_missing_key(hover_telemetry: Telemetry) -> None:
    data = hover_telemetry.to_dict()
    del data["battery_pct"]

    with pytest.raises(ValueError, match="battery_pct"):
        Telemetry.from_dict(data)


def test_flight_modes() -> None:
    assert FLIGHT_MODES == ("grounded", "hover", "moving", "landing", "returning")


@pytest.mark.parametrize(
    "command, kind",
    [
        (TakeOff(altitude_m=40.0), "take_off"),
        (GoTo(north_m=10.0, east_m=-5.0, altitude_m=40.0), "go_to"),
        (Land(), "land"),
        (ReturnToHome(), "return_to_home"),
    ],
)
def test_command_round_trip(command, kind: str) -> None:
    data = command.to_dict()

    assert data["kind"] == kind
    assert command_from_dict(data) == command


def test_command_from_dict_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        command_from_dict({"kind": "fly"})


def test_go_to_requires_altitude() -> None:
    with pytest.raises(ValueError, match="altitude_m"):
        command_from_dict({"kind": "go_to", "north_m": 1.0, "east_m": 2.0})


def test_command_ack_fields() -> None:
    ack = CommandAck(accepted=False, reason="battery low")

    assert ack.accepted is False
    assert ack.reason == "battery low"


def test_class_with_both_methods_satisfies_flight_control(hover_telemetry: Telemetry) -> None:
    class StaticFlightControl:
        def get_telemetry(self) -> Telemetry:
            return hover_telemetry

        def send_command(self, command) -> CommandAck:
            return CommandAck(accepted=True, reason="")

    assert isinstance(StaticFlightControl(), FlightControl)


def test_track_round_trip() -> None:
    track = _make_track()

    assert Track.from_dict(track.to_dict()) == track


@pytest.mark.parametrize("bad_count", [0, -1, 2.5, "3"])
def test_track_rejects_bad_observation_count(bad_count: object) -> None:
    data = _make_track().to_dict()
    data["observation_count"] = bad_count

    with pytest.raises(ValueError, match="observation_count"):
        Track.from_dict(data)
