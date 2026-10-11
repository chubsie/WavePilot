"""선택기 팩토리 — 설정 값 하나(WAVEPILOT_SELECTOR)로 선택기를 고른다 (SEL-04, D-06).

호출 쪽(01-06 재생 하네스, Phase 3 실행기, Phase 6 비교)은 DecisionModel 만 알고,
뒤에 RuleSelector 가 있는지 FallbackSelector(D1Selector, RuleSelector) 가 있는지 모른다.
세 선택기 모두 같은 ParamDefaults 를 받아 파라미터 채움이 같다(D-04).

환경 변수:
| 이름                              | 기본값 | 뜻                                              |
|-----------------------------------|--------|-------------------------------------------------|
| WAVEPILOT_SELECTOR                | rule   | rule 또는 d1 (대소문자·앞뒤 공백 무시, 비우면 기본값) |
| D1_SERVER_URL                     | 없음   | d1-3B llama-server 주소. d1 이면 필수 (앞뒤 공백 무시, 01-04 실서버 테스트와 같은 이름) |
| WAVEPILOT_D1_TIMEOUT_MS           | 1000   | /v1/systemone 요청 timeout (ms, > 0)            |
| WAVEPILOT_D1_CONFIDENCE_THRESHOLD | 0.3    | 이 값 미만이면 low_confidence 로 규칙 대체 ([0, 1]) |
| WAVEPILOT_D1_CIRCUIT_OPEN_SEC     | 5.0    | unavailable·연속 timeout 뒤 d1 을 건너뛰는 시간 (s, >= 0, 0 이면 끔) |

숫자 변수와 WAVEPILOT_SELECTOR 는 비우거나 공백만 두면 기본값을 쓴다.

설정 오류는 시작 시점에 바로 ValueError 로 실패한다 — 규칙 대체 대상이 아니다(T-01-05-04).
warm-up 은 여기서 하지 않는다(01-06 재생 하네스 --warmup 몫).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from modules.decision.d1_selector import D1Selector
from modules.decision.fallback_selector import FallbackSelector
from modules.decision.interface import DecisionModel
from modules.decision.params import ParamDefaults
from modules.decision.rule_selector import RuleSelector
from modules.decision.systemone_client import SystemOneClient

SELECTOR_ENV = "WAVEPILOT_SELECTOR"
D1_URL_ENV = "D1_SERVER_URL"
D1_TIMEOUT_ENV = "WAVEPILOT_D1_TIMEOUT_MS"
CONFIDENCE_THRESHOLD_ENV = "WAVEPILOT_D1_CONFIDENCE_THRESHOLD"
CIRCUIT_OPEN_ENV = "WAVEPILOT_D1_CIRCUIT_OPEN_SEC"

SELECTOR_RULE = "rule"
SELECTOR_D1 = "d1"
SELECTOR_KINDS = (SELECTOR_RULE, SELECTOR_D1)

D1_REQUEST_TIMEOUT_MS = 1000.0  # ASSUMED — 실측 p95·기록 상태 세트로 재조정(RESEARCH §4)
CONFIDENCE_THRESHOLD = 0.3  # ASSUMED — 실측 p95·기록 상태 세트로 재조정(RESEARCH §4)
CIRCUIT_OPEN_SEC = 5.0  # ASSUMED — 실측 p95·기록 상태 세트로 재조정(RESEARCH §4)

MS_PER_SEC = 1000.0


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    kind: str
    d1_url: str | None
    request_timeout_ms: float
    confidence_threshold: float
    circuit_open_sec: float
    param_defaults: ParamDefaults
    allow_return_home: bool = True  # 거짓이면 선택기 보기에서 return_home 을 뺀다(임무 파일 [selector]). 환경 변수 없음

    def __post_init__(self) -> None:
        if self.kind not in SELECTOR_KINDS:
            raise ValueError(f"{SELECTOR_ENV} must be one of {'|'.join(SELECTOR_KINDS)}, got {self.kind!r}")
        if self.kind == SELECTOR_D1 and not (isinstance(self.d1_url, str) and self.d1_url.strip()):
            raise ValueError(f"{D1_URL_ENV} is required when {SELECTOR_ENV}={SELECTOR_D1}")

        if not _is_finite(self.request_timeout_ms) or self.request_timeout_ms <= 0:
            raise ValueError(f"{D1_TIMEOUT_ENV} must be > 0, got {self.request_timeout_ms!r}")
        if not _is_finite(self.confidence_threshold) or not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError(f"{CONFIDENCE_THRESHOLD_ENV} must be in [0, 1], got {self.confidence_threshold!r}")
        if not _is_finite(self.circuit_open_sec) or self.circuit_open_sec < 0:
            raise ValueError(f"{CIRCUIT_OPEN_ENV} must be >= 0, got {self.circuit_open_sec!r}")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "SelectorConfig":
        """호출 쪽이 os.environ 을 넘긴다(테스트는 dict)."""
        kind = (environ.get(SELECTOR_ENV) or "").strip().lower() or SELECTOR_RULE
        d1_url = (environ.get(D1_URL_ENV) or "").strip() or None

        return cls(
            kind=kind,
            d1_url=d1_url,
            request_timeout_ms=_parse_float_env(environ, D1_TIMEOUT_ENV, D1_REQUEST_TIMEOUT_MS),
            confidence_threshold=_parse_float_env(environ, CONFIDENCE_THRESHOLD_ENV, CONFIDENCE_THRESHOLD),
            circuit_open_sec=_parse_float_env(environ, CIRCUIT_OPEN_ENV, CIRCUIT_OPEN_SEC),
            param_defaults=ParamDefaults(),
        )


def create_decision_model(config: SelectorConfig) -> DecisionModel:
    """rule → RuleSelector, d1 → FallbackSelector(D1Selector, RuleSelector). 네트워크는 쓰지 않는다."""
    rule_selector = RuleSelector(config.param_defaults, config.allow_return_home)
    if config.kind == SELECTOR_RULE:
        return rule_selector

    client = SystemOneClient(config.d1_url, config.request_timeout_ms / MS_PER_SEC)
    d1_selector = D1Selector(client, config.param_defaults, config.confidence_threshold, config.allow_return_home)

    return FallbackSelector(
        primary=d1_selector,
        fallback=rule_selector,
        circuit_open_sec=config.circuit_open_sec,
        param_defaults=config.param_defaults,
    )


# --- 모듈 내부 도우미 ---


def _parse_float_env(environ: Mapping[str, str], name: str, default: float) -> float:
    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return float(raw_value)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from None


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
