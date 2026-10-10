"""rule_mode — 규칙 코드로 움직이는 모드(search / approach / hold / return_home) 구현.

구체 모드는 여기서 재노출하지 않는다. 컴포지션 루트가 `modules.rule_mode.hold` 처럼 모듈 경로로 직접 import 한다 —
modules/simulation/__init__.py 와 같은 방식이다. 모드들이 공유하는 계약은 modules.rule_mode.interface 에 있다.
"""
