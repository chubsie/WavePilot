"""BrowserFlightControl — 브라우저 시뮬레이터를 FlightControl 계약으로 노출한다 (SIM-03, D-05).

텔레메트리는 브라우저가 마지막으로 보낸 값을 그대로 돌려준다(오래됐어도 반환 — 끊김 판정은 send_command 몫).
명령은 링크가 살아 있고 페이지가 AI 모드일 때만 큐에 넣는다. 실제 배달은 다음 텔레메트리 응답에 실려 간다.
"""

from modules.flight_control.interface import Command, CommandAck, Telemetry
from modules.simulation.browser.bridge import SimBridge

NOT_CONNECTED_REASON = "simulator not connected"
MANUAL_MODE_REASON = "manual mode"


class BrowserFlightControl:
    def __init__(self, bridge: SimBridge) -> None:
        self._bridge = bridge

    def get_telemetry(self) -> Telemetry:
        telemetry = self._bridge.latest_telemetry()
        if telemetry is None:
            raise RuntimeError(NOT_CONNECTED_REASON)
        return telemetry

    def send_command(self, command: Command) -> CommandAck:
        if not self._bridge.is_connected():
            return CommandAck(False, NOT_CONNECTED_REASON)
        if not self._bridge.ai_mode:
            return CommandAck(False, MANUAL_MODE_REASON)

        command_id = self._bridge.enqueue_command(command)
        return CommandAck(True, f"queued {command.KIND} id={command_id}")
