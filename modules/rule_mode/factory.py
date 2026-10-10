"""rule_mode 팩토리 — 선택기가 고른 rule_mode 이름과 파라미터를 RuleMode 인스턴스로 바꾼다 (RM-07).

| 이름                       | 결과                                              |
|----------------------------|---------------------------------------------------|
| search / approach / hold / return_home / aim_and_fire / observe | 해당 모드 클래스 `(params, settings)` |
| RULE_MODES 밖 이름          | ValueError                                        |
"""

from modules.decision.interface.menu import RULE_MODES
from modules.rule_mode.aim_and_fire import AimAndFireMode
from modules.rule_mode.approach import ApproachMode
from modules.rule_mode.hold import HoldMode
from modules.rule_mode.interface import ModeParams, RuleMode, RuleModeSettings
from modules.rule_mode.observe import ObserveMode
from modules.rule_mode.return_home import ReturnHomeMode
from modules.rule_mode.search import SearchMode

SUPPORTED_RULE_MODES = ("search", "approach", "hold", "return_home", "aim_and_fire", "observe")

_MODE_CLASS_BY_NAME = {
    "search": SearchMode,
    "approach": ApproachMode,
    "hold": HoldMode,
    "return_home": ReturnHomeMode,
    "aim_and_fire": AimAndFireMode,
    "observe": ObserveMode,
}


def create_rule_mode(rule_mode: str, params: ModeParams, settings: RuleModeSettings) -> RuleMode:
    if rule_mode not in RULE_MODES:
        raise ValueError(f"rule_mode must be one of {RULE_MODES}, got {rule_mode!r}")

    return _MODE_CLASS_BY_NAME[rule_mode](params, settings)
