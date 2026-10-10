"""통신 인터페이스 — 드론과 운용자 사이 메시지 envelope 와 링크 계약.

envelope 는 전송 방식과 무관하다: TCP, 무선 모뎀, LTE, MAVLink 터널 어느 것이든 같은 Message 를 싣는다.
메시지 종류는 telemetry / state / report / command 4종으로 고정한다 (SC5).
판단(Decision) 결과는 별도 종류를 만들지 않고 "report" 페이로드 안에 넣어 4종을 유지한다.

link_status() 는 지금부터 둔다 — 재밍 시 자동 복귀 ADR(decisions/2026-10-10/return-to-home-on-jamming.md)이
링크 상태를 근거로 삼기 때문이다. Phase 1 에서 loss_rate 는 0.0, Phase 2 에서 실측값을 채운다.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

MESSAGE_TYPES = ("telemetry", "state", "report", "command")


@dataclass(frozen=True, slots=True)
class Message:
    type: str
    seq: int
    sent_at: str  # ISO 8601, 밀리초까지
    payload: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "seq": self.seq, "sent_at": self.sent_at, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        # 상대 프로세스에서 온 JSON 이므로 키·타입·종류를 모두 검사한다 (T-01-01)
        if not isinstance(data, dict):
            raise ValueError(f"message must be a dict, got {type(data).__name__}")

        message_type = _require_key(data, "type")
        if message_type not in MESSAGE_TYPES:
            raise ValueError(f"field 'type' must be one of {MESSAGE_TYPES}, got {message_type!r}")

        seq = _require_key(data, "seq")
        is_valid_seq = isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0
        if not is_valid_seq:
            raise ValueError(f"field 'seq' must be int >= 0, got {seq!r}")

        sent_at = _require_key(data, "sent_at")
        if not isinstance(sent_at, str):
            raise ValueError(f"field 'sent_at' must be str, got {sent_at!r}")

        payload = _require_key(data, "payload")
        if not isinstance(payload, dict):
            raise ValueError(f"field 'payload' must be a dict, got {type(payload).__name__}")

        return cls(type=message_type, seq=seq, sent_at=sent_at, payload=payload)


@dataclass(frozen=True, slots=True)
class LinkStatus:
    connected: bool
    peer: str | None
    last_received_at: str | None
    loss_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "peer": self.peer,
            "last_received_at": self.last_received_at,
            "loss_rate": self.loss_rate,
        }


@runtime_checkable
class CommLink(Protocol):
    def send(self, message: Message) -> None: ...

    def receive(self, timeout_sec: float | None = None) -> Message | None:
        """다음 메시지. 시간 안에 오지 않으면 None."""
        ...

    def link_status(self) -> LinkStatus: ...

    def close(self) -> None: ...


def _require_key(data: dict, key: str) -> object:
    if key not in data:
        raise ValueError(f"missing field '{key}'")
    return data[key]
