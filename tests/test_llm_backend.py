"""LlamaServerBackend 단위 테스트 — 실서버 없이 표준 http.server 스레드로 llama-server 동작을 재현한다.

요청 본문 모양(OpenAI 중첩 json_schema, thinking 끄기), 오류 매핑(Unavailable/Timeout/Response),
/health 기반 is_ready, 유한 timeout, 인증 헤더 없음, 프록시 무시를 고정한다(IF-04, AI-SPEC Pitfall 2·4·5).
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modules.llm_report.interface.backend import (
    LLMBackend,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from modules.llm_report.llama_server_backend import (
    MAX_RESPONSE_BYTES,
    LlamaServerBackend,
)

TEST_SCHEMA = {
    "type": "object",
    "properties": {"sentences": {"type": "array", "items": {"type": "string"}}},
    "required": ["sentences"],
    "additionalProperties": False,
}
SHORT_TIMEOUT_SEC = 0.3
HANG_WAIT_SEC = 5.0
SERVER_POLL_INTERVAL_SEC = 0.02  # shutdown() 대기를 줄여 테스트 전체를 빠르게 유지


# ---------------------------------------------------------------------------
# 가짜 llama-server
# ---------------------------------------------------------------------------


class FakeLlamaServer(ThreadingHTTPServer):
    """테스트가 정한 "다음 응답"을 돌려주고, 받은 요청을 기록하는 서버."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeLlamaHandler)
        self.response_status = 200
        self.response_body = b"{}"
        self.should_hang = False
        self.release_event = threading.Event()
        self.received_requests: list[dict] = []

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def set_json_response(self, body: dict, status: int = 200) -> None:
        self.response_status = status
        self.response_body = json.dumps(body).encode("utf-8")

    def set_raw_response(self, body: bytes, status: int = 200) -> None:
        self.response_status = status
        self.response_body = body


class FakeLlamaHandler(BaseHTTPRequestHandler):
    server: FakeLlamaServer

    def do_GET(self) -> None:  # noqa: N802 — http.server 규약 이름
        self._record_request(body=None)
        self._send_configured_response()

    def do_POST(self) -> None:  # noqa: N802 — http.server 규약 이름
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        self._record_request(body=json.loads(raw_body))
        self._send_configured_response()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _record_request(self, body: dict | None) -> None:
        self.server.received_requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )

    def _send_configured_response(self) -> None:
        if self.server.should_hang:
            self.server.release_event.wait(HANG_WAIT_SEC)
            return

        try:
            self.send_response(self.server.response_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.server.response_body)))
            self.end_headers()
            self.wfile.write(self.server.response_body)
        except (BrokenPipeError, ConnectionResetError):
            # 클라이언트가 상한까지만 읽고 끊는 경우(크기 초과 테스트)
            pass


@pytest.fixture
def fake_llama_server() -> Iterator[FakeLlamaServer]:
    server = FakeLlamaServer()
    thread = threading.Thread(
        target=server.serve_forever, args=(SERVER_POLL_INTERVAL_SEC,), daemon=True
    )
    thread.start()

    yield server

    server.release_event.set()
    server.shutdown()
    server.server_close()


