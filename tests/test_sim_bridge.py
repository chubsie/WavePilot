"""SimBridge — 텔레메트리·연결 판정·명령 1회 배달·이벤트·최신 프레임 슬롯·탐지 상태 (D-09, D-11, D-12)."""

import dataclasses
import threading

import pytest

from modules.flight_control.interface import FireHeart, GoTo, Telemetry
from modules.simulation.browser.bridge import (
    HEART_SHOT_LIMIT,
    LINK_TIMEOUT_SEC,
    OPERATOR_LINK_TIMEOUT_SEC,
    RECENT_EVENT_LIMIT,
    BrowserFrame,
    SimBridge,
)


class FakeClock:
    def __init__(self, now_sec: float = 0.0) -> None:
        self.now_sec = now_sec

    def __call__(self) -> float:
        return self.now_sec


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bridge(clock: FakeClock) -> SimBridge:
    return SimBridge(clock=clock)


def _make_frame(seq: int, telemetry: Telemetry, received_at_sec: float = 0.0) -> BrowserFrame:
    return BrowserFrame(
        seq=seq,
        timestamp_sec=received_at_sec,
        jpeg_bytes=b"\xff\xd8",
        telemetry=telemetry,
        received_at_sec=received_at_sec,
    )


def _run_wait_frame(bridge: SimBridge, after_seq: int, timeout_sec: float) -> tuple[threading.Thread, list]:
    results: list = []
    started = threading.Event()

    def wait() -> None:
        started.set()
        results.append(bridge.wait_frame(after_seq, timeout_sec))

    thread = threading.Thread(target=wait)
    thread.start()
    started.wait(timeout=1.0)
    return thread, results


# --- 초기 상태 ---


def test_new_bridge_is_empty(bridge: SimBridge) -> None:
    assert bridge.latest_telemetry() is None
    assert bridge.is_connected() is False
    assert bridge.ai_mode is False
    assert bridge.drain_commands() == []
    assert bridge.latest_detections() is None
    assert bridge.recent_events() == ()
    assert bridge.status() == {"frame_fps": 0.0, "yolo_latency_ms": None}
    assert bridge.operator_link_status() == (False, None)
    assert bridge.mission_status() is None
    assert bridge.drain_heart_shots() == []


# --- 텔레메트리·연결 ---


def test_update_from_browser_sets_telemetry_and_link(
    bridge: SimBridge, clock: FakeClock, hover_telemetry: Telemetry
) -> None:
    clock.now_sec = 10.0

    bridge.update_from_browser(hover_telemetry, True, [{"kind": "heart_hit"}])

    assert bridge.latest_telemetry() == hover_telemetry
    assert bridge.ai_mode is True
    assert bridge.is_connected() is True

    clock.now_sec = 10.0 + LINK_TIMEOUT_SEC
    assert bridge.is_connected() is True

    clock.now_sec = 11.01
    assert bridge.is_connected() is False


# --- 운용자 링크 (Phase 5 D-01, D-02) ---


def test_operator_link_heartbeat_times_out_after_timeout(
    bridge: SimBridge, clock: FakeClock, hover_telemetry: Telemetry
) -> None:
    telemetry = dataclasses.replace(hover_telemetry, time_sec=5.0)
    clock.now_sec = 10.0

    bridge.update_from_browser(telemetry, True, [], operator_link=True)

    assert bridge.operator_link_status() == (True, 5.0)

    clock.now_sec = 10.0 + OPERATOR_LINK_TIMEOUT_SEC
    assert bridge.operator_link_status() == (True, 5.0)

    clock.now_sec = 13.01
    assert bridge.operator_link_status() == (False, 5.0)


def test_operator_link_cut_keeps_bridge_connected(
    bridge: SimBridge, clock: FakeClock, hover_telemetry: Telemetry
) -> None:
    clock.now_sec = 10.0
    bridge.update_from_browser(dataclasses.replace(hover_telemetry, time_sec=5.0), True, [])

    # 링크를 끊은 뒤에도 브라우저는 계속 POST 한다 — heartbeat 만 멈춘다
    for step in range(1, 9):
        clock.now_sec = 10.0 + 0.5 * step
        cut_telemetry = dataclasses.replace(hover_telemetry, time_sec=5.0 + 0.5 * step)
        bridge.update_from_browser(cut_telemetry, True, [], operator_link=False)

    assert bridge.operator_link_status() == (False, 5.0)
    assert bridge.is_connected() is True


def test_operator_link_defaults_to_heartbeat(
    bridge: SimBridge, clock: FakeClock, hover_telemetry: Telemetry
) -> None:
    clock.now_sec = 2.0

    bridge.update_from_browser(dataclasses.replace(hover_telemetry, time_sec=7.5), False, [])

    assert bridge.operator_link_status() == (True, 7.5)


# --- 명령 큐 ---


def test_commands_are_delivered_once_with_increasing_ids(bridge: SimBridge) -> None:
    assert bridge.enqueue_command(GoTo(1.0, 2.0, 30.0)) == 1
    assert bridge.enqueue_command(FireHeart(3)) == 2

    assert bridge.drain_commands() == [
        {"id": 1, "kind": "go_to", "north_m": 1.0, "east_m": 2.0, "altitude_m": 30.0},
        {"id": 2, "kind": "fire_heart", "track_id": 3},
    ]
    assert bridge.drain_commands() == []
    assert bridge.enqueue_command(FireHeart(4)) == 3


