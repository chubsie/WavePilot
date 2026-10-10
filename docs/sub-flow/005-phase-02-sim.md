# 005. Phase 2 시뮬레이터 양방향 연동 (GSD execute-phase 2)

- 날짜: 2026-10-11
- 브랜치: feat/phase-02-sim ← feat/llm @ b356ff1
- 머지: d939c4f

## 배경
`/gsd-execute-phase 2`. 브라우저 드론 시뮬레이터(drone_camera.html)와 Python 을 양방향으로 잇는다 — 드론 카메라 프레임 → YOLO, Python 명령 → 브라우저 드론, 브라우저 텔레메트리(GPS 포함) → `FlightControl` (SIM-01·02·03).
미커밋 프로토타입(drone_camera.html·serve_camera.py·live_yolo.py)을 기준선 커밋 1c0987b 로 먼저 남기고 시작했다.

## 완료 조건
- [x] 명령 8종(TakeOff·GoTo·Land·ReturnToHome·SetVelocity·SetYaw·SetGimbal·FireHeart)이 to_dict ⇄ command_from_dict 왕복, Telemetry.gps·ned_to_gps
- [x] SimBridge: 최신 프레임 1칸, 명령 1회 배달, 1.0 s 링크 판정
- [x] stdlib HTTP 서버(/sim/frame, /sim/telemetry, 정적 파일) + BrowserFlightControl(끊김·수동 모드 거절)
- [x] 브라우저 프레임 → YOLO → Frame, 결과를 브라우저 오버레이로 되돌림, `run_scenario --sim mock|browser`
- [x] 브라우저: D-03 화면 배치, 1280x720 HFOV 90 프레임, 10 Hz 텔레메트리, AI 모드에서만 명령 실행
- [x] 사용자 브라우저 확인 중 요청 반영: 페어링 홈, 홈 상공 호버 귀환, Return home 버튼 귀환 모드, GPS 지표

## 웨이브·도메인
| 웨이브 | 도메인 | 파일 | 커밋 | 게이트 | 완료 조건 |
|---|---|---|---|---|---|
| 1 | D0 02-01 계약·SimBridge | 7 | 702f8fc, 985dfe1 | pytest 968 ✓ | 7/7 |
| 2 | D1 02-02 HTTP 서버·BrowserFlightControl·sim_demo | 7 | 32af207, 566f925 | 43 ✓ | 전부 |
| 2 | D2 02-03 브라우저 YOLO 소스·--sim 전환 | 5 | a313d9f, f5693f6 | 1031 ✓, yolo 15 ✓ | 전부 |
| 2 | D3 02-04 sim_link.js·drone_camera.html | 2 | 09ee2c0, 3416415 | node --check·grep ✓ | Task 1·2 (Task 3 사람 확인은 모 에이전트) |

## 검증 결과
- 커밋 대조: 웨이브마다 각 커밋이 자기 도메인 파일만 담음, 미커밋 잔여 없음, pipeline.py 무변경
- 게이트 재실행: 전체 pytest 1031 passed (기준선의 test_yolo_isolation 실패는 live_yolo.py 삭제로 해소)
- 통합 스모크: 실서버 port 0 — 연결 전/수동 모드 명령 거절, AI 모드 큐잉→1회 배달→재배달 없음, GPS 전달, /sim/frame 204, 정적 파일 200
- 사용자 확인 중 모 에이전트 수정(verify 커밋):
  - c380181 첫 페어링 시점 위치·GPS 를 홈으로(NED 원점·return_to_home 기준)
  - dfb9a80 sim_demo 에 return_to_home 단계
  - 86bed74 return_to_home = 홈 상공 호버(착륙 안 함, 최소 10 m) — Node 시뮬레이션으로 수렴·goal_reached 1회 확인
  - 2b8de6c Return home 버튼 = 도착까지 유지되는 귀환 모드, 패널 GPS·홈·거리/방위/고도차

## 판단 보류 · 범위 밖 발견
- SimBridge 에 공개 `closed` 속성이 없어 BrowserDetectionSource 가 이른 None 반환으로 닫힘을 추정한다
- `.gitignore` 의 recordings/ 항목은 녹화 제거 후에도 로컬 폴더가 있어 유지
- WavePilot-phase07 worktree(feat/phase-07-orin-yolo)는 86bed74 에서 분기 — 2b8de6c 이후 변경을 받으려면 feat/llm 머지 필요
- 페어링 홈·호버 귀환 결정은 ADR 후보

## 다음 단계
- `/gsd-code-review 2` (설정상 code_review on)
- Phase 7 07-02 (Phase 2 머지 후 실행 조건 충족) 또는 Phase 3 discuss
