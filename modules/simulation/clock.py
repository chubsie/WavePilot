"""시뮬레이션 시계 — mock 월드의 시각 주인.

mock 탐지 소스가 프레임마다 시각을 앞으로 밀고, mock 비행 제어는 같은 시계를 읽어
텔레메트리 시각·배터리를 계산한다. 벽시계에 의존하지 않으므로 `--speed 0` 실행이 결정론적이다.
실제 구현(YOLO 어댑터, ArduPilot)은 각자 자기 시계를 가져오므로 이 클래스는 simulation 전용이다.
"""


class SimClock:
    def __init__(self) -> None:
        self._now_sec = 0.0

    @property
    def now_sec(self) -> float:
        return self._now_sec

    def advance_to(self, time_sec: float) -> None:
        """시각은 앞으로만 간다 — 되돌아가면 텔레메트리·track 시각이 뒤엉킨다."""
        if time_sec < self._now_sec:
            raise ValueError(f"sim time cannot go backwards: {self._now_sec} -> {time_sec}")

        self._now_sec = time_sec
