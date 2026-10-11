"""스크립트 월드 대상 탐지 소스 — YOLO 와 트래커 없이 rule_mode 를 헤드리스로 돌리기 위한 입력.

월드(NED)에 놓은 대상들을 FlightControl 의 현재 텔레메트리로 1280x720 YOLO 형 Frame 에 투영한다.
헤드리스 rule_mode 테스트와 03-05 mock 임무가 YOLO+트래커 자리를 모델 없이 대신할 때 쓴다.

박스는 발 점과 머리 점(발 위 height_m)을 각각 투영해 만든다. 그래서 박스 밑변 중앙이 정확히 발 픽셀이고,
approach 의 지면 투영 추정이 기하 정답과 일치한다. 프레임마다 그 순간의 텔레메트리를 기록해 두고
telemetry_for(timestamp_sec) 로 돌려준다 (frame_telemetry — 촬영 순간 자세).
배우 색은 upper_color 로 그대로 실어 보낸다(HSV 없음, D-07).
"""

import math
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from modules.flight_control.interface import FlightControl, Telemetry
from modules.perception.interface import DEFAULT_CAMERA, CameraIntrinsics, project_ned_to_pixel
from modules.yolo_detection.interface import COCO_CLASS_IDS, Detection, Frame
from modules.yolo_detection.interface.detection import UPPER_COLORS

DEFAULT_CONFIDENCE = 0.9
BOX_WIDTH_RATIO = 0.4
MIN_BOX_HEIGHT_PX = 4.0
TELEMETRY_HISTORY_SIZE = 100
TIMESTAMP_TOLERANCE_SEC = 1e-9


@dataclass(frozen=True, slots=True)
class ScriptedTarget:
    track_id: int
    class_name: str
    north_m: float
    east_m: float
    height_m: float = 1.7
    ground_down_m: float = 0.0
    north_mps: float = 0.0
    east_mps: float = 0.0
    upper_color: str | None = None

    def __post_init__(self) -> None:
        if self.class_name not in COCO_CLASS_IDS:
            raise ValueError(f"unknown class_name {self.class_name!r}, expected one of {sorted(COCO_CLASS_IDS)}")
        if not self.height_m > 0:
            raise ValueError(f"field 'height_m' must be > 0, got {self.height_m!r}")

        if self.upper_color is not None:
            if self.upper_color not in UPPER_COLORS:
                raise ValueError(f"field 'upper_color' must be one of {UPPER_COLORS} or None, got {self.upper_color!r}")
            if self.class_name != "person":
                raise ValueError(f"field 'upper_color' is only for person targets, got class_name {self.class_name!r}")

    def get_position_ned(self, time_sec: float) -> tuple[float, float, float]:
        """time_sec 시점의 발 점 (north, east, down)."""
        return (
            self.north_m + self.north_mps * time_sec,
            self.east_m + self.east_mps * time_sec,
            self.ground_down_m,
        )


class ScriptedTargetSource:
    def __init__(
        self,
        flight_control: FlightControl,
        targets: Sequence[ScriptedTarget],
        camera: CameraIntrinsics = DEFAULT_CAMERA,
        confidence: float = DEFAULT_CONFIDENCE,
    ) -> None:
        track_id_list = [target.track_id for target in targets]
        if len(set(track_id_list)) != len(track_id_list):
            raise ValueError(f"duplicate track_id in targets: {track_id_list}")

        self._flight_control = flight_control
        self._targets = list(targets)
        self._camera = camera
        self._confidence = confidence
        self._frame_index = 0
        self._telemetry_history: OrderedDict[float, Telemetry] = OrderedDict()

    @property
    def targets(self) -> tuple[ScriptedTarget, ...]:
        return tuple(self._targets)

    def remove_target(self, track_id: int) -> None:
        """대상을 월드에서 치운다. 없는 id 면 아무 일도 하지 않는다."""
        self._targets = [target for target in self._targets if target.track_id != track_id]

    def read_frame(self) -> Frame:
        telemetry = self._flight_control.get_telemetry()
        self._record_telemetry(telemetry)

        detections = tuple(
            detection
            for target in sorted(self._targets, key=lambda target: target.track_id)
            if (detection := self._project_target(target, telemetry)) is not None
        )
        frame = Frame(
            frame_id=self._frame_index,
            timestamp_sec=telemetry.time_sec,
            image_width=int(self._camera.width_px),
            image_height=int(self._camera.height_px),
            detections=detections,
        )
        self._frame_index += 1

        return frame

    def telemetry_for(self, timestamp_sec: float) -> Telemetry | None:
        """프레임 timestamp_sec 를 찍은 순간의 텔레메트리. 모르는 시각이면 None."""
        for recorded_sec, telemetry in self._telemetry_history.items():
            if math.isclose(recorded_sec, timestamp_sec, rel_tol=0.0, abs_tol=TIMESTAMP_TOLERANCE_SEC):
                return telemetry

        return None

    def _record_telemetry(self, telemetry: Telemetry) -> None:
        self._telemetry_history[telemetry.time_sec] = telemetry
        while len(self._telemetry_history) > TELEMETRY_HISTORY_SIZE:
            self._telemetry_history.popitem(last=False)

    def _project_target(self, target: ScriptedTarget, telemetry: Telemetry) -> Detection | None:
        feet_north, feet_east, feet_down = target.get_position_ned(telemetry.time_sec)
        feet = project_ned_to_pixel((feet_north, feet_east, feet_down), self._camera, telemetry)
        head = project_ned_to_pixel((feet_north, feet_east, feet_down - target.height_m), self._camera, telemetry)
        if feet is None or head is None:
            return None

        for u, v in (feet, head):
            if not 0.0 <= u <= self._camera.width_px or not 0.0 <= v <= self._camera.height_px:
                return None

        box_height_px = feet[1] - head[1]
        if box_height_px < MIN_BOX_HEIGHT_PX:
            return None

        half_width_px = BOX_WIDTH_RATIO * box_height_px / 2.0
        x1 = max(0.0, feet[0] - half_width_px)
        x2 = min(float(self._camera.width_px), feet[0] + half_width_px)

        return Detection(
            class_id=COCO_CLASS_IDS[target.class_name],
            class_name=target.class_name,
            confidence=self._confidence,
            xyxy=(x1, head[1], x2, feet[1]),
            track_id=target.track_id,
            upper_color=target.upper_color,
        )
