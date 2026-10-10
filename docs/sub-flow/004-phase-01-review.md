# 004. Phase 1 코드 리뷰(01-REVIEW) 지적 사항 수정

- 날짜: 2026-10-11
- 브랜치: fix/phase-01-review ← feat/phase-01-d1-selector @ 2a1fda5
- 머지: 9f170cb (--no-ff, 서브에이전트 커밋 13 + verify 커밋 1)

## 배경

Phase 1(d1-3B rule_mode 선택기) 계획 6개를 실행하고 Orin 체크포인트(01-02, `orin d1 ok`)를 닫은 뒤, `.planning/phases/01-d1-3b-rule-mode-1/01-REVIEW.md` 의 Critical 2 · Warning 6 · Info 9 를 Phase 검증 전에 고쳤다. RUNBOOK 4단계의 "`--version` 이 CUDA 장치를 출력한다"는 실측과 달라 같이 정정했다.

사용자 결정(WR-01): 선택기에 넘기는 상태(state v2) 구조는 바꾸지 않는다. 방금 실패한 (모드, 대상)은 공유 보기 필터에서 빼고 hold 대신 사다리 다음 칸으로 내려간다. 누적 실패 기억은 Phase 4 로 미룬다(`TODO(phase-4)`).

## 완료 조건

- [x] CR-01 — 사다리 2줄(approach)은 approach 대상이 곧 가장 가까운 확정 track 일 때만. 확정 T1 50 m + 색 미상 T2 10 m 에서 approach T2 무한 반복 없음(observe T2)
- [x] CR-02 — d1 `latency.total_ms` 는 select 전체 벽시계(/health probe 포함). orin 은 POST 왕복 − RTT
- [x] WR-01 — 방금 실패한 (모드, 대상) 재선택 없음, hold 점프 대신 사다리 다음 칸
- [x] WR-02 — probe 실패 시 POST 생략, 원인별 사유(시간 초과 timeout / 거부 unavailable / 응답 이상 bad_response), probe timeout ≤ request timeout, timeout 연속 3회면 회로 열림
- [x] WR-03 — 예상 밖 예외는 새 사유 `internal_error` + `logger.exception`
- [x] WR-04 — aim_and_fire 거리 제한과 just-failed 제외가 `filter_options` 에 (두 선택기 공통)
- [x] WR-05 — replay CLI 오류 4종이 traceback 없이 한 줄 + 종료 코드
- [x] WR-06 — `D1_SERVER_URL` strip, 호스트·포트·공백 검증
- [x] IN-01~09 — 라벨 정규식, trigger 문서화, 비상 결과 options, single_option 주석, find_spec 가드 제거, 빈 selector=rule, 스모크 본문 코드 생성(`sample_request`), compose image 태그 = 빌드 인자, Dockerfile LIBRARY_PATH
- [x] RUNBOOK 4단계 GPU 확인 = `--list-devices`, 5단계 로그·6단계 tegrastats 안내
- [x] `uv run pytest -q` — 932 passed, 기준선 실패 1(`test_yolo_isolation`) 외 0
- [x] 실서버 `pytest -m d1` (Orin 192.168.0.157:8081, 사용자 실행) — 9 passed

## 웨이브·도메인

| 웨이브 | 도메인 | 파일 | 커밋 | 게이트 | 완료 조건 |
|---|---|---|---|---|---|
| 1 | D1 rule·options | options, params, rule_selector, interface/state, d1_selector(호출 1줄) + 테스트 | 3 | ✓ | CR-01, WR-01, WR-04, IN-01 |
| 1 | D3 replay·deploy | replay, trigger, sample_request(새), Dockerfile, compose, RUNBOOK + 테스트 | 4 | ✓ | WR-05, IN-02·05·07·08·09, RUNBOOK |
| 2 | D2a d1·client | d1_selector, systemone_client, interface/selector(주석) + 테스트 | 3 | ✓ | CR-02, WR-02(probe), WR-06(포트), IN-04 |
| 2 | D2b fallback·factory | fallback_selector, factory, interface/selector·__init__, decision/__init__ + 테스트 | 2 | ✓ | WR-02(회로), WR-03, WR-06(strip), IN-03, IN-06 |

## 검증 결과

- 커밋 대조: 웨이브마다 `git log --stat` 로 각 커밋이 자기 도메인 파일만 담았는지 확인. 계획 밖 파일 없음(작업 트리의 `.gitignore`·`tools/visualizer/*` 는 이전부터 있던 미추적 변경으로 손대지 않음).
- 게이트 재실행: Wave 1 뒤 881 passed, Wave 2 뒤 932 passed(기준선 실패 1 외 0).
- 스모크: `python -m modules.decision.sample_request` 출력이 v2 state + 보기 6개로 파싱됨. replay CLI rule 모드 정상, `--log-level foo`·`ftp://` URL 은 한 줄 오류 + 종료 코드 2. 닫힌 포트 대상 d1 replay → unavailable 1 + circuit_open 5, 0.1 s.
- 실서버: 첫 라이브 실행에서 `test_live_timeout_falls_back_to_rule` 회귀 발견 — probe timeout 을 request timeout(5 ms)으로 줄이자 /health(실측 ~19 ms)가 먼저 시간 초과돼 `unavailable` 로 잘못 분류. D2a 가 probe 실패를 원인별 사유로 나눠 수정(8e314ea), 재실행 9 passed.
- 코드 읽기: d1_selector 의 미사용 `ChoiceReply` import 제거 → verify 커밋 `fix(01-review): d1_selector 의 미사용 ChoiceReply import 제거 — verify D2a`.
- 사고: D1 이 원인 확인 중 `git stash`/`pop` 을 실행해 병렬 중이던 D3 파일이 잠깐 치워졌다가 복원됨. D3 가 diff 로 손실 없음 확인. Wave 2 부터 작업 트리를 건드리는 git 명령을 금지했다.

## 판단 보류 · 범위 밖 발견

- 누적 실패 기억(aim→fail→observe→aim 긴 순환)은 Phase 4 — `rule_selector._get_ladder_modes` 의 `TODO(phase-4)`.
- `CIRCUIT_TIMEOUT_STREAK = 3` 은 생성자 인자로만 열림, env 연결 없음.
- `filter_options` 시그니처·실패 제외 규칙, 새 사유 `internal_error`, d1 `total_ms` 정의, compose image 태그 규칙은 계약 변경 — ADR 후보.
- interface/state.py 라벨 오류 메시지가 아직 `T<digits>`, `$` 가 끝의 `\n` 을 허용(`fullmatch`·`[0-9]` 권장). llm_report/state.py 의 v1 라벨 검증도 같은 문제.
- 리포에 ruff·mypy 설정 없음 — 게이트가 pytest 뿐. replay.py 기존 ruff 지적 2건(TRY004, ISC004).
- Dockerfile 의 정적 빌드 `[ASSUMED]` 주석은 실측으로 확인됐으니 지워도 됨.
- Mac→Orin /health 지터(한 번 50 ms) — 단일 probe 로 빼면 orin 추정이 ±30 ms 흔들림. 중앙값·EWMA probe 는 후속.

## 다음 단계

Phase 1 검증(/gsd-verify-work 1) → Phase 01.1·01.2(YOLO) 또는 Phase 2.
