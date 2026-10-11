"""BridgeCommLink — 운용자 TCP 링크 대신 파이프라인 state·report 를 SimBridge 로 넘긴다 (run_scenario --operator browser).

페이지가 /sim/telemetry 응답의 "pipeline" 키로 최신 state·report 를 읽는다. 브라우저에서 오는 운용자 명령은 없으므로
receive 는 항상 None 이다 — 파이프라인이 timeout 0.0 으로 매 틱 부르므로 블로킹하면 안 된다.
"""

from modules.comm.interface import LinkStatus, Message
from modules.simulation.browser.bridge import PIPELINE_MESSAGE_KINDS, SimBridge

BROWSER_PEER = "browser"


class BridgeCommLink:
    def __init__(self, bridge: SimBridge) -> None:
        self._bridge = bridge

    def send(self, message: Message) -> None:
        # telemetry 는 브라우저가 이미 원본을 갖고 있어 버린다
        if message.type not in PIPELINE_MESSAGE_KINDS:
            return

        entry = {"seq": message.seq, "sent_at": message.sent_at, "payload": message.payload}
        self._bridge.publish_pipeline_message(message.type, entry)

    def receive(self, timeout_sec: float | None = None) -> Message | None:
        return None

    def link_status(self) -> LinkStatus:
        return LinkStatus(connected=self._bridge.is_connected(), peer=BROWSER_PEER, last_received_at=None)

    def close(self) -> None:
        # 브리지는 run_scenario 가 소유하고 닫는다
        pass
