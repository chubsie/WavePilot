"""LLM 레이어 패키지. 외부 코드는 이 최상위 모듈의 공개 이름만 import 한다 (D-03, IF-06).

사용 예:
    module = create_llm_module(LLMModuleConfig(backend="fake"))
    payload = module.process(tracks, telemetry).to_report_payload()
"""

from llm_module.backend import (
    Completion,
    LLMBackend,
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from llm_module.decision import Answer, Decision, DecisionModel, RuleBasedDecisionModel
from llm_module.facade import LLMModule, LLMModuleConfig, ModuleOutput, create_llm_module
from llm_module.report_schema import ReportFormatError, ReportSentence, SituationReport
from llm_module.state import StateJson

__all__ = [
    # SKELETON 계약 (01-06 이 조립에 쓴다)
    "LLMModule",
    "LLMModuleConfig",
    "ModuleOutput",
    "create_llm_module",
    "StateJson",
    "Decision",
    "SituationReport",
    "LLMBackendError",
    "ReportFormatError",
    # 확장용 — 외부 담당자가 LLMModule 에 백엔드·판단 모델을 직접 주입할 때 필요
    "LLMBackend",
    "Completion",
    "LLMUnavailableError",
    "LLMTimeoutError",
    "LLMResponseError",
    "DecisionModel",
    "RuleBasedDecisionModel",
    "Answer",
    "ReportSentence",
]
