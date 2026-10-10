"""보고 LLM 백엔드 계약 — LLMBackend Protocol, Completion, 오류 계층."""

from modules.llm_report.interface.backend import (
    Completion,
    LLMBackend,
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

__all__ = [
    "Completion",
    "LLMBackend",
    "LLMBackendError",
    "LLMResponseError",
    "LLMTimeoutError",
    "LLMUnavailableError",
]
