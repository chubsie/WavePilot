"""CommLink 의 시뮬레이션 구현(TCP JSON lines).

파이프라인 코드는 interfaces.comm.CommLink 만 보고, 이 패키지는 진입점
(drone/run_scenario.py, operator_console/__main__.py)에서만 import 한다 — SC4.
"""

from comm.codec import MAX_LINE_BYTES, decode_message, encode_message

__all__ = ["MAX_LINE_BYTES", "decode_message", "encode_message"]
