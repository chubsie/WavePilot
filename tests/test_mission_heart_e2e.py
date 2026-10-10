"""헤드리스 하트 E2E — mock 세계(빨간 옷 1명 + 다른 색 1명) 에서 규칙 선택기로 임무 전체를 돈다.

증명하는 것:
- SC3 mock 쪽 / HRT-01 / RM-02: --target-color red 면 빨간 배우에게만 FireHeart 가 정확히 10발 나가고 other 배우는 0발.
- SC4 / HRT-02: 같은 사람에게 재발사가 없다 — aim_and_fire success 가 한 번뿐이고 하트를 받은 track 은 빨강 하나.
- D-07: target_upper_color=None(--target-color none) 이면 색 조건 없는 Phase 3 동작 — 두 사람 모두 10발씩.
CLI(main) 대신 runner 수준에서 단언한다 — flight.commands_received 로 FireHeart 를 세야 하기 때문이다.
"""

from collections import Counter

from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.drone.mission_loop import END_LANDED, MissionConfig, MissionRunner, build_rule_mode_settings
from modules.drone.run_mission import DEFAULT_MOCK_TARGETS, build_mock_world
from modules.flight_control.interface import FireHeart
from modules.perception.interface import DEFAULT_CAMERA
from modules.perception.tracking import TrackAggregator
from modules.rule_mode.interface import track_label

MAX_MISSION_SEC = 600.0
HEART_BURST_COUNT = 10


def get_target_track_id(upper_color: str) -> int:
    return next(target.track_id for target in DEFAULT_MOCK_TARGETS if target.upper_color == upper_color)


class RecordingAggregator(TrackAggregator):
    def __init__(self) -> None:
        super().__init__(DEFAULT_CAMERA)
        self.seen_colors_by_track: dict[int, set[str]] = {}

    def snapshot(self, now_sec: float):
        track_list = super().snapshot(now_sec)
        for track in track_list:
            if track.upper_color is not None:
                self.seen_colors_by_track.setdefault(track.track_id, set()).add(track.upper_color)

        return track_list


def run_headless_mission(target_upper_color: str | None, aggregator: TrackAggregator | None = None):
    defaults = ParamDefaults()
    flight, frame_source = build_mock_world()
    runner = MissionRunner(
        flight_control=flight,
        frame_source=frame_source,
        aggregator=aggregator or TrackAggregator(DEFAULT_CAMERA),
        decision_model=RuleSelector(defaults),
        param_defaults=defaults,
        settings=build_rule_mode_settings(defaults, target_upper_color=target_upper_color),
        clock=lambda: flight.now_sec,
        advance=flight.advance,
        config=MissionConfig(max_mission_sec=MAX_MISSION_SEC),
    )

    return runner.run(), flight


def count_hearts_by_track(flight) -> dict[int, int]:
    return dict(Counter(command.track_id for _, command in flight.commands_received if isinstance(command, FireHeart)))


def test_red_target_mission_hearts_only_the_red_actor_once():
    red_track_id = get_target_track_id("red")
    other_track_id = get_target_track_id("other")

    result, flight = run_headless_mission("red")

    hearts_by_track = count_hearts_by_track(flight)
    aim_successes = [
        mode_result for mode_result in result.mode_results
        if mode_result.rule_mode == "aim_and_fire" and mode_result.outcome == "success"
    ]
    assert result.end_reason == END_LANDED
    assert not result.forced_return
    assert result.final_flight_mode == "grounded"
    assert hearts_by_track == {red_track_id: HEART_BURST_COUNT}
    assert hearts_by_track.get(other_track_id, 0) == 0
    assert [mode_result.target_track for mode_result in aim_successes] == [track_label(red_track_id)]


def test_red_target_mission_actually_sees_the_other_actor():
    # other 배우가 0발인 이유가 "못 봐서" 가 아니라 "색이 달라서" 임을 확인한다
    other_track_id = get_target_track_id("other")
    aggregator = RecordingAggregator()

    result, _ = run_headless_mission("red", aggregator)

    assert result.end_reason == END_LANDED
    assert aggregator.seen_colors_by_track.get(other_track_id) == {"other"}


def test_no_target_color_hearts_both_actors_like_phase3():
    red_track_id = get_target_track_id("red")
    other_track_id = get_target_track_id("other")

    result, flight = run_headless_mission(None)

    assert result.end_reason == END_LANDED
    assert result.final_flight_mode == "grounded"
    assert count_hearts_by_track(flight) == {red_track_id: HEART_BURST_COUNT, other_track_id: HEART_BURST_COUNT}
