"""llm_module 최상위 파사드 — 외부 코드가 아는 유일한 LLM 레이어 입구 (IF-06, D-03).

track 목록 + 텔레메트리(또는 상태 JSON dict)를 받아 상태 JSON → 판단 → 보고를 한 번에 돌려준다.
보고 LLM 실패는 예외가 아니라 ModuleOutput.report_error 로 바뀐다 — 보고는 부가 정보라서
LLM 이 죽거나 느려도 드론 측 제어 루프가 멈추면 안 된다(AI-SPEC G3·G6).
판단(Decision)은 규칙 stub 이 내므로 보고 실패와 무관하게 항상 있다.

파사드가 자기 내부 구현(RuleBasedDecisionModel, FakeReportBackend, LlamaServerBackend)을 조립하는 것은
허용된다(RESEARCH Pattern 3). 확장·테스트용으로 LLMModule 직접 주입 생성자도 공개한다.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from interfaces.flight_control import Telemetry
from interfaces.track import Track
from llm_module.backend import (
    LLMBackend,
    LLMBackendError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from llm_module.decision import DEFAULT_QUESTIONS, Decision, DecisionModel, RuleBasedDecisionModel
from llm_module.fake_backend import FakeReportBackend
from llm_module.report_generator import ReportGenerator
from llm_module.report_schema import ReportFormatError, SituationReport
from llm_module.state import DEFAULT_MAX_TRACKS, StateJson, build_state_json, truncate_state

logger = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "http://127.0.0.1:8080"
DEFAULT_REQUEST_TIMEOUT_SEC = 60.0  # Mac 기준 임의값, Jetson 은 설정으로 바꾼다 (AI-SPEC §4)
BACKEND_KINDS = ("fake", "llama_server")
READY_POLL_INTERVAL_SEC = 1.0  # 모델 로딩은 수십 초 단위라 1 s 간격이면 충분하다
REPORT_ERROR_MAX_CHARS = 200  # 운용자 콘솔 한 줄에 들어가게


@dataclass(frozen=True, slots=True)
class LLMModuleConfig:
    backend: str = "fake"
    backend_url: str = DEFAULT_BACKEND_URL
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    api_key: str | None = None  # 로그·payload 에 넣지 않는다
    max_tracks: int = DEFAULT_MAX_TRACKS

    def __post_init__(self) -> None:
        if self.backend not in BACKEND_KINDS:
            raise ValueError(f"field 'backend' must be one of {BACKEND_KINDS}, got {self.backend!r}")
        if self.max_tracks < 1:
            raise ValueError(f"field 'max_tracks' must be >= 1, got {self.max_tracks}")
        if self.request_timeout_sec <= 0:
            raise ValueError(f"field 'request_timeout_sec' must be > 0, got {self.request_timeout_sec}")


@dataclass(frozen=True, slots=True)
class ModuleOutput:
    state: StateJson
    decision: Decision
    report: SituationReport | None  # 보고 생성 실패면 None
    report_error: str | None  # "unavailable: ..." 처럼 분류가 앞에 붙은 짧은 문자열

    def to_report_payload(self) -> dict:
        """SKELETON report 메시지 페이로드 모양 그대로."""
        return {
            "time_sec": self.state.time_sec,
            "decision": self.decision.to_dict(),
            "report": self.report.to_dict() if self.report is not None else None,
            "report_error": self.report_error,
        }


class LLMModule:
    def __init__(
        self, decision_model: DecisionModel, backend: LLMBackend, max_tracks: int = DEFAULT_MAX_TRACKS
    ) -> None:
        self._decision_model = decision_model
        self._backend = backend
        self._max_tracks = max_tracks
        self._report_generator = ReportGenerator(backend)

    def build_state(self, tracks: Sequence[Track], telemetry: Telemetry) -> StateJson:
        return build_state_json(tracks, telemetry, self._max_tracks)

    def decide(self, state: StateJson) -> Decision:
        return self._decision_model.decide(state, DEFAULT_QUESTIONS)

    def generate_report(self, state: StateJson) -> SituationReport:
        """LLMBackendError / ReportFormatError 를 그대로 올린다. 격리가 필요하면 process 계열을 쓴다."""
        return self._report_generator.generate(state)

    def process(self, tracks: Sequence[Track], telemetry: Telemetry) -> ModuleOutput:
        state = self.build_state(tracks, telemetry)
        return self._run(state)

    def process_state(self, state: StateJson | dict) -> ModuleOutput:
        # 외부 dict 는 신뢰하지 않는다 — 검증 실패는 ValueError 로 호출자에게 올린다
        validated_state = state if isinstance(state, StateJson) else StateJson.from_dict(state)
        truncated_state = truncate_state(validated_state, self._max_tracks)
        return self._run(truncated_state)

    def wait_until_ready(self, timeout_sec: float) -> None:
        deadline = time.monotonic() + timeout_sec

        while not self._backend.is_ready():
            remaining_sec = deadline - time.monotonic()
            if remaining_sec <= 0:
                raise LLMUnavailableError(f"LLM backend not ready after {timeout_sec:.0f} [s]")
            time.sleep(min(READY_POLL_INTERVAL_SEC, remaining_sec))

    def _run(self, state: StateJson) -> ModuleOutput:
        decision = self.decide(state)

        # 보고 실패만 잡는다. 그 밖의 예외는 프로그래밍 오류라서 숨기지 않는다
        try:
            report, report_error = self.generate_report(state), None
        except (LLMBackendError, ReportFormatError) as error:
            report, report_error = None, _describe_report_error(error)
            logger.error("report unavailable (%s)", report_error)

        return ModuleOutput(state=state, decision=decision, report=report, report_error=report_error)


def create_llm_module(config: LLMModuleConfig | None = None) -> LLMModule:
    config = config or LLMModuleConfig()

    backend = _create_backend(config)

    return LLMModule(decision_model=RuleBasedDecisionModel(), backend=backend, max_tracks=config.max_tracks)


# --- 모듈 내부 도우미 ---


def _create_backend(config: LLMModuleConfig) -> LLMBackend:
    if config.backend == "fake":
        return FakeReportBackend()

    # 01-07 이 같은 웨이브에 병렬로 만드는 모듈이라 모듈 수준에서 import 하지 않는다.
    # 지연 import 덕에 fake 경로는 HTTP 클라이언트 없이도 돌고, `import llm_module` 이 가볍다.
    from llm_module.llama_server_backend import LlamaServerBackend

    return LlamaServerBackend(
        base_url=config.backend_url,
        request_timeout_sec=config.request_timeout_sec,
        api_key=config.api_key,
    )


def _describe_report_error(error: Exception) -> str:
    if isinstance(error, LLMUnavailableError):
        kind = "unavailable"
    elif isinstance(error, LLMTimeoutError):
        kind = "timeout"
    elif isinstance(error, ReportFormatError):
        kind = "format"
    else:
        kind = "response"  # LLMResponseError 와 그 밖의 LLMBackendError

    return f"{kind}: {error}"[:REPORT_ERROR_MAX_CHARS]
