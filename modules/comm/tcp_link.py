"""TCP 단일 연결 양방향 CommLink — UTF-8 JSON lines (COMM-01, IF-03).

운용자가 listen(서버), 드론이 connect(클라이언트) 한다:
- 운용 순서상 운용자 콘솔을 먼저 띄우고 드론을 붙인다.
- 드론 측 connect 는 지수 백오프로 재시도한다 — Phase 2 두절 복구 때 드론 측 재연결이 들어갈 자리다.
- 서버는 한 번에 한 peer 만 서비스하고, 끊기면 다음 연결을 다시 받는다.

한 TCP 연결로 양방향(드론→운용자 telemetry/state/report, 운용자→드론 command)을 모두 싣는다.
Phase 1 은 인증·암호화가 없다 — 로컬 루프백 시뮬레이션 전제이며 링크 인증은 v2 범위다.
LinkStatus.loss_rate 는 Phase 2(COMM-02)에서 실측값을 채우고, 지금은 항상 0.0 이다.
"""

import logging
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from typing import BinaryIO

from modules.comm.codec import MAX_LINE_BYTES, decode_message, encode_message
from modules.comm.interface.comm import LinkStatus, Message

# 첫 재시도 간격. 루프백에서 서버가 막 뜨는 중이면 0.1 s 안에 붙는다
CONNECT_BACKOFF_INITIAL_SEC = 0.1
# 재시도 간격 상한(배수 2로 늘린다) — 늦게 뜬 서버를 2 s 넘게 기다리게 하지 않는다
CONNECT_BACKOFF_MAX_SEC = 2.0
# 서버 소켓 accept 시간 제한 = stop 이벤트 확인 주기
ACCEPT_POLL_SEC = 0.5
# close() 가 백그라운드 스레드를 기다리는 최대 시간 — 프로세스 종료가 막히지 않게
THREAD_JOIN_TIMEOUT_SEC = 1.0
# 동시에 한 peer 만 서비스하므로 대기열도 1
LISTEN_BACKLOG = 1
# 거대한 오류 문자열이 운용자 화면을 덮지 않게 자른다
DROP_REASON_MAX_CHARS = 200

logger = logging.getLogger(__name__)