def make_chat_response(
    content: str | None,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 120,
    completion_tokens: int = 30,
) -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def get_closed_port() -> int:
    """바인드 후 바로 닫아, 아무도 listen 하지 않는 포트를 얻는다."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def call_complete_json(backend: LlamaServerBackend, max_tokens: int = 640):
    return backend.complete_json("system text", "user text", TEST_SCHEMA, max_tokens)


# ---------------------------------------------------------------------------
# 정상 응답과 요청 모양
# ---------------------------------------------------------------------------


def test_success_response_returns_completion(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response('{"sentences": []}'))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    completion = call_complete_json(backend)

    assert completion.text == '{"sentences": []}'
    assert completion.finish_reason == "stop"
    assert completion.prompt_tokens == 120
    assert completion.completion_tokens == 30
    assert completion.latency_sec >= 0.0


def test_request_targets_chat_completions_with_post_json(
    fake_llama_server: FakeLlamaServer,
) -> None:
    fake_llama_server.set_json_response(make_chat_response("{}"))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    call_complete_json(backend)

    request = fake_llama_server.received_requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Content-Type"] == "application/json"


def test_request_body_has_fixed_sampling_and_messages(
    fake_llama_server: FakeLlamaServer,
) -> None:
    fake_llama_server.set_json_response(make_chat_response("{}"))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    call_complete_json(backend, max_tokens=640)

    body = fake_llama_server.received_requests[0]["body"]
    assert body["model"] == "nemotron-3-nano-4b"
    assert body["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "user text"},
    ]
    assert body["temperature"] == 0.0
    assert body["top_p"] == 1.0
    assert body["max_tokens"] == 640
    assert body["stream"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_request_body_uses_nested_json_schema(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response("{}"))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    call_complete_json(backend)

    body = fake_llama_server.received_requests[0]["body"]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "situation_report", "strict": True, "schema": TEST_SCHEMA},
    }
    # README 모양(최상위 schema, response_format.schema)은 서버가 무시하므로 금지 (Pitfall 2)
    assert "schema" not in body
    assert "schema" not in body["response_format"]
    assert "tools" not in body


def test_request_has_no_authorization_header(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response("{}"))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    call_complete_json(backend)

    headers = fake_llama_server.received_requests[0]["headers"]
    assert "Authorization" not in headers


def test_finish_reason_length_is_passed_through(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response('{"sent', finish_reason="length"))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    completion = call_complete_json(backend)

    assert completion.finish_reason == "length"
    assert completion.text == '{"sent'


# ---------------------------------------------------------------------------
# 오류 매핑
# ---------------------------------------------------------------------------


def test_http_503_raises_unavailable(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response({"error": "loading model"}, status=503)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMUnavailableError):
        call_complete_json(backend)


@pytest.mark.parametrize("status", [500, 400])
def test_http_error_status_raises_response_error_with_detail(
    fake_llama_server: FakeLlamaServer, status: int
) -> None:
    detail = "x" * 1000
    fake_llama_server.set_raw_response(f"server says {detail}".encode(), status=status)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError) as error_info:
        call_complete_json(backend)

    message = str(error_info.value)
    assert str(status) in message
    assert "server says" in message
    assert len(message) < 400  # 서버 본문은 최대 300자로 잘린다


def test_server_hang_raises_timeout_quickly(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.should_hang = True
    backend = LlamaServerBackend(fake_llama_server.base_url, request_timeout_sec=SHORT_TIMEOUT_SEC)

    started_sec = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        call_complete_json(backend)
    elapsed_sec = time.monotonic() - started_sec

    assert elapsed_sec < 2.0


def test_connection_refused_raises_unavailable() -> None:
    backend = LlamaServerBackend(f"http://127.0.0.1:{get_closed_port()}")

    with pytest.raises(LLMUnavailableError):
        call_complete_json(backend)


def test_non_json_body_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_raw_response(b"<html>not json</html>")
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_non_object_json_body_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_raw_response(b"[1, 2, 3]")
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_empty_choices_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    response = make_chat_response("{}")
    response["choices"] = []
    fake_llama_server.set_json_response(response)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_null_content_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response(None))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_missing_usage_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    response = make_chat_response("{}")
    del response["usage"]
    fake_llama_server.set_json_response(response)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_missing_finish_reason_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    response = make_chat_response("{}")
    del response["choices"][0]["finish_reason"]
    fake_llama_server.set_json_response(response)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_boolean_token_count_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    response = make_chat_response("{}")
    response["usage"]["prompt_tokens"] = True
    fake_llama_server.set_json_response(response)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError):
        call_complete_json(backend)


def test_oversized_body_raises_response_error(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_raw_response(b" " * (MAX_RESPONSE_BYTES + 1024))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    with pytest.raises(LLMResponseError, match="exceeds"):
        call_complete_json(backend)


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------


def test_health_200_is_ready(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response({"status": "ok"})
    backend = LlamaServerBackend(fake_llama_server.base_url)

    assert backend.is_ready() is True

    request = fake_llama_server.received_requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/health"


def test_health_503_is_not_ready(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response({"error": "loading"}, status=503)
    backend = LlamaServerBackend(fake_llama_server.base_url)

    assert backend.is_ready() is False


def test_health_connection_refused_is_not_ready() -> None:
    backend = LlamaServerBackend(f"http://127.0.0.1:{get_closed_port()}")

    assert backend.is_ready() is False


# ---------------------------------------------------------------------------
# 생성자 검증 · 비노출 · 프로토콜 · 프록시
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeout_sec", [0, -1.0, float("inf"), float("nan")])
def test_invalid_timeout_raises_value_error(timeout_sec: float) -> None:
    with pytest.raises(ValueError):
        LlamaServerBackend("http://127.0.0.1:8080", request_timeout_sec=timeout_sec)


@pytest.mark.parametrize("base_url", ["file:///etc/passwd", "ftp://x", "http://", "127.0.0.1:8080"])
def test_non_http_base_url_raises_value_error(base_url: str) -> None:
    with pytest.raises(ValueError):
        LlamaServerBackend(base_url)


def test_trailing_slash_base_url_has_no_double_slash(fake_llama_server: FakeLlamaServer) -> None:
    fake_llama_server.set_json_response(make_chat_response("{}"))
    backend = LlamaServerBackend(fake_llama_server.base_url + "/")

    call_complete_json(backend)

    assert fake_llama_server.received_requests[0]["path"] == "/v1/chat/completions"


def test_backend_satisfies_llm_backend_protocol() -> None:
    assert isinstance(LlamaServerBackend("http://127.0.0.1:1"), LLMBackend)


def test_proxy_environment_is_ignored(
    fake_llama_server: FakeLlamaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    fake_llama_server.set_json_response(make_chat_response('{"sentences": []}'))
    backend = LlamaServerBackend(fake_llama_server.base_url)

    completion = call_complete_json(backend)

    assert completion.text == '{"sentences": []}'
