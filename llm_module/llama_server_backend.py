"""llama-server(OpenAI 호환 HTTP)를 표준 라이브러리만으로 부르는 LLMBackend 구현.

Mac 로컬과 Orin 컨테이너를 base_url 만 바꿔 같은 코드로 부른다(IF-04, D-07: urllib 만 사용).
전제: 서버는 --jinja 로 기동되어 chat_template_kwargs 를 읽고, thinking 은 서버 기본값에 기대지 않고
요청마다 끈다(AI-SPEC Pitfall 4·6). 예외·Completion 은 llm_module.backend(01-01)의 것을 그대로 쓴다.
로깅은 하지 않는다 — 호출 메트릭 로그는 01-04 ReportGenerator 담당.
"""

import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from llm_module.backend import (
    Completion,
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
HEALTH_PATH = "/health"
JSON_SCHEMA_NAME = "situation_report"
HEALTH_TIMEOUT_SEC = 2.0
MAX_RESPONSE_BYTES = 1_048_576
ERROR_DETAIL_MAX_CHARS = 300
SHAPE_SUMMARY_MAX_CHARS = 200
ALLOWED_URL_SCHEMES = ("http", "https")


def _validate_base_url(base_url: str) -> None:
    """urllib 은 file:// 등도 열 수 있으므로 http/https + 호스트가 있는 URL 만 허용한다."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ALLOWED_URL_SCHEMES or not parts.netloc:
        raise ValueError(f"base_url must be an http(s) URL with a host, got {base_url!r}")


def _validate_timeout(request_timeout_sec: float) -> None:
    """urlopen 의 기본 timeout 은 무한 대기라, 유한한 양수만 받는다(AI-SPEC Pitfall 5(a))."""
    if not math.isfinite(request_timeout_sec) or request_timeout_sec <= 0:
        raise ValueError("request_timeout_sec must be a finite number > 0")


def _is_token_count(value: object) -> bool:
    # bool 은 int 의 하위 클래스라 따로 걸러낸다
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class LlamaServerBackend:
    """llama-server /v1/chat/completions 로 스키마 강제 JSON 을 받는 백엔드."""

    __slots__ = (
        "_base_url",
        "_model",
        "_temperature",
        "_top_p",
        "_request_timeout_sec",
        "_api_key",
        "_opener",
    )

    def __init__(
        self,
        base_url: str,
        model: str = "nemotron-3-nano-4b",
        temperature: float = 0.0,
        top_p: float = 1.0,
        request_timeout_sec: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        _validate_base_url(base_url)
        _validate_timeout(request_timeout_sec)

        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._top_p = top_p
        self._request_timeout_sec = request_timeout_sec
        self._api_key = api_key
        # 빈 프록시 표를 준 opener: llama-server 는 항상 로컬(127.0.0.1) 또는 LAN(Orin)이라 프록시가
        # 필요 없다. 개발 머신의 HTTP_PROXY 가 요청을 가로채 프롬프트가 밖으로 새는 것을 막는다(Pitfall 5(d))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:
        # api_key 값은 절대 표시하지 않는다(설정 여부만)
        api_key_label = "<set>" if self._api_key is not None else "None"
        return (
            f"LlamaServerBackend(base_url={self._base_url!r}, model={self._model!r}, "
            f"request_timeout_sec={self._request_timeout_sec}, api_key={api_key_label})"
        )

    def is_ready(self) -> bool:
        """GET /health 가 200 이면 True. _fetch_json 이 모든 실패를 LLMBackendError 로 바꾸므로 새지 않는다."""
        request = self._build_request(HEALTH_PATH, payload=None)

        try:
            self._fetch_json(request, HEALTH_TIMEOUT_SEC)
        except LLMBackendError:
            return False

        return True

    def complete_json(
        self, system_prompt: str, user_prompt: str, schema: dict, max_tokens: int
    ) -> Completion:
        payload = self._build_chat_payload(system_prompt, user_prompt, schema, max_tokens)
        request = self._build_request(CHAT_COMPLETIONS_PATH, payload)

        started_sec = time.monotonic()
        body = self._fetch_json(request, self._request_timeout_sec)
        latency_sec = time.monotonic() - started_sec

        return self._parse_completion(body, latency_sec)

    def _build_chat_payload(
        self, system_prompt: str, user_prompt: str, schema: dict, max_tokens: int
    ) -> dict:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "top_p": self._top_p,
            "max_tokens": max_tokens,
            "stream": False,
            # thinking 기본값이 on 인 모델이라 매 요청 끈다(Pitfall 4)
            "chat_template_kwargs": {"enable_thinking": False},
            # OpenAI 중첩형만 쓴다: 서버 소스는 response_format.json_schema.schema 만 읽고,
            # README 의 {"type": "json_schema", "schema": ...} 모양은 조용히 무시된다(Pitfall 2)
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": JSON_SCHEMA_NAME, "strict": True, "schema": schema},
            },
        }

    def _build_request(self, path: str, payload: dict | None) -> urllib.request.Request:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if self._api_key is not None:
            # Orin 쪽 --api-key 대비. 헤더 값은 어떤 메시지에도 넣지 않는다
            headers["Authorization"] = f"Bearer {self._api_key}"

        if payload is None:
            return urllib.request.Request(url, headers=headers, method="GET")

        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(url, data=data, headers=headers, method="POST")

    def _fetch_json(self, request: urllib.request.Request, timeout_sec: float) -> dict:
        """요청을 보내 JSON 객체를 돌려준다. 모든 실패를 LLMBackendError 계열로 바꾼다.

        except 순서가 의미를 가진다(AI-SPEC _send, Pitfall 5).
        """
        target = f"{self._base_url}{urllib.parse.urlsplit(request.full_url).path}"

        try:
            with self._opener.open(request, timeout=timeout_sec) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_body) > MAX_RESPONSE_BYTES:
                raise LLMResponseError(
                    f"response body exceeds {MAX_RESPONSE_BYTES} bytes from {target}"
                )
            body = json.loads(raw_body.decode("utf-8"))
        # HTTPError 는 URLError 의 하위 클래스라 먼저 잡아야 한다(Pitfall 5(b))
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise LLMUnavailableError(
                    f"server is loading the model (503) at {target}"
                ) from error
            detail = self._read_error_detail(error)
            raise LLMResponseError(f"HTTP {error.code}: {detail}") from error
        # 연결 거부·DNS 실패·연결 단계 타임아웃
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise LLMTimeoutError(f"timed out connecting to {target}") from error
            raise LLMUnavailableError(f"cannot reach {self._base_url}: {error.reason}") from error
        # 본문을 읽는 도중의 타임아웃은 URLError 로 감싸지지 않는다(Pitfall 5(c))
        except TimeoutError as error:
            raise LLMTimeoutError(f"timed out waiting for {target}") from error
        # 서버가 응답 도중 죽거나 연결을 끊음
        except ConnectionError as error:
            raise LLMUnavailableError(f"connection lost to {target}") from error
        # IncompleteRead, RemoteDisconnected 이외의 프로토콜 오류 등
        except http.client.HTTPException as error:
            raise LLMResponseError(f"malformed HTTP response from {target}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LLMResponseError(f"response body is not JSON from {target}") from error

        if not isinstance(body, dict):
            raise LLMResponseError(f"response body is not a JSON object from {target}")

        return body

    @staticmethod
    def _read_error_detail(error: urllib.error.HTTPError) -> str:
        """오류 응답 본문 앞부분만 읽는다. 읽기 자체가 실패해도 원래 오류 매핑을 유지한다."""
        try:
            raw_detail = error.read(ERROR_DETAIL_MAX_CHARS * 4)
        except (OSError, http.client.HTTPException):
            return "<unreadable body>"

        return raw_detail.decode("utf-8", errors="replace")[:ERROR_DETAIL_MAX_CHARS]

    @staticmethod
    def _parse_completion(body: dict, latency_sec: float) -> Completion:
        """서버 응답을 신뢰하지 않고 타입까지 다시 확인한다(T-01-07-01).

        finish_reason 은 값 그대로 넘긴다 — "length"(잘림) 재시도 판단은 01-04 ReportGenerator 의
        책임이라 여기서 오류로 바꾸지 않는다. content 의 JSON·스키마 검증도 01-04 parse_report 몫이다.
        """
        choices = body.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else None
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        finish_reason = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None
        usage = body.get("usage")
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None

        is_valid_shape = (
            isinstance(content, str)
            and isinstance(finish_reason, str)
            and _is_token_count(prompt_tokens)
            and _is_token_count(completion_tokens)
        )
        if not is_valid_shape:
            summary = json.dumps(body)[:SHAPE_SUMMARY_MAX_CHARS]
            raise LLMResponseError(f"unexpected response shape: {summary}")

        return Completion(
            text=content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_sec=latency_sec,
        )
