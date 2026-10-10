"""인식 계층 (D-06) — 프레임 단위 탐지를 월드 좌표 track 목록으로 바꾼다.

projection: 픽셀 → 평지 NED 좌표 (PERC-01)
tracking:   YOLO track_id 별 집계 → Track 목록 (PERC-02)

llm_module 은 이 계층이 만든 Track 목록과 Telemetry 만 받고, 원시 프레임은 보지 않는다.
"""
