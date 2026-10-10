"""운용자 콘솔 진입점(컴포지션 루트) — 구체 전송 TcpJsonLinesLink 는 여기서만 import 한다.

실행: python -m operator_console --host 127.0.0.1 --port 8765 [--exit-on-disconnect]
stdin 명령: rth | land | takeoff <alt_m> | goto <north_m> <east_m> <alt_m> | quit
"""

import argparse
import logging
import sys
import threading

from comm.tcp_link import TcpJsonLinesLink
from operator_console.console import OperatorConsole

DEFAULT_OPERATOR_PORT = 8765


class _ConsoleWarningHandler(logging.Handler):
    """WARNING 이상 로그를 WARN 태그 줄로 — 태그 없는 줄이 stdout 에 섞이지 않게(E2E 가 모든 줄 형식을 검사)."""

    def __init__(self, console: OperatorConsole) -> None:
        super().__init__(level=logging.WARNING)
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        self._console.write("WARN", record.getMessage())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m operator_console", description="WavePilot operator console")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_OPERATOR_PORT, help="port to listen on; 0 picks a free port")
    parser.add_argument("--exit-on-disconnect", action="store_true", help="exit with code 0 after the drone disconnects")
    return parser.parse_args()


def _read_stdin_commands(console: OperatorConsole, stop_event: threading.Event) -> None:
    # stdin EOF 는 이 스레드만 끝낸다 — 백그라운드 실행처럼 stdin 이 닫혀 있어도 콘솔은 계속 돈다
    while line := sys.stdin.readline():
        if not console.handle_command_line(line):
            stop_event.set()
            return


def main() -> None:
    args = _parse_args()
    link = TcpJsonLinesLink.listen(args.host, args.port)

    console = OperatorConsole(link)
    stop_event = threading.Event()

    logging.getLogger().addHandler(_ConsoleWarningHandler(console))

    console.write("LISTEN", f"host={args.host} port={link.bound_port}")

    threading.Thread(target=_read_stdin_commands, args=(console, stop_event), name="operator-stdin", daemon=True).start()

    try:
        exit_code = console.run(stop_event, exit_on_disconnect=args.exit_on_disconnect)
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        link.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
