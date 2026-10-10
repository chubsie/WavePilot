"""상태 JSON 한 건을 근거 track ID 가 붙은 영어 보고로 바꾼다 (RPT-02, AI-SPEC §4 Core Pattern).

LLM 은 판정·비상 전환 권한이 없다. 이 클래스는 보고 문장만 만들고, 실패는 그대로 올린다(fail fast).
실패가 제어 경로를 막지 않게 격리하는 것은 파사드(LLMModule) 책임이다.

흐름: 무트랙이면 호출 생략(G1) → 입력 ID 로 enum 을 고정한 스키마 구성(G2) → 호출 →
잘림(finish_reason "length")만 1회 재시도 → parse_report 재검증(G3·G4) → 정책 flag(G5).
temperature 0 이라 같은 입력은 같은 결과이므로, 잘림이 아닌 검증 실패는 재시도하지 않는다.
전송 세부(HTTP, thinking 끄기)는 백엔드(01-07)에만 있고 여기에는 없다.
"""

import logging
from dataclasses import replace
from typing import Protocol, runtime_checkable

from llm_module.backend import (
    Completion,
    LLMBackend,
    LLMBackendError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from llm_module.report_policy import find_policy_violations
from llm_module.report_prompt import SYSTEM_PROMPT, build_user_prompt
from llm_module.report_schema import ReportFormatError, SituationReport, build_report_schema, parse_report
from llm_module.state import StateJson

logger = logging.getLogger(__name__)

REPORT_MAX_TOKENS = 640  # 문장 6개 × 약 80 토큰 + 여유 30% (AI-SPEC §4, 실측으로 조정)
REPORT_MAX_TOKENS_CAP = 1280  # 재시도 상한: -c 4096 안에서 입력(약 2,500)과 합쳐 넘지 않게
MAX_ATTEMPTS = 2  # 잘림 재시도 1회까지만 — 무한 재시도 금지
CONTEXT_TOKENS = 4096  # 서버 -c
CONTEXT_WARN_RATIO = 0.8  # 사용률이 이 이상이면 상태 크기 증가를 조기에 알린다
CHARS_PER_TOKEN_ESTIMATE = 3  # 보수적 추정 (영어·JSON 은 보통 3~4 자/토큰)
RAW_TEXT_LOG_CHARS = 200  # 검증 실패 원문은 앞부분만 콘솔 로그에 남긴다
NO_TRACKS_FLAG = "no tracks in state; LLM skipped"


@runtime_checkable
class TraceSink(Protocol):
    """호출 시도 1회 = event 1개를 받는 기록 지점. 파일 구현(JsonlTraceSink)은 이 모듈 밖이다."""

    def record(self, event: dict) -> None: ...


class ReportGenerator:
    def __init__(self, backend: LLMBackend, trace_sink: TraceSink | None = None) -> None:
        self._backend = backend
        self._trace_sink = trace_sink

    def generate(self, state: StateJson) -> SituationReport:
        labels = state.track_labels
        if not labels:
            # 근거로 삼을 track 이 없으면 "문장마다 근거 ID" 규칙을 지킬 수 없으므로 LLM 을 부르지 않는다
            self._record_trace(_make_trace_event(0, 0, labels, "") | {"outcome": "skipped_no_tracks"})
            return SituationReport(sentences=(), flags=(NO_TRACKS_FLAG,))

        schema = build_report_schema(labels)
        user_prompt = build_user_prompt(state)
        self._warn_if_prompt_too_large(user_prompt)

        max_tokens = REPORT_MAX_TOKENS
        for attempt in range(1, MAX_ATTEMPTS + 1):
            event = _make_trace_event(attempt, max_tokens, labels, user_prompt)
            completion = self._call_backend(schema, user_prompt, max_tokens, event)
            self._log_completion(attempt, completion)
            event |= _describe_completion(completion)

            if completion.finish_reason == "length":
                self._record_trace(event | {"outcome": "truncated"})
                if attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "report truncated attempt=%d max_tokens=%d completion_tokens=%d; retrying",
                        attempt, max_tokens, completion.completion_tokens,
                    )
                    max_tokens = min(max_tokens * 2, REPORT_MAX_TOKENS_CAP)
                    continue
                # 잘린 텍스트가 우연히 JSON 이 되는 경우까지 막으려고 파싱 전에 명시적으로 올린다
                raise ReportFormatError(f"report truncated at max_tokens={max_tokens}")

            report = self._validate_completion(completion, labels, event)
            self._warn_if_context_nearly_full(completion)
            return report

        raise ReportFormatError("unreachable: attempts exhausted")  # 루프는 항상 return/raise 로 끝난다

    def _call_backend(self, schema: dict, user_prompt: str, max_tokens: int, event: dict) -> Completion:
        try:
            return self._backend.complete_json(SYSTEM_PROMPT, user_prompt, schema, max_tokens)
        except LLMBackendError as error:
            # 재시도하지 않고 그대로 올린다. trace 에만 실패 종류를 남긴다
            self._record_trace(event | {"outcome": _classify_backend_error(error), "error": str(error)})
            raise

    def _validate_completion(self, completion: Completion, labels: tuple[str, ...], event: dict) -> SituationReport:
        try:
            report = parse_report(completion.text, labels)
        except ReportFormatError as error:
            logger.error(
                "report format error: %s finish=%s raw=%r",
                error, completion.finish_reason, completion.text[:RAW_TEXT_LOG_CHARS],
            )
            self._record_trace(event | {"outcome": "format_error", "error": str(error)})
            raise

        # E-03: 정책 위반은 차단이 아니라 표시 — 문장은 그대로 두고 flags 뒤에 붙인다
        policy_flags = tuple(find_policy_violations(report))
        report = replace(report, flags=report.flags + policy_flags)
        if report.flags:
            logger.warning("report flagged: %s", list(report.flags))

        outcome = "flagged" if report.flags else "ok"
        self._record_trace(event | {"outcome": outcome, "flags": list(report.flags)})

        return report

    def _log_completion(self, attempt: int, completion: Completion) -> None:
        logger.info(
            "report call attempt=%d finish=%s prompt_tokens=%d completion_tokens=%d latency=%.2f [s]",
            attempt, completion.finish_reason, completion.prompt_tokens,
            completion.completion_tokens, completion.latency_sec,
        )

    def _warn_if_prompt_too_large(self, user_prompt: str) -> None:
        estimated_prompt_tokens = (len(SYSTEM_PROMPT) + len(user_prompt)) // CHARS_PER_TOKEN_ESTIMATE
        if estimated_prompt_tokens + REPORT_MAX_TOKENS > CONTEXT_TOKENS:
            logger.warning(
                "estimated prompt %d tokens + max_tokens %d exceeds context %d",
                estimated_prompt_tokens, REPORT_MAX_TOKENS, CONTEXT_TOKENS,
            )

    def _warn_if_context_nearly_full(self, completion: Completion) -> None:
        used_tokens = completion.prompt_tokens + completion.completion_tokens
        if used_tokens > CONTEXT_TOKENS * CONTEXT_WARN_RATIO:
            logger.warning("context usage %d / %d tokens is above %.0f%%", used_tokens, CONTEXT_TOKENS,
                           CONTEXT_WARN_RATIO * 100)

    def _record_trace(self, event: dict) -> None:
        if self._trace_sink is not None:
            self._trace_sink.record(event)


# --- 모듈 내부 도우미 ---


def _make_trace_event(attempt: int, max_tokens: int, labels: tuple[str, ...], user_prompt: str) -> dict:
    # 모든 event 가 같은 키 집합을 갖게 기본값으로 채운다 (JSONL 집계가 쉬워진다)
    return {
        "attempt": attempt,
        "max_tokens": max_tokens,
        "schema_track_ids": list(labels),
        "user_prompt": user_prompt,
        "response_text": None,
        "finish_reason": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "latency_sec": None,
        "outcome": None,
        "flags": [],
        "error": None,
    }


def _describe_completion(completion: Completion) -> dict:
    return {
        "response_text": completion.text,
        "finish_reason": completion.finish_reason,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "latency_sec": completion.latency_sec,
    }


def _classify_backend_error(error: LLMBackendError) -> str:
    if isinstance(error, LLMUnavailableError):
        return "unavailable"
    if isinstance(error, LLMTimeoutError):
        return "timeout"
    return "backend_error"
