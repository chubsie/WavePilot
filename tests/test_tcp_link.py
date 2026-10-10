"""comm.tcp_link.TcpJsonLinesLink — 루프백 임의 포트 통합 테스트 (COMM-01, IF-03).

모든 소켓은 127.0.0.1 port 0 이고, 모든 대기에 시간 제한을 둔다 — pytest-timeout 이 없으므로
테스트가 영원히 멈추면 안 된다.
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from comm.codec import MAX_LINE_BYTES, encode_message
from comm.tcp_link import TcpJsonLinesLink
from interfaces.comm import CommLink, LinkStatus, Message

RECEIVE_TIMEOUT_SEC = 2.0
SENT_AT = "2026-10-10T12:00:00.000+00:00"


def _wait_until(predicate: Callable[[], bool], timeout_sec: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _make_message(message_type: str = "state", seq: int = 0, payload: dict | None = None) -> Message:
    return Message(type=message_type, seq=seq, sent_at=SENT_AT, payload=payload or {"n": seq})


def _encode_line(message: Message) -> bytes:
    return (encode_message(message) + "\n").encode("utf-8")


def _reserve_free_port() -> int:
    # 비어 있는 포트를 얻고 바로 닫는다 — 백오프 테스트에서 "아직 아무도 없는 포트"로 쓴다
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def opened_links() -> Iterator[list[TcpJsonLinesLink]]:
    # 테스트가 만든 링크를 실패 여부와 무관하게 모두 닫는다
    links: list[TcpJsonLinesLink] = []
    yield links
    for link in links:
        link.close()


@pytest.fixture
def server(opened_links: list[TcpJsonLinesLink]) -> TcpJsonLinesLink:
    link = TcpJsonLinesLink.listen("127.0.0.1", 0)
    opened_links.append(link)
    return link


@pytest.fixture
def raw_client(server: TcpJsonLinesLink) -> Iterator[socket.socket]:
    sock = socket.create_connection(("127.0.0.1", server.bound_port), timeout=2.0)
    yield sock
    sock.close()


def _connect(server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]) -> TcpJsonLinesLink:
    client = TcpJsonLinesLink.connect("127.0.0.1", server.bound_port, retry_timeout_sec=2.0)
    opened_links.append(client)
    return client


def test_listen_binds_free_port_and_starts_disconnected(server: TcpJsonLinesLink) -> None:
    assert server.bound_port > 0
    assert server.link_status() == LinkStatus(
        connected=False, peer=None, last_received_at=None, loss_rate=0.0
    )


def test_link_satisfies_comm_link_protocol(server: TcpJsonLinesLink) -> None:
    for method_name in ("send", "receive", "link_status", "close"):
        assert callable(getattr(server, method_name))
    assert isinstance(server, CommLink)


def test_messages_flow_both_ways(
    server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]
) -> None:
    client = _connect(server, opened_links)
    state_message = _make_message("state", seq=1)
    command_message = _make_message("command", seq=0, payload={"kind": "return_to_home"})

    assert _wait_until(lambda: server.link_status().connected)
    client.send(state_message)
    server.send(command_message)

    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC) == state_message
    assert client.receive(timeout_sec=RECEIVE_TIMEOUT_SEC) == command_message

    server_status = server.link_status()
    assert server_status.connected and client.link_status().connected
    host, port = server_status.peer.rsplit(":", 1)
    assert host == "127.0.0.1" and port.isdigit()
    assert server_status.last_received_at is not None and "T" in server_status.last_received_at


def test_partial_and_batched_lines_arrive_in_order(
    server: TcpJsonLinesLink, raw_client: socket.socket
) -> None:
    messages = [_make_message(seq=seq) for seq in range(3)]
    first_line = _encode_line(messages[0])
    chunk_size = len(first_line) // 3 + 1

    # 한 메시지를 3 조각으로 나눠 사이에 쉬며 보낸다
    for start in range(0, len(first_line), chunk_size):
        raw_client.sendall(first_line[start:start + chunk_size])
        time.sleep(0.05)
    raw_client.sendall(_encode_line(messages[1]) + _encode_line(messages[2]))

    received = [server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC) for _ in range(3)]
    assert received == messages


def test_malformed_line_is_dropped_and_link_stays_up(
    server: TcpJsonLinesLink, raw_client: socket.socket
) -> None:
    raw_client.sendall(_encode_line(_make_message(seq=0)) + b"not json\n" + _encode_line(_make_message(seq=1)))

    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC).seq == 0
    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC).seq == 1
    assert server.link_status().connected
    assert server.dropped_line_count == 1


def test_oversized_line_is_dropped_and_link_stays_up(
    server: TcpJsonLinesLink, raw_client: socket.socket
) -> None:
    raw_client.sendall(b"x" * (MAX_LINE_BYTES + 10) + b"\n" + _encode_line(_make_message(seq=5)))

    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC).seq == 5
    assert server.link_status().connected
    assert server.dropped_line_count == 1


def test_blank_lines_are_ignored_silently(
    server: TcpJsonLinesLink, raw_client: socket.socket
) -> None:
    raw_client.sendall(b"\n  \n" + _encode_line(_make_message(seq=2)))

    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC).seq == 2
    assert server.dropped_line_count == 0


def test_peer_close_is_detected_without_losing_messages(
    server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]
) -> None:
    client = _connect(server, opened_links)
    assert _wait_until(lambda: server.link_status().connected)
    peer_while_connected = server.link_status().peer

    for seq in range(5):
        client.send(_make_message(seq=seq))
    client.close()

    # connected False 를 본 시점에 이미 받은 메시지는 전부 inbox 에 있어야 한다
    assert _wait_until(lambda: not server.link_status().connected)
    received = [server.receive(timeout_sec=0) for _ in range(5)]
    assert [message.seq for message in received] == [0, 1, 2, 3, 4]
    assert server.link_status().peer == peer_while_connected


def test_server_accepts_reconnect_after_disconnect(
    server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]
) -> None:
    first_client = _connect(server, opened_links)
    assert _wait_until(lambda: server.link_status().connected)
    first_client.close()
    assert _wait_until(lambda: not server.link_status().connected)

    second_client = _connect(server, opened_links)
    second_client.send(_make_message(seq=9))

    assert server.receive(timeout_sec=RECEIVE_TIMEOUT_SEC).seq == 9
    assert server.link_status().connected


def test_connect_backs_off_until_server_appears(opened_links: list[TcpJsonLinesLink]) -> None:
    port = _reserve_free_port()
    result: dict[str, object] = {}

    def connect_later() -> None:
        try:
            result["link"] = TcpJsonLinesLink.connect("127.0.0.1", port, retry_timeout_sec=5.0)
        except Exception as error:  # 스레드 밖에서 확인하려고 모은다
            result["error"] = error

    connector = threading.Thread(target=connect_later, daemon=True)
    connector.start()
    time.sleep(0.3)
    opened_links.append(TcpJsonLinesLink.listen("127.0.0.1", port))
    connector.join(timeout=5.0)

    assert "error" not in result, result.get("error")
    client = result["link"]
    opened_links.append(client)
    assert client.link_status().connected


def test_connect_gives_up_after_retry_timeout() -> None:
    port = _reserve_free_port()
    started_at = time.monotonic()

    with pytest.raises(ConnectionError):
        TcpJsonLinesLink.connect("127.0.0.1", port, retry_timeout_sec=0.5)

    assert time.monotonic() - started_at < 3.0


def test_send_without_peer_raises_connection_error(server: TcpJsonLinesLink) -> None:
    with pytest.raises(ConnectionError):
        server.send(_make_message())


def test_bound_port_is_only_for_listen_side(
    server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]
) -> None:
    client = _connect(server, opened_links)

    with pytest.raises(RuntimeError):
        _ = client.bound_port


def test_close_is_idempotent_and_stops_threads(
    server: TcpJsonLinesLink, opened_links: list[TcpJsonLinesLink]
) -> None:
    client = _connect(server, opened_links)
    assert _wait_until(lambda: server.link_status().connected)

    client.close()
    client.close()
    server.close()
    server.close()

    assert server.receive(timeout_sec=0) is None
    assert client.receive(timeout_sec=0) is None
    for thread in server._threads + client._threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()
