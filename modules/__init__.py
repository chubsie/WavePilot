"""WavePilot 드론 측 모듈 모음.

각 모듈은 `interface/`(Protocol·메시지 타입)와 그 구현을 함께 둔다.
다른 모듈은 상대 모듈의 `interface/` 만 import 한다 — 구현을 고르고 조립하는 곳은 `drone/` 뿐이다.
"""