def test_enqueue_rejects_non_command(bridge: SimBridge) -> None:
    with pytest.raises(TypeError):
        bridge.enqueue_command("go_to")


# --- 임무 상태 ---


def test_mission_status_is_copied(bridge: SimBridge) -> None:
    status = {"mode": "AI", "reason": None, "t": 1.0}

    bridge.set_mission_status(status)
    status["mode"] = "MANUAL"

    stored = bridge.mission_status()
    assert stored == {"mode": "AI", "reason": None, "t": 1.0}

    stored["mode"] = "RETURN"
    assert bridge.mission_status()["mode"] == "AI"


def test_set_mission_status_rejects_non_dict(bridge: SimBridge) -> None:
    with pytest.raises(TypeError, match="status must be a dict"):
        bridge.set_mission_status(["AI"])


# --- 이벤트 ---


def test_recent_events_keep_last_limit_in_order(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    events = [{"n": n} for n in range(60)]

    bridge.update_from_browser(hover_telemetry, False, events)

    assert RECENT_EVENT_LIMIT == 50
    assert bridge.recent_events() == tuple({"n": n} for n in range(10, 60))


def test_recent_events_are_copied(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    event = {"kind": "heart_hit"}

    bridge.update_from_browser(hover_telemetry, False, [event])
    event["kind"] = "tampered"

    assert bridge.recent_events() == ({"kind": "heart_hit"},)


# --- heart_shot 큐 (Phase 6 D-10) ---


def _make_heart_shot(track_id: int, time_sec: float, actor_id: str | None = "red_1") -> dict:
    return {"kind": "heart_shot", "track_id": track_id, "time_sec": time_sec, "actor_id": actor_id}


def test_heart_shots_are_queued_apart_from_recent_events(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    goal = {"kind": "goal_reached", "command_id": 1, "time_sec": 1.0}
    first_shot = _make_heart_shot(3, 1.1)
    second_shot = _make_heart_shot(3, 1.2, actor_id=None)

    bridge.update_from_browser(hover_telemetry, True, [goal, first_shot, second_shot])

    assert bridge.recent_events() == (goal,)
    assert bridge.drain_heart_shots() == [first_shot, second_shot]


def test_heart_shots_drain_once(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    bridge.update_from_browser(hover_telemetry, True, [_make_heart_shot(1, 0.5)])

    assert bridge.drain_heart_shots() == [_make_heart_shot(1, 0.5)]
    assert bridge.drain_heart_shots() == []


def test_heart_shots_keep_last_limit(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    shots = [_make_heart_shot(n, float(n)) for n in range(HEART_SHOT_LIMIT + 5)]

    bridge.update_from_browser(hover_telemetry, True, shots)

    assert HEART_SHOT_LIMIT == 1000
    assert bridge.drain_heart_shots() == shots[5:]


def test_heart_shots_are_copied(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    shot = _make_heart_shot(2, 3.0)

    bridge.update_from_browser(hover_telemetry, True, [shot])
    shot["actor_id"] = "tampered"

    assert bridge.drain_heart_shots() == [_make_heart_shot(2, 3.0)]


# --- 프레임 슬롯 ---


def test_frame_slot_keeps_only_latest(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    bridge.put_frame(_make_frame(1, hover_telemetry))
    bridge.put_frame(_make_frame(2, hover_telemetry))

    assert bridge.wait_frame(None, 0.0).seq == 2
    assert bridge.wait_frame(after_seq=2, timeout_sec=0.05) is None


def test_waiting_thread_receives_new_frame(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    bridge.put_frame(_make_frame(2, hover_telemetry))
    thread, results = _run_wait_frame(bridge, after_seq=2, timeout_sec=2.0)

    bridge.put_frame(_make_frame(3, hover_telemetry))
    thread.join(timeout=1.0)

    assert thread.is_alive() is False
    assert results[0].seq == 3


def test_close_releases_waiters_and_ignores_new_frames(bridge: SimBridge, hover_telemetry: Telemetry) -> None:
    thread, results = _run_wait_frame(bridge, after_seq=0, timeout_sec=2.0)

    bridge.close()
    thread.join(timeout=1.0)

    assert thread.is_alive() is False
    assert results == [None]

    bridge.put_frame(_make_frame(5, hover_telemetry))
    assert bridge.wait_frame(None, 1.0) is None


# --- 탐지·상태 ---


def test_frame_fps_counts_frames_in_window(bridge: SimBridge, clock: FakeClock, hover_telemetry: Telemetry) -> None:
    for i in range(1, 11):
        clock.now_sec = i * 0.1
        bridge.put_frame(_make_frame(i, hover_telemetry, received_at_sec=clock.now_sec))

    assert bridge.status()["frame_fps"] == 10.0

    clock.now_sec += 2.0
    assert bridge.status()["frame_fps"] == 0.0


def test_publish_detections_updates_latest_and_latency(bridge: SimBridge) -> None:
    bridge.publish_detections({"frame": 1}, 42.5)

    assert bridge.latest_detections() == {"frame": 1}
    assert bridge.status()["yolo_latency_ms"] == 42.5
