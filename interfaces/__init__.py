"""WavePilot 공유 인터페이스(계약) 패키지.

시뮬레이터 개발자는 이 패키지와 llm_module 최상위 파사드만 알면 된다 (D-04).
탐지 소스·비행 제어·통신은 여기 Protocol 을 구현해 끼우고, 데이터는 여기 dataclass 로 주고받는다.
구현체(mocks, comm, perception)는 이 패키지를 import 하지만, 이 패키지는 어떤 구현체도 import 하지 않는다.
"""

from interfaces.detection import COCO_CLASS_IDS, Detection, DetectionSource, Frame
from interfaces.flight_control import (
    FLIGHT_MODES,
    Command,
    CommandAck,
    FlightControl,
    GoTo,
    Land,
    ReturnToHome,
    TakeOff,
    Telemetry,
    command_from_dict,
)
from interfaces.track import Track

__all__ = [
    "COCO_CLASS_IDS",
    "Detection",
    "DetectionSource",
    "Frame",
    "FLIGHT_MODES",
    "Command",
    "CommandAck",
    "FlightControl",
    "GoTo",
    "Land",
    "ReturnToHome",
    "TakeOff",
    "Telemetry",
    "command_from_dict",
    "Track",
]
