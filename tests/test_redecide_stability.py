"""재판단 안정성 — 실측(run6, 규칙 선택기)에서 본 문제의 회귀 테스트.

3. aim_and_fire 연사 중 sim 시계가 튀어(벽시계 0.3 s 에 1.4 s) 한두 step 놓친 것이 유예를 넘은 failure 가 됐다.
"""

from modules.decision.interface.state import ModeResult
from modules.flight_control.interface import CommandAck, FireHeart, Telemetry
from modules.perception.interface import Track
from modules.rule_mode.aim_and_fire import AimAndFireMode
from modules.rule_mode.interface import ModeTick, RuleModeSettings
from modules.yolo_detection.interface import Detection, Frame

TICK_SEC = 0.1


# --- 3. aim_and_fire: 시계가 튀어도 한두 step 놓침으로 연사를 끝내지 않는다 ---

WIDTH_PX = 1280
HEIGHT_PX = 720


def _make_telemetry(time_sec: float) -> Telemetry:
    return Telemetry(
        time_sec=time_sec, north_m=0.0, east_m=0.0, down_m=-15.0, roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0,
        gimbal_pitch_deg=-30.0, gimbal_yaw_deg=0.0, battery_pct=100.0, flight_mode="hover",
    )


def _make_frame(frame_id: int, now_sec: float, has_target: bool) -> Frame:
    center_u, center_v = WIDTH_PX / 2, HEIGHT_PX / 2
    box = (center_u - 20.0, center_v - 40.0, center_u + 20.0, center_v + 60.0)  # 조준점(박스 위 40%)이 화면 가운데
    detections = (Detection(0, "person", 0.9, box, 10),) if has_target else ()

    return Frame(frame_id, now_sec, WIDTH_PX, HEIGHT_PX, detections)


class _AcceptAllControl:
    def __init__(self) -> None:
        self.fire_list: list[FireHeart] = []

    def get_telemetry(self):
        return _make_telemetry(0.0)

    def send_command(self, command):
        if isinstance(command, FireHeart):
            self.fire_list.append(command)
        return CommandAck(True, "")


def _step_aim(mode, control, frame_id: int, now_sec: float, has_target: bool) -> ModeResult | None:
    track = Track(10, "person", 20.0, 0.0, 0.0, now_sec, 5, 0.9, True, "red")
    tick = ModeTick(
        now_sec=now_sec,
        telemetry=_make_telemetry(now_sec),
        tracks=(track,),
        frame=_make_frame(frame_id, now_sec, has_target),
        frame_telemetry=_make_telemetry(now_sec),
        hearted_track_ids=frozenset(),
    )
    return mode.step(tick, control)


def _run_aim_with_clock_jump(jump_sec: float) -> tuple[ModeResult | None, int]:
    """5발을 쏜 뒤 한 step 놓치고, 시계가 jump_sec 뛴 다음 step 도 놓치고, 그 뒤로는 다시 보인다."""
    mode = AimAndFireMode({"target_track": "T10"}, RuleModeSettings(target_upper_color="red"))
    control = _AcceptAllControl()
    now_sec = 0.0
    frame_id = 0
    result = None

    while len(control.fire_list) < 5 and result is None:
        result = _step_aim(mode, control, frame_id, now_sec, has_target=True)
        frame_id, now_sec = frame_id + 1, round(now_sec + TICK_SEC, 6)

    for has_target, advance_sec in ((False, jump_sec), (False, TICK_SEC)):
        if result is None:
            result = _step_aim(mode, control, frame_id, now_sec, has_target)
            frame_id, now_sec = frame_id + 1, round(now_sec + advance_sec, 6)

    for _ in range(40):
        if result is not None:
            break
        result = _step_aim(mode, control, frame_id, now_sec, has_target=True)
        frame_id, now_sec = frame_id + 1, round(now_sec + TICK_SEC, 6)

    return result, len(control.fire_list)


def test_aim_and_fire_survives_clock_jump_with_brief_loss():
    """run6 T10: 5발 뒤 sim 시계가 68.6→70.0 으로 뛰어 failure. 이제는 연사를 끝까지 쏜다."""
    result, fire_count = _run_aim_with_clock_jump(jump_sec=1.3)

    assert result == ModeResult("aim_and_fire", "success", "T10")
    assert fire_count == 10


def test_aim_and_fire_still_fails_on_sustained_loss():
    mode = AimAndFireMode({"target_track": "T10"}, RuleModeSettings(target_upper_color="red"))
    control = _AcceptAllControl()
    results = [
        _step_aim(mode, control, frame_id, frame_id * TICK_SEC, has_target=frame_id < 4) for frame_id in range(20)
    ]

    finished = [result for result in results if result is not None]
    assert finished == [ModeResult("aim_and_fire", "failure", "T10")]
    assert results.index(finished[0]) * TICK_SEC - 0.4 > RuleModeSettings().target_lost_grace_sec  # 유예가 지난 뒤
