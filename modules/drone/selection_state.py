"""텔레메트리 + track 을 선택기가 보는 SelectionState 로 바꾼다 (RM-07).

값은 반올림하지 않고 원값 그대로 담는다. 반올림(소수 1자리, 신뢰도 2자리, -0.0 -> 0.0)은 선택기가
상태를 직렬화할 때 build_request_state 가 한다. 그래야 options.py 의 거리 계산이 정확하다.

상태 JSON 에는 짧은 토큰만 들어가야 한다. YOLO 클래스 이름은 소문자·"_" 토큰으로 바꾸고,
그래도 CLASS_NAME_PATTERN 에 맞지 않는 track 은 상태에서 뺀다.
"""

import logging
from collections.abc import Iterable, Sequence

from modules.decision.interface.state import (
    CLASS_NAME_PATTERN,
    DroneState,
    MissionSummary,
    SelectionState,
    TrackState,
)
from modules.flight_control.interface import Telemetry
from modules.perception.interface import Track
from modules.rule_mode.interface import track_label

logger = logging.getLogger("modules.drone.selection_state")


def build_selection_state(
    now_sec: float,
    telemetry: Telemetry,
    tracks: Sequence[Track],
    target_class: str,
    hearted_labels: Iterable[str] = (),
) -> SelectionState:
    track_state_list = []
    for track in tracks:
        track_state = _build_track_state(track)
        if track_state is not None:
            track_state_list.append(track_state)

    return SelectionState(
        time_sec=now_sec,
        drone=_build_drone_state(telemetry),
        tracks=tuple(track_state_list),
        mission=MissionSummary(target_class, None, tuple(hearted_labels)),
    )


# --- 모듈 내부 도우미 ---


def _build_drone_state(telemetry: Telemetry) -> DroneState:
    return DroneState(
        north_m=telemetry.north_m,
        east_m=telemetry.east_m,
        down_m=telemetry.down_m,
        altitude_m=telemetry.altitude_m,
        roll_deg=telemetry.roll_deg,
        pitch_deg=telemetry.pitch_deg,
        yaw_deg=telemetry.yaw_deg,
        gimbal_pitch_deg=telemetry.gimbal_pitch_deg,
        gimbal_yaw_deg=telemetry.gimbal_yaw_deg,
        battery_pct=telemetry.battery_pct,
        flight_mode=telemetry.flight_mode,
    )


def _build_track_state(track: Track) -> TrackState | None:
    class_token = _to_class_token(track.class_name)
    if not CLASS_NAME_PATTERN.match(class_token):
        logger.debug("dropped track %s: class %r is not a safe token", track_label(track.track_id), track.class_name)
        return None

    return TrackState(
        track_id=track_label(track.track_id),
        class_name=class_token,
        north_m=track.north_m,
        east_m=track.east_m,
        first_seen_sec=track.first_seen_sec,
        last_seen_sec=track.last_seen_sec,
        observation_count=track.observation_count,
        mean_confidence=min(1.0, max(0.0, track.mean_confidence)),  # 부동소수 오차 보정
        is_active=track.is_active,
        upper_color=None,  # Phase 4 가 채운다
    )


def _to_class_token(class_name: str) -> str:
    return class_name.strip().lower().replace(" ", "_").replace("-", "_")
