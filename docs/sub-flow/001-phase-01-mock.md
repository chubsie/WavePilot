# 001. GSD Phase 01 — 인터페이스 + 끝단 mock 경로 구현

- 날짜: 2026-10-10
- 브랜치: feat/phase-01-mock ← feat/llm @ 8fbd4a4
- 머지: 1ff3803 (feat/llm, --no-ff) — 사용자 결정으로 Orin 실기 검증 전에 머지. 01-08·01-09 checkpoint 는 feat/llm 위에서 진행

## 배경

`.planning/phases/01-mock` 의 10개 PLAN(GSD)을 sub-flow 방식으로 실행했다.
목표는 시나리오 mock → YOLO 프레임 → 투영·track → 상태 JSON → 규칙 판단 → 보고 LLM → CommLink → 운용자 CLI 까지 2프로세스 끝단 경로다.

## 완료 조건

- [x] 8/10 PLAN 의 SUMMARY 존재, 자기 파일만 커밋
- [x] `uv run pytest` 444 passed, `uv run pytest -m e2e` 2 passed (walking skeleton GREEN)
- [x] `scripts/run_demo.sh --backend fake --speed 0` exit 0, 잔여 프로세스 없음
- [ ] 01-08 Task 2 — 사용자가 Orin 에서 RUNBOOK 실행 (human-action)
- [ ] 01-09 Task 3 — 사용자가 Mac 에서 llama-server + Nemotron 실 보고 확인 (human-verify)

## 웨이브·도메인

| 웨이브 | 플랜 | 내용 | 커밋 | 게이트 |
|---|---|---|---|---|
| 1 | 01-01 | uv 스캐폴드, interfaces/, llm_module/backend.py, RED E2E | 3 | 59 passed |
| 2 | 01-02 | mocks/ (시나리오 TOML, 카메라, 프레임 소스, 호버링 FC) | 2 | ✓ |
| 2 | 01-03 | perception/ (평지 투영, TrackAggregator) | 4 | ✓ |
| 2 | 01-04 | llm_module 내부 + 파사드 | 6 | ✓ |
| 2 | 01-05 | comm/ (JSON lines TCP), operator_console/ | 3 | ✓ |
| 2 | 01-07 | LlamaServerBackend (stdlib urllib) | 3 | ✓ |
| 2 | 01-08 | deploy/jetson Dockerfile·compose·RUNBOOK | 1 | ✓ (Task 2 대기) |
| 3 | 01-06 | drone/ 파이프라인 + run_scenario | 5 | 409 passed, e2e ✓ |
| 4 | 01-09 | scripts/, examples/ | 4 | ✓ (Task 3 대기) |
| 4 | 01-10 | README Python 섹션 + 일치성 테스트 | 2 | 437 passed |

서브에이전트 커밋 33 + 모 에이전트 커밋 3 (사용자 지시 반영 2, verify 1).

## 검증 결과

- 웨이브마다 커밋별 파일 집합을 PLAN files_modified 와 대조 — 계획 밖 파일 없음.
- 웨이브마다 전체 테스트 재실행: 59 → 399 → 409 → 437 → 444 passed.
- 2프로세스 실행에서 LINK/STATE/DECISION/REPORT 줄과 track ID 인용 보고 확인.
- 사용자 지시 반영: API key 전면 제거 — `78adb9c` (LlamaServerBackend·LLMModuleConfig·테스트), `6954376` (RUNBOOK). SKELETON·01-06·01-09 계획도 수정.
- 코드 리뷰(01-REVIEW.md): Critical 1 / Warning 8 / Info 10. CR-01(운용자 콘솔 로그 위조 — \v·U+2028·ANSI escape) 재현 후 `fac01a1` 로 수정.

## 판단 보류 · 범위 밖 발견

- 다른 세션이 Wave 3 도중 공유 체크아웃 `WavePilot/` 의 브랜치를 바꿈 → 사용자 결정으로 전용 워크트리 `WavePilot-phase01` 로 분리해 계속했다. 원 체크아웃은 `feat/llm`.
- `feat/phase-01.1-yolo`(WavePilot-yolo) 는 Wave 2 도중 커밋 `4bca49d` 에서 분기 — 이후 커밋이 없다.
- `main` 에 원격 README 변경(`9048622`) — `main` 으로 합칠 때 README 충돌 가능.
- 리뷰 WR-01~08, IN-01~10 미해결 (NaN/inf 입력, send 타임아웃 없음, Ctrl-C 시 최대 60 s 대기 등).
- 평지 투영은 박스 하단 기준이라 차량 north 30 m → 27.7 m 로 보고된다.

## 다음 단계

1. 사용자: 01-09 Mac 실 LLM 검증, 01-08 Orin RUNBOOK 실행 → 결과 회신
2. 리뷰 warning 처리 여부 결정 (`/gsd-code-review 1 --fix`)
3. 검증 통과 후 `feat/llm` 으로 `--no-ff` 머지, 페이즈 완료 처리
