"""LLM 실행 백엔드 계약 — 보고·판단 로직은 이 Protocol 만 알고 서버 종류를 모른다.

계약은 AI-SPEC §3 을 따른다(complete_json + is_ready). RESEARCH 의 ChatRequest/complete 스케치보다
AI-SPEC 을 채택한 이유: AI-SPEC 이 LLM 통합의 정본이고, 스키마 강제 JSON 출력을 계약 수준에서 드러낸다.
HTTP 구현(LlamaServerBackend)은 01-07 에서 같은 패키지에 둔다. 여기에는 전송 코드가 없다.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMBackendError(Exception):
    """백엔드 호출 실패의 공통 부모 — 호출측은 이것 하나만 잡아도 된다."""


class LLMUnavailableError(LLMBackendError):
    """서버가 없거나(연결 거부) 아직 모델을 올리는 중(503)."""


class LLMTimeoutError(LLMBackendError):
    """연결 또는 응답 대기 시간 초과."""


class LLMResponseError(LLMBackendError):
    """HTTP 4xx/5xx 이거나 응답 본문이 기대한 모양이 아님."""


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    finish_reason: str  # "stop" 정상, "length" 는 max_tokens 에 잘림
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float


@runtime_checkable
class LLMBackend(Protocol):
    def complete_json(
        self, system_prompt: str, user_prompt: str, schema: dict, max_tokens: int
    ) -> Completion:
        """schema(JSON Schema)를 따르는 JSON 텍스트를 생성한다. 실패는 LLMBackendError 계열."""
        ...

    def is_ready(self) -> bool:
        """모델이 요청을 받을 수 있으면 True. 예외를 던지지 않는다."""
        ...