class TcpJsonLinesLink:
    """CommLink 구현. 공개 생성 경로는 listen() / connect() 두 classmethod 뿐이다."""

    def __init__(self) -> None:
        self._inbox: queue.Queue[Message] = queue.Queue()
        self._send_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._connection: socket.socket | None = None
        self._peer: str | None = None
        self._is_connected = False
        self._last_received_at: str | None = None
        self._dropped_line_count = 0
        self._server_socket: socket.socket | None = None
        self._bound_port: int | None = None
        self._threads: list[threading.Thread] = []

    # --- 생성 ---

    @classmethod
    def listen(cls, host: str, port: int) -> "TcpJsonLinesLink":
        """운용자 측 서버. port 0 이면 임의 포트 — 실제 포트는 bound_port."""
        link = cls()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(LISTEN_BACKLOG)
        server_socket.settimeout(ACCEPT_POLL_SEC)

        link._server_socket = server_socket
        link._bound_port = server_socket.getsockname()[1]
        link._start_thread(link._accept_loop, "tcp-link-accept")
        return link

    @classmethod
    def connect(cls, host: str, port: int, retry_timeout_sec: float = 10.0) -> "TcpJsonLinesLink":
        """드론 측 클라이언트. 지수 백오프로 재시도하고 기한을 넘기면 ConnectionError."""
        deadline = time.monotonic() + retry_timeout_sec
        delay_sec = CONNECT_BACKOFF_INITIAL_SEC
        last_error: OSError | None = None

        while True:
            remaining_sec = deadline - time.monotonic()
            if remaining_sec <= 0:
                raise ConnectionError(
                    f"could not connect to {host}:{port} within {retry_timeout_sec} s"
                ) from last_error
            try:
                sock = socket.create_connection((host, port), timeout=min(delay_sec, remaining_sec))
                break
            except OSError as error:
                last_error = error
                time.sleep(max(0.0, min(delay_sec, deadline - time.monotonic())))
                delay_sec = min(delay_sec * 2, CONNECT_BACKOFF_MAX_SEC)

        # 클라이언트 측 자동 재연결은 하지 않는다 — Phase 2 COMM-02 범위
        link = cls()
        sock.settimeout(None)
        peer_host, peer_port = sock.getpeername()[:2]
        link._handle_peer_connected(sock, f"{peer_host}:{peer_port}")
        link._start_thread(link._serve_connection_once, "tcp-link-reader", args=(sock,))
        return link

    @property
    def bound_port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("bound_port is only available on a link created by listen()")
        return self._bound_port

    # --- CommLink ---

    def send(self, message: Message) -> None:
        # ValueError(NaN·상한 초과)는 인코딩 단계에서 그대로 전파한다
        line = (encode_message(message) + "\n").encode("utf-8")

        with self._send_lock:
            with self._status_lock:
                connection = self._connection if self._is_connected else None
            if connection is None:
                raise ConnectionError("link not connected")
            try:
                connection.sendall(line)
            except OSError as error:
                # 두절 처리는 읽기 스레드가 EOF 로 한다 — 여기서는 호출자에게 알리기만
                raise ConnectionError(f"send failed: {error}") from error

    def receive(self, timeout_sec: float | None = None) -> Message | None:
        try:
            if timeout_sec == 0:
                return self._inbox.get_nowait()
            return self._inbox.get(timeout=timeout_sec)
        except queue.Empty:
            return None

    def link_status(self) -> LinkStatus:
        with self._status_lock:
            return LinkStatus(
                connected=self._is_connected,
                peer=self._peer,
                last_received_at=self._last_received_at,
                loss_rate=0.0,
            )

    @property
    def dropped_line_count(self) -> int:
        with self._status_lock:
            return self._dropped_line_count

    def close(self) -> None:
        """멱등. 연결·서버 소켓을 닫고 백그라운드 스레드를 기다린다."""
        self._stop_event.set()

        with self._status_lock:
            connection = self._connection
            server_socket = self._server_socket
        if connection is not None:
            _shutdown_socket(connection)
        if server_socket is not None:
            server_socket.close()

        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=THREAD_JOIN_TIMEOUT_SEC)

    # --- 백그라운드 스레드 ---

    def _start_thread(self, target, name: str, args: tuple = ()) -> None:
        thread = threading.Thread(target=target, name=name, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self) -> None:
        # 한 스레드가 accept → 읽기 → 두절 처리 → 다시 accept 를 돈다 (동시에 peer 하나)
        while not self._stop_event.is_set():
            try:
                sock, address = self._server_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # close() 가 서버 소켓을 닫았다

            sock.settimeout(None)
            self._handle_peer_connected(sock, f"{address[0]}:{address[1]}")
            self._serve_connection_once(sock)

    def _serve_connection_once(self, sock: socket.socket) -> None:
        self._read_loop(sock)
        self._handle_peer_disconnected(sock)

    def _read_loop(self, sock: socket.socket) -> None:
        # 텍스트 모드 대신 바이너리로 읽는다 — 상한을 문자 수가 아닌 바이트 수로 정확히 지키기 위해
        peer = self.link_status().peer
        try:
            with sock.makefile("rb") as reader:
                while not self._stop_event.is_set():
                    raw_line = reader.readline(MAX_LINE_BYTES + 1)
                    if raw_line == b"":
                        return  # EOF

                    is_oversized = len(raw_line) == MAX_LINE_BYTES + 1 and not raw_line.endswith(b"\n")
                    if is_oversized:
                        _discard_rest_of_line(reader)
                        self._handle_dropped_line(peer, "line exceeds MAX_LINE_BYTES")
                        continue
                    if not raw_line.strip():
                        continue

                    self._handle_line(peer, raw_line)
        except OSError:
            return  # close() 중 shutdown 등 — 루프 종료로 처리

    # --- 이벤트 처리 ---

    def _handle_line(self, peer: str | None, raw_line: bytes) -> None:
        try:
            message = decode_message(raw_line)
        except ValueError as error:
            self._handle_dropped_line(peer, str(error)[:DROP_REASON_MAX_CHARS])
            return

        with self._status_lock:
            self._last_received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._inbox.put(message)

    def _handle_peer_connected(self, sock: socket.socket, peer: str) -> None:
        with self._status_lock:
            self._connection = sock
            self._peer = peer
            self._is_connected = True

        logger.info("link connected peer=%s", peer)

    def _handle_peer_disconnected(self, sock: socket.socket) -> None:
        # _read_loop 가 마지막 줄을 inbox 에 넣은 뒤에만 호출된다 — 그래서 "connected False 를 본 뒤
        # inbox 를 비우면 유실이 없다"는 순서가 보장된다(운용자 콘솔이 의존). peer 는 마지막 값을 유지한다
        with self._status_lock:
            self._is_connected = False
            self._connection = None
            peer = self._peer

        _shutdown_socket(sock)
        logger.info("link disconnected peer=%s", peer)

    def _handle_dropped_line(self, peer: str | None, reason: str) -> None:
        with self._status_lock:
            self._dropped_line_count += 1

        logger.warning("dropped line from %s: %s", peer, reason)


def _discard_rest_of_line(reader: BinaryIO) -> None:
    # 상한을 넘긴 줄은 끝("\n")이나 EOF 까지 같은 크기로 읽어 버린다 — 메모리에 쌓지 않는다
    while True:
        chunk = reader.readline(MAX_LINE_BYTES + 1)
        if chunk == b"" or chunk.endswith(b"\n"):
            return


def _shutdown_socket(sock: socket.socket) -> None:
    # shutdown 은 다른 스레드의 블로킹 readline 을 깨운다. 이미 닫혔으면 OSError 무시
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
