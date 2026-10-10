"""mock 월드 — 시나리오 파일로 탐지 프레임과 호버링 비행 제어를 흉내 낸다.

구체 클래스는 여기서 재노출하지 않는다. 컴포지션 루트(drone/run_scenario.py)가
`mocks.scenario`, `mocks.detection_source` 처럼 모듈 경로로 직접 import 한다 —
그래야 파이프라인 쪽 코드가 mock 의 존재를 모른 채 interfaces 만 바라본다(D-04).
"""
