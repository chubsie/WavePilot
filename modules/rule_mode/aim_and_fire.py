"""aim_and_fire 모드 — 대상을 화면 가운데에 맞춘 뒤 하트를 연사한다 (RM-02, HRT-01, D-08~D-14).

새 프레임마다 대상 박스로 중앙 맞춤 명령(SetYaw + SetGimbal)을 보낸다. 위치 명령은 없다(제자리 호버, D-08).
연사 시작 전에는 중앙 프레임이 연속 stable_frame_count 개면 첫 발을 쏜다. 한 프레임이라도 벗어나거나 놓치면 안정
카운트는 0 (D-09, D-10). 연사는 heart_burst_count 발을 heart_burst_interval_sec 간격으로 보내고, 그동안에도 조준
명령은 계속 나간다 (D-12). 모든 발 수락 → success, 어떤 발이든 거절 → 즉시 failure (D-14). hearted 기록은 임무
루프의 몫이며 모드는 모른다.

놓침이 target_lost_grace_sec 를 넘으면 failure, timeout_sec 경과는 timeout (D-11). 끝난 뒤 step 은 None.
"""

import logging

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import FireHeart, FlightControl
from modules.rule_mode.centering import (
    TargetLossTimer,
    compute_centering,
    find_target_detection,
    find_target_track,
    parse_frame_count_param,
    parse_positive_param,
    parse_ratio_param,
    parse_target_track,
    send_centering,
)
from modules.rule_mode.interface import ModeParams, ModeTick, RuleModeSettings, track_label

logger = logging.getLogger(__name__)

# decision/params.py ParamDefaults(aim_*) 와 같은 값
DEFAULT_CENTER_TOLERANCE_RATIO = 0.05
DEFAULT_STABLE_FRAME_COUNT = 5
DEFAULT_TIMEOUT_SEC = 15.0
# 부동소수 시각 누적 오차 때문에 간격 비교에 두는 여유
FIRE_INTERVAL_EPSILON_SEC = 1e-6


class AimAndFireMode:
    rule_mode = "aim_and_fire"

    def __init__(self, params: ModeParams, settings: RuleModeSettings) -> None:
        self._target_id = parse_target_track(params.get("target_track"), self.rule_mode)
        self._tolerance_ratio = parse_ratio_param(
            params, "center_tolerance_ratio", DEFAULT_CENTER_TOLERANCE_RATIO, self.rule_mode
        )
        self._required_stable_count = parse_frame_count_param(
            params, "stable_frame_count", DEFAULT_STABLE_FRAME_COUNT, self.rule_mode
        )
        self._timeout_sec = parse_positive_param(params, "timeout_sec", DEFAULT_TIMEOUT_SEC, self.rule_mode)
        self._settings = settings
        self._target_label = track_label(self._target_id)
        self._loss_timer = TargetLossTimer(settings.target_lost_grace_sec)

        self._start_sec: float | None = None
        self._last_frame_id: int | None = None
        self._stable_frame_count = 0
        self._fired_count = 0
        self._last_fire_sec: float | None = None
        self._is_finished = False

    def step(self, tick: ModeTick, control: FlightControl) -> ModeResult | None:
        if self._is_finished:
            return None

        if self._start_sec is None:
            self._start_sec = tick.now_sec

        detection = find_target_detection(tick.frame, self._target_id)
        track = find_target_track(tick.tracks, self._target_id)
        is_missing = track is None or not track.is_active or (tick.frame is not None and detection is None)

        if is_missing:
            self._stable_frame_count = 0
        if self._loss_timer.update(tick.now_sec, is_missing):
            return self._finish("failure")

        if tick.now_sec - self._start_sec >= self._timeout_sec:
            return self._finish("timeout")

        is_new_frame = tick.frame is not None and tick.frame.frame_id != self._last_frame_id
        if is_new_frame and detection is not None:
            self._last_frame_id = tick.frame.frame_id
            frame_telemetry = tick.frame_telemetry if tick.frame_telemetry is not None else tick.telemetry
            command = compute_centering(
                detection, tick.frame, frame_telemetry, self._settings.camera, self._tolerance_ratio
            )
            if not send_centering(control, command, self._target_label, self.rule_mode):
                return self._finish("failure")

            if not self._is_firing():
                self._stable_frame_count = self._stable_frame_count + 1 if command.is_centered else 0

        if self._is_fire_due(tick.now_sec):
            if not self._fire(tick.now_sec, control):
                return self._finish("failure")

        if self._fired_count >= self._settings.heart_burst_count:
            return self._finish("success")

        return None

    def _is_firing(self) -> bool:
        return self._fired_count > 0

    def _is_fire_due(self, now_sec: float) -> bool:
        if not self._is_firing():
            return self._stable_frame_count >= self._required_stable_count

        elapsed_sec = now_sec - self._last_fire_sec  # type: ignore[operator]
        return elapsed_sec >= self._settings.heart_burst_interval_sec - FIRE_INTERVAL_EPSILON_SEC

    def _fire(self, now_sec: float, control: FlightControl) -> bool:
        """하트 한 발을 보낸다. 거절되면 False (D-14)."""
        ack = control.send_command(FireHeart(self._target_id))
        if not ack.accepted:
            logger.warning("aim_and_fire %s heart rejected: %s", self._target_label, ack.reason)
            return False

        self._fired_count += 1
        self._last_fire_sec = now_sec
        logger.info(
            "aim_and_fire %s heart %d/%d", self._target_label, self._fired_count, self._settings.heart_burst_count
        )

        return True

    def _finish(self, outcome: str) -> ModeResult:
        self._is_finished = True
        logger.info("aim_and_fire %s finished: %s", self._target_label, outcome)

        return ModeResult(self.rule_mode, outcome, self._target_label)
