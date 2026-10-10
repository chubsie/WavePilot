"""보기 필터 — 질문 전에 지금 고를 수 없는 rule_mode 를 뺀다 (D-02).

보기 필터는 불가능한 모드를 구조적으로 없애는 Safety by design 장치다(D-02).
d1 선택기와 규칙 선택기가 같은 필터를 쓴다(D-04) — 두 선택기의 차이는 모드 선택에만 있다.
배터리·비행 모드·구역 이탈·링크 판정은 Phase 5 안전 감시가 선택보다 먼저 하므로 여기서 하지 않는다.

용어:
- 후보 track: is_active 가 참이고, class_name == mission.target_class 이고, hearted 가 아니고, 색이 어긋나지 않는 track.
  색이 어긋남 = mission.target_upper_color 와 track.upper_color 가 둘 다 있는데 다름. 색을 모르는(None) track 은 후보다.
- 확정 track: 후보 중 mission.target_upper_color 가 None 이거나 upper_color 가 그와 같은 track.
  임무가 색을 요구하지 않으면 후보 = 확정.
- 방금 하트 보낸 track: event 가 mode_ended 이고 mode_result 가 aim_and_fire·success 이면 그 target_track.
  상태의 hearted_tracks 가 한 틱 늦게 갱신돼도 다시 겨누지 않도록 hearted 로 취급한다.
- 가장 가까운 track: 드론과의 수평 거리 hypot(north 차, east 차) 가 가장 작은 track.
  거리가 같으면 라벨 숫자(int(track_id[1:]))가 작은 쪽. 결정적.

표 1 — 보기 필터:
| rule_mode    | 보기에 남는 조건       | 빠지는 경우                                   | 근거 |
|--------------|------------------------|-----------------------------------------------|------|
| search       | 항상                   | 없음                                          | 후보가 있어도 다른 구역을 더 보는 선택은 가능하다 |
| approach     | 후보 track 1개 이상    | 후보 없음                                     | 다가갈 대상이 없다 |
| aim_and_fire | 확정 track 1개 이상    | 확정 없음(후보 없음, 또는 색 요구인데 색 확정 없음) | 색을 모르는 사람에게 하트를 보내지 않는다. Phase 1 은 upper_color 가 늘 None 이라 색 요구 임무에서는 항상 빠진다 |
| observe      | 후보 track 1개 이상    | 후보 없음                                     | 지켜볼 대상이 없다 |
| hold         | 항상                   | 없음                                          | 언제나 안전한 선택 |
| return_home  | 항상                   | 없음                                          | 언제나 안전한 선택. 비상 RETURN 판정은 Phase 5 안전 감시 몫 |

결과 순서는 RULE_MODES 순서이고, 최소 보기는 ("search", "hold", "return_home") 이라 빈 보기가 나오지 않는다.
"""

import math
from collections.abc import Sequence

from modules.decision.interface.menu import RULE_MODES
from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    DroneState,
    MissionSummary,
    SelectionEvent,
    SelectionState,
    TrackState,
)


def get_horizontal_distance_m(drone: DroneState, track: TrackState) -> float:
    return math.hypot(track.north_m - drone.north_m, track.east_m - drone.east_m)


def get_candidate_tracks(state: SelectionState, event: SelectionEvent) -> tuple[TrackState, ...]:
    mission = state.mission
    hearted_labels = _get_hearted_labels(state, event)

    return tuple(
        track
        for track in state.tracks
        if track.is_active
        and track.class_name == mission.target_class
        and track.track_id not in hearted_labels
        and not _is_color_mismatch(track, mission)
    )


def get_confirmed_tracks(state: SelectionState, event: SelectionEvent) -> tuple[TrackState, ...]:
    target_upper_color = state.mission.target_upper_color

    return tuple(
        track
        for track in get_candidate_tracks(state, event)
        if target_upper_color is None or track.upper_color == target_upper_color
    )


def get_nearest_track(drone: DroneState, tracks: Sequence[TrackState]) -> TrackState | None:
    if not tracks:
        return None

    # 동률은 라벨 숫자 작은 쪽 — 입력 순서와 무관하게 결정적이다
    return min(tracks, key=lambda track: (get_horizontal_distance_m(drone, track), int(track.track_id[1:])))


def filter_options(state: SelectionState, event: SelectionEvent) -> tuple[str, ...]:
    has_candidate = bool(get_candidate_tracks(state, event))
    has_confirmed = bool(get_confirmed_tracks(state, event))

    # 표 1 과 1:1 — 모드별로 보기에 남는지
    is_allowed_by_mode = {
        "search": True,
        "approach": has_candidate,
        "aim_and_fire": has_confirmed,
        "observe": has_candidate,
        "hold": True,
        "return_home": True,
    }

    unknown_modes = [mode for mode in RULE_MODES if mode not in is_allowed_by_mode]
    if unknown_modes:
        raise ValueError(f"option filter has no rule for modes {unknown_modes}")

    return tuple(mode for mode in RULE_MODES if is_allowed_by_mode[mode])


# --- 모듈 내부 도우미 ---


def _get_hearted_labels(state: SelectionState, event: SelectionEvent) -> frozenset[str]:
    hearted_labels = set(state.mission.hearted_tracks)

    mode_result = event.mode_result
    is_just_hearted = (
        event.kind == EVENT_MODE_ENDED
        and mode_result is not None
        and mode_result.rule_mode == "aim_and_fire"
        and mode_result.outcome == "success"
        and mode_result.target_track is not None
    )
    if is_just_hearted:
        hearted_labels.add(mode_result.target_track)

    return frozenset(hearted_labels)


def _is_color_mismatch(track: TrackState, mission: MissionSummary) -> bool:
    if mission.target_upper_color is None or track.upper_color is None:
        return False
    return track.upper_color != mission.target_upper_color
