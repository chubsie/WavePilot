"""modules.comm.interface.comm 계약: 메시지 4종 envelope, 입력 검증, 링크 상태."""

import pytest

from modules.comm.interface.comm import MESSAGE_TYPES, CommLink, LinkStatus, Message

SENT_AT = "2026-10-10T12:00:00.000+09:00"


def _make_message(message_type: str = "state") -> Message:
    return Message(type=message_type, seq=7, sent_at=SENT_AT, payload={"time_sec": 1.5})


def test_message_types_are_fixed_to_four() -> None:
    assert MESSAGE_TYPES == ("telemetry", "state", "report", "command")


@pytest.mark.parametrize("message_type", MESSAGE_TYPES)
def test_message_round_trip(message_type: str) -> None:
    message = _make_message(message_type)

    assert Message.from_dict(message.to_dict()) == message


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("type", "decision"),
        ("seq", -1),
        ("seq", "7"),
        ("seq", True),
        ("sent_at", 12345),
        ("payload", ["not", "a", "dict"]),
    ],
)
def test_message_from_dict_rejects_bad_field(field: str, bad_value: object) -> None:
    data = _make_message().to_dict()
    data[field] = bad_value

    with pytest.raises(ValueError, match=field):
        Message.from_dict(data)


def test_message_from_dict_rejects_missing_key() -> None:
    data = _make_message().to_dict()
    del data["payload"]

    with pytest.raises(ValueError, match="payload"):
        Message.from_dict(data)


def test_message_from_dict_rejects_non_dict() -> None:
    with pytest.raises(ValueError):
        Message.from_dict(["state"])


def test_link_status_defaults_loss_rate_to_zero() -> None:
    status = LinkStatus(connected=True, peer="127.0.0.1:5000", last_received_at=SENT_AT)

    assert status.loss_rate == 0.0
    assert status.to_dict() == {
        "connected": True,
        "peer": "127.0.0.1:5000",
        "last_received_at": SENT_AT,
        "loss_rate": 0.0,
    }


def test_class_with_link_methods_satisfies_comm_link() -> None:
    class NullLink:
        def send(self, message: Message) -> None:
            return None

        def receive(self, timeout_sec: float | None = None) -> Message | None:
            return None

        def link_status(self) -> LinkStatus:
            return LinkStatus(connected=False, peer=None, last_received_at=None)

        def close(self) -> None:
            return None

    assert isinstance(NullLink(), CommLink)
