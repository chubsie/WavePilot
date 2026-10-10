"""브라우저 시뮬레이터 명령 데모 — 서버를 띄우고 FlightControl 로 드론을 한 바퀴 몰아 본다 (SIM-02, SIM-03 수동 확인).

실행 (저장소 루트에서):
    uv run python -m modules.drone.sim_demo
    실시간 YOLO 박스까지: uv run --group yolo python -m modules.drone.sim_demo --live
    → 브라우저(Chrome/Edge)로 http://127.0.0.1:8765/drone_camera.html 열고 M 으로 AI 모드

GoTo → SetYaw → SetGimbal → SetVelocity → FireHeart → Land 를 차례로 보내고(D-06, D-07),
각 단계를 지키는 동안 GPS 를 포함한 텔레메트리 줄을 찍는다.
drone/ 은 컴포지션 루트라 --live 에서만 YOLO 구현(02-03)을 함수 안에서 늦게 import 한다.
"""

import argparse
import functools
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from modules.flight_control.interface import (
    Command,
    FireHeart,
    FlightControl,
    GoTo,
    Land,
    ReturnToHome,
    SetGimbal,
    SetVelocity,
    SetYaw,
    Telemetry,
)
from modules.simulation.browser.bridge import SimBridge
from modules.simulation.browser.flight_control import BrowserFlightControl
from modules.simulation.browser.server import DEFAULT_HOST, DEFAULT_PORT, start_sim_server

POLL_INTERVAL_SEC = 0.5
WAIT_HINT_INTERVAL_SEC = 2.0
AI_MODE_HINT = "open the page and press M for AI mode"


@dataclass(frozen=True, slots=True)
class DemoStep:
    command: Command
    hold_sec: float


def build_demo_steps() -> list[DemoStep]:
    # 값은 X-02/X-03 클램프 안쪽 — 브라우저가 잘라내지 않고 그대로 따른다
    return [
        DemoStep(GoTo(north_m=20.0, east_m=0.0, altitude_m=15.0), hold_sec=8.0),
        DemoStep(SetYaw(yaw_deg=90.0), hold_sec=3.0),
        DemoStep(SetGimbal(pitch_deg=-45.0, yaw_deg=30.0), hold_sec=3.0),
        DemoStep(SetVelocity(north_mps=0.0, east_mps=3.0, down_mps=0.0, duration_sec=2.0), hold_sec=3.0),
        DemoStep(FireHeart(track_id=1), hold_sec=2.0),
        # 홈 = 첫 페어링 위치 — 약 22 m 이동(6 m/s) + 15 m 하강(1 m/s)
        DemoStep(ReturnToHome(), hold_sec=22.0),
        DemoStep(Land(), hold_sec=2.0),
    ]


def format_telemetry_line(t: Telemetry) -> str:
    gps_text = "none" if t.gps is None else f"{t.gps.latitude_deg:.6f},{t.gps.longitude_deg:.6f},{t.gps.altitude_m:.1f}"

    return (
        f"t={t.time_sec:.1f}s mode={t.flight_mode} "
        f"N={t.north_m:.1f} E={t.east_m:.1f} alt={t.altitude_m:.1f} "
        f"rpy={t.roll_deg:.1f}/{t.pitch_deg:.1f}/{t.yaw_deg:.1f} "
        f"gimbal={t.gimbal_pitch_deg:.1f}/{t.gimbal_yaw_deg:.1f} "
        f"battery={t.battery_pct:.1f}% gps={gps_text}"
    )


def run_demo(
    control: FlightControl,
    steps: Sequence[DemoStep],
    *,
    sleep: Callable[[float], None] = time.sleep,
    print_fn: Callable[[str], None] = print,
    poll_interval_sec: float = POLL_INTERVAL_SEC,
) -> None:
    for step in steps:
        _send_until_accepted(control, step.command, sleep, print_fn, poll_interval_sec)

        # sleep 이 no-op 이어도 끝나도록 시간 대신 횟수로 돈다
        hold_count = max(1, round(step.hold_sec / poll_interval_sec))
        for _ in range(hold_count):
            print_fn(format_telemetry_line(control.get_telemetry()))
            sleep(poll_interval_sec)


def _send_until_accepted(
    control: FlightControl,
    command: Command,
    sleep: Callable[[float], None],
    print_fn: Callable[[str], None],
    poll_interval_sec: float,
) -> None:
    while True:
        ack = control.send_command(command)
        print_fn(f"-> {command.KIND}: {ack.reason}")
        if ack.accepted:
            return

        print_fn(f"   waiting — {AI_MODE_HINT}")
        sleep(poll_interval_sec)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive the browser drone through every command kind")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--live", action="store_true", help="run yolo11n on browser frames (needs uv --group yolo)")
    parser.add_argument("--device", default="auto", help="--live device: auto | mps | cpu | 0 (default auto)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    bridge = SimBridge()
    server = start_sim_server(bridge, args.host, args.port)
    print(f"open http://{args.host}:{args.port}/drone_camera.html (Chrome/Edge)", flush=True)

    try:
        if args.live:
            _start_live_yolo(bridge, args.device)
        _wait_for_ai_mode(bridge)
        run_demo(BrowserFlightControl(bridge), build_demo_steps())
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        server.shutdown()
        server.server_close()


def _start_live_yolo(bridge: SimBridge, device: str) -> None:
    # ultralytics·cv2 는 yolo 그룹에만 있다 — --live 일 때만 import 한다
    from modules.simulation.browser.detection_source import run_live_yolo
    from modules.yolo_detection.jpeg_source import create_jpeg_yolo_source

    yolo_factory = functools.partial(create_jpeg_yolo_source, device=device)
    threading.Thread(
        target=run_live_yolo,
        args=(bridge,),
        kwargs={"yolo_factory": yolo_factory},
        daemon=True,
        name="sim-live-yolo",
    ).start()


def _wait_for_ai_mode(bridge: SimBridge) -> None:
    next_hint_sec = 0.0

    while not (bridge.is_connected() and bridge.ai_mode):
        now_sec = time.monotonic()
        if now_sec >= next_hint_sec:
            print(f"waiting for the simulator page — {AI_MODE_HINT}", flush=True)
            next_hint_sec = now_sec + WAIT_HINT_INTERVAL_SEC
        time.sleep(0.2)


if __name__ == "__main__":
    main()
