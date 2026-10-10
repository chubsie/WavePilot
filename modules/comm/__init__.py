"""CommLink 의 시뮬레이션 구현(TCP JSON lines).

파이프라인 코드는 modules.comm.interface.comm.CommLink 만 보고, 이 패키지는 진입점
(modules/drone/run_scenario.py, modules/operator_console/__main__.py)에서만 import 한다 — SC4.
"""

from modules.comm.codec import MAX_LINE_BYTES, decode_message, encode_message
from modules.comm.tcp_link import TcpJsonLinesLink

__all__ = ["MAX_LINE_BYTES", "TcpJsonLinesLink", "decode_message", "encode_message"]
