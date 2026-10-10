"""rule_mode 팩토리 — 선택기가 고른 rule_mode 이름과 파라미터를 RuleMode 인스턴스로 바꾼다 (RM-07).

| 이름                       | 결과                                              |
|----------------------------|---------------------------------------------------|
| search / approach / hold / return_home | 해당 모드 클래스 `(params, settings)`  |
| aim_and_fire / observe     | UnsupportedRuleModeError (Phase 4 에서 구현)       |
| RULE_MODES 밖 이름          | ValueError                                        |

호출 쪽(임무 루프)이 UnsupportedRuleModeError 를 받으면 비행 없이 자리표시 결과로 처리한다.
"""

from modules.decision.interface.menu import RULE_MODES
from modules.rule_mode.approach import ApproachMode
from modules.rule_mode.hold import HoldMode
from modules.rule_mode.interface import ModeParams, RuleMode, RuleModeSettings
from modules.rule_mode.return_home import ReturnHomeMode
from modules.rule_mode.search import SearchMode

SUPPORTED_RULE_MODES = ("search", "approach", "hold", "return_home")
PHASE4_RULE_MODES = ("aim_and_fire", "observe")

_MODE_CLASS_BY_NAME = {
    "search": SearchMode,
    "approach": ApproachMode,
    "hold": HoldMode,
    "return_home": ReturnHomeMode,
}


class UnsupportedRuleModeError(ValueError):
    def __init__(self, rule_mode: str) -> None:
        super().__init__(f"rule_mode {rule_mode!r} is not implemented until Phase 4")
        self.rule_mode = rule_mode


def create_rule_mode(rule_mode: str, params: ModeParams, settings: RuleModeSettings) -> RuleMode:
    if rule_mode not in RULE_MODES:
        raise ValueError(f"rule_mode must be one of {RULE_MODES}, got {rule_mode!r}")
    if rule_mode in PHASE4_RULE_MODES:
        raise UnsupportedRuleModeError(rule_mode)

    return _MODE_CLASS_BY_NAME[rule_mode](params, settings)
