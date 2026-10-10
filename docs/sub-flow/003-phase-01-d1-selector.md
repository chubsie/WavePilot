# 003. GSD Phase 1 — d1-3B rule_mode 선택기 실행 (계획 6개, 3웨이브)

- 날짜: 2026-10-11
- 브랜치: feat/phase-01-d1-selector ← feat/llm @ 18f6f0b
- 머지: 미머지 — 01-02 의 Orin 실측 체크포인트(사용자 실행)가 남아 있고, 코드 리뷰 BLOCKER 2건이 아직 열려 있다

## 배경
`/gsd-execute-phase 1`. 계획은 `.planning/phases/01-d1-3b-rule-mode-1/01-0{1..6}-PLAN.md` 이다. d1-3B 가 rule_mode 를 고르고, 실패하면 규칙 선택기로 넘어가는 선택 계층을 `modules/decision/` 에 새로 만든다.

## 완료 조건
- [x] 01-01 decision 계약 — 메뉴 6모드, state v2, SelectionResult, DecisionModel Protocol
- [x] 01-03 보기 필터, 공용 fill_params, 결정적 RuleSelector
- [x] 01-04 /v1/systemone 클라이언트, 질문 문구, d1 마커(기본 게이트에서 제외)
- [x] 01-05 D1Selector, FallbackSelector(회로 차단), WAVEPILOT_SELECTOR 팩토리
- [x] 01-06 SelectionTrigger, 재생 하네스(SELECT 줄, LATENCY 표, CSV)
- [ ] 01-02 Orin 배포 — 파일과 정적 테스트는 완료했다. Task 3(사용자가 RUNBOOK 을 실행하고 실측을 보고)이 남아 있다
- [ ] SEL-01 실서버 확인 — `-m d1` 라이브 테스트 9개는 D1_SERVER_URL 이 없어 skip 됐다

## 웨이브·도메인
| 웨이브 | 도메인 | 파일 | 커밋 | 게이트 | 완료 조건 |
|---|---|---|---|---|---|
| 1 | 01-01 interface | 7 | 2 | ✓ | 7/7 |
| 1 | 01-02 Orin deploy (wave 3 에서 앞당김, depends_on 없음) | 4 | 4 | ✓ | 체크포인트 대기 |
| 2 | 01-03 rule selector | 6 | 7 | ✓ | 7/7 |
| 2 | 01-04 systemone client | 6 | 5 | ✓ | 6/6 (실서버 제외) |
| 3 | 01-05 d1/fallback/factory | 6 | 5 | ✓ | 7/7 (실서버 제외) |
| 3 | 01-06 trigger/replay | 6 | 6 | ✓ | 6/6 |

## 검증 결과
- 커밋 대조: 웨이브마다 `git show --name-only` 로 커밋 29개를 확인했다. 모두 자기 계획의 files_modified 만 담고 있고, Co-Authored-By 트레일러는 0개다. 사용자의 미커밋 파일(.gitignore, tools/visualizer/*)은 건드리지 않았다.
- 게이트 재실행:
  - `uv run pytest -q` 결과는 836 passed, 1 failed 다. 실패 1건 `test_yolo_isolation` 은 기준선부터 있던 것으로, 미추적 파일 tools/visualizer/live_yolo.py 때문이다.
  - `-m d1` 결과는 9 skipped 다.
- 스모크: `python -m modules.decision.replay modules/decision/fixtures/target_found_mode_chain.jsonl --selector rule` 를 돌렸다. 30틱 동안 질문 6번(사건 수와 같음), LATENCY/FALLBACK 표 출력, exit 0 이다.
- 코드 리뷰(gsd-code-reviewer, standard): `.planning/phases/01-d1-3b-rule-mode-1/01-REVIEW.md` 에 BLOCKER 2, WARNING 6, INFO 9 가 나왔다. 모 에이전트가 BLOCKER 2건을 코드로 확인했다.
  - CR-01: ladder 의 2행은 색이 확정된 track 이 있으면 approach 를 고른다. 그런데 approach 파라미터는 가장 가까운 *후보*(색 미확인 포함)를 대상으로 잡는다(params.py `_build_approach_params`). 그래서 확정 track 이 하나라도 보이는 동안에는 observe 가 선택되지 않는다.
  - CR-02: d1 의 total_ms 에 /health 프로브 시간이 빠져 있다(d1_selector.py). 계획 문구("POST round trip")대로 구현됐지만, 규칙 선택기와 지연을 비교하면 d1 쪽이 유리하게 나온다.
- verify 커밋: 없음

## 판단 보류 · 범위 밖 발견
- CR-01, CR-02 를 고칠지와 어떤 의미로 고칠지(approach 대상 규칙, total_ms 정의)는 사용자가 정한다.
- 01-02-SUMMARY 는 체크포인트 대기 상태라 `01-02-CHECKPOINT-PENDING.md` 로 이름을 바꿔 두었다. GSD 가 이 계획을 완료로 오인하지 않게 하려는 것이다.
- 범위 밖: test_yolo_isolation 기준선 실패(사용자의 visualizer 작업)

## 다음 단계
1. 사용자가 Orin 에서 RUNBOOK 0~10 단계를 실행하고 step-11 보고를 붙여 넣는다. 그다음 01-02 를 마무리하고 `D1_SERVER_URL=... uv run pytest -m d1` 를 돌린다.
2. `/gsd-code-review 1 --fix` 로 리뷰 지적 사항을 고친다. 단, 그 전에 CR-01·CR-02 의 의미를 결정해야 한다.
3. 두 가지가 끝나면 phase 검증을 하고 feat/llm 에 --no-ff 로 머지한다.
