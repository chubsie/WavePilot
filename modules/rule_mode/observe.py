"""observe 모드 — 제자리에서 대상을 화면 가운데에 맞춰 따라 보며 상체 색이 확정되기를 기다린다 (RM-06, D-16, D-17).

새 프레임마다 대상 박스로 중앙 맞춤 명령(SetYaw + SetGimbal)을 보낸다. 위치 명령과 발사는 없다.
대상 Track.upper_color 가 None 이 아니게 되면 success.

놓침: track 이 없거나 비활성, 또는 프레임이 있는데 대상 박스가 없는 상태가 target_lost_grace_sec 를 넘으면 failure.
timeout_sec 경과는 timeout(이미 제자리 호버라 추가 명령 없음), 명령 거절은 failure (D-04).
끝난 뒤 step 이 다시 불리면 None 을 돌려준다.
"""

import logging

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import FlightControl
from modules.rule_mode.centering import (
    TargetLossTimer,
    compute_centering,
    find_target_detection,
    find_target_track,
    parse_positive_param,
    parse_target_track,
    send_centering,
)
from modules.rule_mode.interface import ModeParams, ModeTick, RuleModeSettings, track_label

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 10.0
# observe params 에는 비율이 없어 상수로 둔다 (발사가 없어 중앙 판정 결과는 쓰지 않는다)
OBSERVE_CENTER_TOLERANCE_RATIO = 0.05


class ObserveMode:
    rule_mode = "observe"

    def __init__(self, params: ModeParams, settings: RuleModeSettings) -> None:
        self._target_id = parse_target_track(params.get("target_track"), self.rule_mode)
        self._timeout_sec = parse_positive_param(params, "timeout_sec", DEFAULT_TIMEOUT_SEC, self.rule_mode)
        self._settings = settings
        self._target_label = track_label(self._target_id)
        self._loss_timer = TargetLossTimer(settings.target_lost_grace_sec)

        self._start_sec: float | None = None
        self._last_frame_id: int | None = None
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            return None

        if self._start_sec is None:
            self._start_sec = tick.now_sec

        detection = find_target_detection(tick.frame, self._target_id)
        track = find_target_track(tick.tracks, self._target_id)
        is_track_missing = track is None or not track.is_active
        is_box_missing = tick.frame is not None and detection is None

        if self._loss_timer.update(tick.now_sec, is_track_missing or is_box_missing):
            return self._finish("failure")

        if tick.now_sec - self._start_sec >= self._timeout_sec:
            return self._finish("timeout")

        is_new_frame = tick.frame is not None and tick.frame.frame_id != self._last_frame_id
        if is_new_frame and detection is not None:
            self._last_frame_id = tick.frame.frame_id
            frame_telemetry = tick.frame_telemetry if tick.frame_telemetry is not None else tick.telemetry
            command = compute_centering(
                detection, tick.frame, frame_telemetry, self._settings.camera, OBSERVE_CENTER_TOLERANCE_RATIO
            )
            if not send_centering(control, command, self._target_label, self.rule_mode):
                return self._finish("failure")

        if track is not None and track.upper_color is not None:
            logger.info(
                "observe %s upper_color=%s ratio=%s votes=%d",
                self._target_label,
                track.upper_color,
                track.upper_color_ratio,
                track.upper_color_vote_count,
            )
            return self._finish("success")

        return None

    def _finish(self, outcome: str) -> ModeResult:
        self._is_finished = True
        logger.info("observe %s finished: %s", self._target_label, outcome)

        return ModeResult(self.rule_mode, outcome, self._target_label)
