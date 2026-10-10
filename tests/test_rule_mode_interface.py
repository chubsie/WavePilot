import dataclasses
import math

import pytest

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import Telemetry
from modules.perception.interface import DEFAULT_CAMERA
from modules.rule_mode.interface import (
    DEFAULT_SEARCH_AREA,
    ModeTick,
    RuleMode,
    RuleModeSettings,
    SearchArea,
    track_label,
)


def _telemetry() -> Telemetry:
    return Telemetry(
        time_sec=0.0, north_m=0.0, east_m=0.0, down_m=-10.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-20.0, gimbal_yaw_deg=0.0, battery_pct=100.0, flight_mode="hover",
    )


def _tick(**overrides) -> ModeTick:
    fields = dict(
        now_sec=0.0, telemetry=_telemetry(), tracks=(), frame=None, frame_telemetry=None,
        hearted_track_ids=frozenset(),
    )
    fields.update(overrides)

    return ModeTick(**fields)


def test_search_area_accepts_default_bounds():
    area = SearchArea(0.0, 10.0, -5.0, 5.0)

    assert (area.north_min_m, area.north_max_m, area.east_min_m, area.east_max_m) == (0.0, 10.0, -5.0, 5.0)


@pytest.mark.parametrize(
    "bounds",
    [
        (10.0, 10.0, -5.0, 5.0),
        (11.0, 10.0, -5.0, 5.0),
        (0.0, 10.0, 5.0, 5.0),
        (0.0, 10.0, 6.0, 5.0),
        (math.nan, 10.0, -5.0, 5.0),
        (0.0, math.inf, -5.0, 5.0),
        (0.0, 10.0, -math.inf, 5.0),
        (True, 10.0, -5.0, 5.0),
    ],
)
def test_search_area_rejects_invalid_bounds(bounds):
    with pytest.raises(ValueError):
        SearchArea(*bounds)


def test_settings_defaults_match_outline():
    settings = RuleModeSettings()

    assert settings.target_class == "person"
    assert settings.search_area == DEFAULT_SEARCH_AREA
    assert (
        settings.search_area.north_min_m,
        settings.search_area.north_max_m,
        settings.search_area.east_min_m,
        settings.search_area.east_max_m,
    ) == (20.0, 120.0, -50.0, 50.0)
    assert settings.search_altitude_m == 15.0
    assert settings.search_lane_spacing_m == 25.0
    assert settings.camera == DEFAULT_CAMERA
    assert settings.arrival_tolerance_m == 1.5


@pytest.mark.parametrize("field", ["search_altitude_m", "search_lane_spacing_m", "arrival_tolerance_m"])
@pytest.mark.parametrize("value", [0.0, -1.0, math.nan])
def test_settings_reject_non_positive_values(field, value):
    with pytest.raises(ValueError):
        RuleModeSettings(**{field: value})


def test_settings_reject_empty_target_class():
    with pytest.raises(ValueError):
        RuleModeSettings(target_class="")


def test_mode_tick_accepts_valid_input():
    tick = _tick(tracks=(), hearted_track_ids=frozenset({1}))

    assert tick.hearted_track_ids == frozenset({1})


def test_mode_tick_rejects_invalid_input():
    with pytest.raises(ValueError):
        _tick(now_sec=math.nan)
    with pytest.raises(ValueError):
        _tick(tracks=[])
    with pytest.raises(ValueError):
        _tick(hearted_track_ids={1})


def test_track_label_round_trips_through_mode_result():
    assert track_label(7) == "T7"
    assert ModeResult("approach", "success", track_label(7)).target_track == "T7"


@pytest.mark.parametrize("track_id", [-1, True])
def test_track_label_rejects_invalid_id(track_id):
    with pytest.raises(ValueError):
        track_label(track_id)


def test_minimal_stub_satisfies_rule_mode_protocol():
    class StubMode:
        rule_mode = "hold"

        def step(self, tick, control):
            return None

    assert isinstance(StubMode(), RuleMode)


def test_contract_dataclasses_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RuleModeSettings().target_class = "car"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_SEARCH_AREA.north_min_m = 0.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _tick().now_sec = 1.0  # type: ignore[misc]
