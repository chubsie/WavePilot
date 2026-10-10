"""운용자 입력 계약 (C2, D-01/D-02).

운용자 링크 = 장갑/브라우저를 든 운용자가 보내는 heartbeat 다.
브리지(HTTP) 자체의 연결과는 다르다 — 브리지가 살아 있어도 운용자 heartbeat 가 끊기면 링크 두절이다(D-01).
"""

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """운용자 링크 상태 — 연결 여부와 임무 시계(sim 시각) 기준 마지막 heartbeat 시각."""

    connected: bool
    last_heartbeat_sec: float | None  # 받은 적 없으면 None

    def __post_init__(self) -> None:
        if not isinstance(self.connected, bool):
            raise ValueError(f"field 'connected' must be a bool, got {self.connected!r}")

        heartbeat = self.last_heartbeat_sec
        is_finite_number = (
            isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool) and math.isfinite(heartbeat)
        )
        if heartbeat is not None and not is_finite_number:
            raise ValueError(f"field 'last_heartbeat_sec' must be None or a finite number, got {heartbeat!r}")


@runtime_checkable
class OperatorInput(Protocol):
    """임무 루프가 운용자 쪽에서 읽는 두 값.

    구현 예: 브라우저 브리지 어댑터(05-06 run_mission), 헤드리스 ScriptedOperator.
    """

    def is_ai_requested(self) -> bool:
        """조종자 모드 버튼(브리지 ai_mode)이 AI 를 요청 중인가."""
        ...

    def link_status(self, now_sec: float) -> LinkStatus:
        ...


@dataclass(slots=True)
class ScriptedOperator:
    """헤드리스용 운용자. 기본은 AI 요청 + 링크 연결이고, 테스트가 속성을 바꿔 상황을 주입한다."""

    ai_requested: bool = True
    link_connected: bool = True
    _last_heartbeat_sec: float | None = field(default=None, init=False, repr=False)

    def is_ai_requested(self) -> bool:
        return self.ai_requested

    def link_status(self, now_sec: float) -> LinkStatus:
        # 연결 중이면 지금 heartbeat 를 받은 것으로 보고, 끊기면 마지막 값을 그대로 둔다
        if self.link_connected:
            self._last_heartbeat_sec = now_sec

        return LinkStatus(self.link_connected, self._last_heartbeat_sec)
