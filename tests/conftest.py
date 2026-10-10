"""테스트 공용 fixture.

tests/ 에는 __init__.py 를 두지 않는다 — pytest 가 rootdir 기준 경로로 모듈을 잡게 해서
패키지 이름 충돌을 피한다 (RESEARCH Pitfall 10). import 경로는 pyproject 의 pythonpath=["."] 가 잡는다.
"""

from pathlib import Path

import pytest


@pytest.fixture
def wavepilot_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def scenarios_dir(wavepilot_root: Path) -> Path:
    return wavepilot_root / "scenarios"


@pytest.fixture
def hover_telemetry():
    # interfaces 는 fixture 안에서 import 한다 — 이 fixture 를 안 쓰는 테스트(경계 검사 등)가
    # interfaces 구현 여부와 무관하게 수집되도록.
    from interfaces.flight_control import Telemetry

    # 감시 위치 (0,0) 고도 40 m, 짐벌 -55° — SKELETON 카메라 기본값과 같다
    return Telemetry(
        time_sec=0.0,
        north_m=0.0,
        east_m=0.0,
        down_m=-40.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        yaw_deg=0.0,
        gimbal_pitch_deg=-55.0,
        gimbal_yaw_deg=0.0,
        battery_pct=100.0,
        flight_mode="hover",
    )


@pytest.fixture
def default_camera():
    from interfaces.geometry import DEFAULT_CAMERA

    return DEFAULT_CAMERA
