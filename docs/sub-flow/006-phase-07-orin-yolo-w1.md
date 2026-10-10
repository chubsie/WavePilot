# 006. Phase 7 Wave 1 — Orin YOLO 서버·프로토콜(07-01)과 배포 파일(07-03)

- 날짜: 2026-10-11
- 브랜치: feat/phase-07-orin-yolo ← feat/phase-02-sim @ 86bed74 (worktree WavePilot-phase07)
- 머지: feat/llm 에 --no-ff 머지 73c631d (Phase 2 머지 d939c4f 이후, phase-07 브랜치에 feat/llm 반영 후 1094 passed 확인)
- 번호: 005 는 Phase 2 sub-flow 기록용으로 비워 둠

## 배경
Phase 2 와 병렬로 진행 가능한 Phase 7(SIM-05) 중 Phase 2 파일과 겹치지 않는 Wave 1 만 먼저 실행.

## 완료 조건
- [x] 07-01: protocol 계약, /health·/v1/detect·/v1/reset, 422/400/500, reset, py3.10, 무거운 import 격리
- [x] 07-03: compose(8082, engine, restart no), 런북 R0~R8 + 보고 양식
- [ ] 07-03 Task 3: Orin 실측 — 사용자 실행 대기 (07-02 머지 후 R6~R7)

## 웨이브·도메인
| 웨이브 | 도메인 | 파일 | 커밋 | 게이트 | 완료 조건 |
|---|---|---|---|---|---|
| 1 | D1 07-01 서버·프로토콜 | 5 | e5ffe02 | ✓ | 충족 |
| 1 | D2 07-03 compose·런북 | 3 | 94f86c7 | ✓ | Task 1·2 충족, Task 3 대기 |

## 검증 결과
- 커밋 대조: 두 커밋이 각자 도메인 파일만 포함, 미커밋 잔여 없음
- 전체 테스트: 1094 passed, 2 skipped (baseline 1031)
- 스모크: 실제 yolo11n.pt(cpu)로 서버 기동 → health 200, 녹화 프레임 두 번 요청 시 track ID 유지, timestamp 1.23456→1.235, broken JPEG 422, reset 200
- verify 커밋: 없음

## 판단 보류 · 범위 밖 발견
- compose 의 `~/wavepilot` 마운트는 compose v2 의 `~` 확장 전제
- reset 후 track ID 번호가 1 부터 다시 시작하는지는 ultralytics 동작에 따름

## 다음 단계
- Phase 2 확정 후 이 브랜치에 Phase 2 최신을 머지하고 07-02(클라이언트·지연 분리·bench·--yolo 전환) 실행
- 사용자: Orin 에서 런북 R0~R5 는 지금도 가능
