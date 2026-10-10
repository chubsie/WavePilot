"""시나리오 기반 탐지 소스 — 시나리오 객체를 YOLO 형 Frame 으로 내보낸다 (MOCK-01, IF-01).

프레임 i 의 시각은 i / fps 이고, 시각이 시나리오 길이를 넘으면 None 으로 끝을 알린다.
카메라 자세는 FlightControl Protocol 의 get_telemetry() 로만 얻는다 — MockFlightControl 같은
구체 클래스를 모르므로, 나중에 ArduPilot SITL 로 바꿔도 이 파일은 그대로다.
"""

import random

from interfaces.detection import COCO_CLASS_IDS, Detection, Frame
from interfaces.flight_control import FlightControl
from interfaces.geometry import CameraIntrinsics
from mocks.camera import project_object_box
from mocks.clock import SimClock
from mocks.scenario import Scenario

DEFAULT_FPS = 10.0
END_TOLERANCE_SEC = 1e-9  # i / fps 부동소수 오차로 마지막 프레임(t = duration)을 잃지 않게


class ScenarioDetectionSource:
    def __init__(
        self,
        scenario: Scenario,
        flight_control: FlightControl,
        camera: CameraIntrinsics,
        clock: SimClock,
        fps: float = DEFAULT_FPS,
        rng: random.Random | None = None,
    ) -> None:
        if fps <= 0:
            raise ValueError(f"fps must be > 0, got {fps}")

        self._scenario = scenario
        self._flight_control = flight_control
        self._camera = camera
        self._clock = clock
        self._fps = fps
        # Phase 2 탐지 불완전성(MOCK-03) 주입 자리 — 전역 random 대신 주입된 인스턴스만 쓴다. 지금은 미사용
        self._rng = rng
        self._frame_index = 0

    def read_frame(self) -> Frame | None:
        time_sec = self._frame_index / self._fps
        if time_sec > self._scenario.duration_sec + END_TOLERANCE_SEC:
            return None

        self._clock.advance_to(time_sec)
        telemetry = self._flight_control.get_telemetry()

        detections: list[Detection] = []
        for obj in self._scenario.objects:
            north_m, east_m = obj.position_at(time_sec)
            box = project_object_box(north_m, east_m, obj.size_m, self._camera, telemetry)
            if box is None:
                continue
            detections.append(
                Detection(
                    class_id=COCO_CLASS_IDS[obj.kind],
                    class_name=obj.kind,
                    confidence=obj.confidence,
                    xyxy=box,
                    track_id=obj.track_id,
                )
            )

        frame = Frame(
            frame_id=self._frame_index,
            timestamp_sec=round(time_sec, 3),
            image_width=self._camera.width_px,
            image_height=self._camera.height_px,
            detections=tuple(detections),
        )
        self._frame_index += 1
        return frame
