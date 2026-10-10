"""사건 판정기 — 선택기는 사건이 있을 때만 불린다 (SEL-05).

판정 위치: 선택기 래퍼가 아니라 호출 쪽(재생 하네스, Phase 3 실행기)이 들고 있는 이 판정기에서 한다.
선택기(DecisionModel, D-06)는 "언제"를 모르고 "무엇"만 고른다. 그래야 규칙과 d1 을 바꿔 끼워도
호출 빈도가 같다(Phase 6 비교 공정성).

판정 규칙:
| 우선순위 | 사건            | 조건                                                                  |
|----------|-----------------|-----------------------------------------------------------------------|
| 1        | mode_ended      | 이번 틱에 mode_result 가 있다                                          |
| 2        | new_target      | 처음 보는 활성 track 중 클래스가 mission.target_class 와 같은 것이 있다 |
| 3        | periodic_check  | 마지막 사건 뒤 periodic_interval_sec 이 지났다 (첫 poll 포함)           |

- 한 틱에 사건은 최대 1개다. 겹치면 상위 하나로 합친다 — 질문 하나의 상태에 모든 변화가 이미 들어 있다.
- 첫 poll 은 주기 확인으로 친다(아직 고른 모드가 없다).
- 사건이 하나라도 나면 주기 시계를 그 시각으로 다시 맞춘다.
- 활성 track 만 "봤다"로 센다. 비활성으로 처음 보인 track 은 활성이 되는 틱에 새 대상이 된다.
- track 라벨은 임무 내 고유해야 한다. 본 라벨은 잊지 않으므로 트래커가 ID 를 재사용(리셋 포함)하면
  그 라벨로는 new_target 이 다시 나지 않는다. 본 라벨 집합은 임무 동안 계속 커진다(한 임무 = 판정기 하나).
"""

import math

from modules.decision.interface.state import (
    EVENT_MODE_ENDED,
    EVENT_NEW_TARGET,
    EVENT_PERIODIC_CHECK,
    ModeResult,
    SelectionEvent,
    SelectionState,
)

PERIODIC_CHECK_INTERVAL_SEC = 10.0  # CONTRACTS 기본값(ASSUMED), 실측 후 조정


class SelectionTrigger:
    def __init__(self, periodic_interval_sec: float = PERIODIC_CHECK_INTERVAL_SEC) -> None:
        if not math.isfinite(periodic_interval_sec) or periodic_interval_sec <= 0:
            raise ValueError(f"periodic_interval_sec must be a finite number > 0, got {periodic_interval_sec!r}")

        self._periodic_interval_sec = periodic_interval_sec
        self._last_event_sec: float | None = None
        self._last_poll_sec: float | None = None
        self._seen_active_labels: set[str] = set()

    def poll(self, now_sec: float, state: SelectionState, mode_result: ModeResult | None) -> SelectionEvent | None:
        if not math.isfinite(now_sec):
            raise ValueError(f"now_sec must be finite, got {now_sec!r}")
        if self._last_poll_sec is not None and now_sec < self._last_poll_sec:
            raise ValueError(f"now_sec must not go backwards: {now_sec} < {self._last_poll_sec}")
        self._last_poll_sec = now_sec

        new_target_labels = self._find_new_target_labels(state)
        # 사건 여부와 상관없이 기억한다 — 우선순위에서 밀린 new_target 을 다음 틱에 다시 내지 않기 위해서
        self._seen_active_labels.update(track.track_id for track in state.tracks if track.is_active)

        if mode_result is not None:
            event = SelectionEvent(EVENT_MODE_ENDED, mode_result)
        elif new_target_labels:
            event = SelectionEvent(EVENT_NEW_TARGET, None)
        elif self._is_periodic_due(now_sec):
            event = SelectionEvent(EVENT_PERIODIC_CHECK, None)
        else:
            return None

        self._last_event_sec = now_sec
        return event

    def _find_new_target_labels(self, state: SelectionState) -> tuple[str, ...]:
        target_class = state.mission.target_class
        return tuple(
            track.track_id
            for track in state.tracks
            if track.is_active and track.class_name == target_class and track.track_id not in self._seen_active_labels
        )

    def _is_periodic_due(self, now_sec: float) -> bool:
        if self._last_event_sec is None:
            return True
        return now_sec - self._last_event_sec >= self._periodic_interval_sec
