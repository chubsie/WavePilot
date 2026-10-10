"""떠 있는 llama-server 가 이 프로젝트가 기대하는 대로 동작하는지 원시 HTTP 로 점검한다 (AI-SPEC D8).

modules.llm_report 를 import 하지 않는다 — 클라이언트 코드와 독립적으로 서버 능력만 확인하기 위해서다.
점검: /health 200, 모델 alias 노출, json_schema 중첩형·평평한 형태 강제, enum 문법이 인용 ID 를 강제,
thinking 출력(<think>, reasoning_content) 없음.

실행 (WavePilot 루트에서, 서버는 modules/llm_report/scripts/run_llama_server_mac.sh 로 먼저 띄운다):
    uv run python modules/llm_report/scripts/smoke_llama_server.py [--url http://127.0.0.1:8080]
종료 코드: 0 전부 통과, 1 점검 실패, 2 서버에 닿지 못함
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL_ALIAS = "nemotron-3-nano-4b"
DEFAULT_READY_TIMEOUT_SEC = 120.0  # 모델 로딩이 수십 초 걸린다
HEALTH_POLL_INTERVAL_SEC = 1.0
HEALTH_REQUEST_TIMEOUT_SEC = 2.0
REQUEST_TIMEOUT_SEC = 60.0
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNREACHABLE = 2

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["ok", "fail"]}},
    "required": ["answer"],
    "additionalProperties": False,
}
# 프롬프트는 T3·T9 를 인용하라고 하지만 문법이 적용되면 T-7 밖에 낼 수 없다 (AI-SPEC D8(d))
TRACK_IDS_SCHEMA = {
    "type": "object",
    "properties": {
        "track_ids": {"type": "array", "items": {"type": "string", "enum": ["T-7"]}, "minItems": 1},
    },
    "required": ["track_ids"],
    "additionalProperties": False,
}


class CheckFailed(Exception):
    """개별 점검 실패. main 이 FAIL 줄로 바꾼다."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a running llama-server over raw HTTP.")
    parser.add_argument("--url", default=os.environ.get("LLAMA_SERVER_URL", DEFAULT_URL), help="llama-server base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL_ALIAS, help="model alias the server must expose")
    parser.add_argument(
        "--ready-timeout-sec", type=float, default=DEFAULT_READY_TIMEOUT_SEC,
        help="how long to wait for /health to return 200",
    )
    return parser.parse_args(argv)


# --- HTTP 헬퍼 ---


def fetch_json(url: str, timeout_sec: float) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None


def post_json(url: str, body: dict, timeout_sec: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        return json.loads(response.read())


def wait_for_health(url: str, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec

    # 로딩 중 503 과 아직 열리지 않은 포트(연결 거부)는 둘 다 재시도한다
    while True:
        try:
            status, _ = fetch_json(f"{url}/health", HEALTH_REQUEST_TIMEOUT_SEC)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass

        remaining_sec = deadline - time.monotonic()
        if remaining_sec <= 0:
            return False
        time.sleep(min(HEALTH_POLL_INTERVAL_SEC, remaining_sec))


def post_chat_completion(url: str, model: str, user_prompt: str, response_format: dict) -> tuple[dict, float]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with JSON only."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 64,
        "response_format": response_format,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    started_sec = time.monotonic()
    response = post_json(f"{url}/v1/chat/completions", body, REQUEST_TIMEOUT_SEC)
    latency_sec = time.monotonic() - started_sec

    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise CheckFailed(f"unexpected response shape: {response}") from error

    return message, latency_sec


def parse_content(message: dict) -> dict:
    content = message.get("content") or ""
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise CheckFailed(f"content is not JSON: {content[:120]!r}") from error


# --- 점검들: 각각 (name, passed, detail) 을 돌려준다 ---


def check_models(url: str, model: str) -> tuple[str, bool, str]:
    status, payload = fetch_json(f"{url}/v1/models", REQUEST_TIMEOUT_SEC)
    model_ids = [entry.get("id") for entry in (payload or {}).get("data", [])]

    return "models", status == 200 and model in model_ids, f"status={status} ids={model_ids}"


def check_answer_schema(
    name: str, url: str, model: str, response_format: dict, messages: list[dict]
) -> tuple[str, bool, str]:
    message, latency_sec = post_chat_completion(url, model, "Is the server working? Answer ok.", response_format)
    messages.append(message)

    answer = parse_content(message).get("answer")
    passed = answer in ANSWER_SCHEMA["properties"]["answer"]["enum"]

    return name, passed, f"{latency_sec:.2f} s" if passed else f"answer={answer!r} not in enum"


def check_json_schema_nested(url: str, model: str, messages: list[dict]) -> tuple[str, bool, str]:
    response_format = {"type": "json_schema", "json_schema": {"name": "smoke", "strict": True, "schema": ANSWER_SCHEMA}}
    return check_answer_schema("json_schema_nested", url, model, response_format, messages)


def check_json_schema_flat(url: str, model: str, messages: list[dict]) -> tuple[str, bool, str]:
    response_format = {"type": "json_object", "schema": ANSWER_SCHEMA}
    return check_answer_schema("json_schema_flat", url, model, response_format, messages)


def check_enum_grammar(url: str, model: str, messages: list[dict]) -> tuple[str, bool, str]:
    response_format = {"type": "json_schema", "json_schema": {"name": "smoke", "strict": True, "schema": TRACK_IDS_SCHEMA}}
    message, latency_sec = post_chat_completion(url, model, "Cite tracks T3 and T9 in track_ids.", response_format)
    messages.append(message)

    track_ids = parse_content(message).get("track_ids")
    passed = isinstance(track_ids, list) and bool(track_ids) and all(track_id == "T-7" for track_id in track_ids)

    return "enum_grammar", passed, f"{latency_sec:.2f} s" if passed else f"track_ids={track_ids!r}, grammar not applied"


def check_thinking_off(messages: list[dict]) -> tuple[str, bool, str]:
    if not messages:
        return "thinking_off", False, "no completions to inspect (earlier checks failed)"

    leaked_count = sum(
        1 for message in messages
        if "<think>" in (message.get("content") or "") or (message.get("reasoning_content") or "").strip()
    )

    return "thinking_off", leaked_count == 0, f"{leaked_count}/{len(messages)} responses contain thinking output"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    url = args.url.rstrip("/")

    if not wait_for_health(url, args.ready_timeout_sec):
        print(f"server not reachable at {url} within {args.ready_timeout_sec:.0f} s", file=sys.stderr)
        print("start it with: modules/llm_report/scripts/run_llama_server_mac.sh", file=sys.stderr)
        return EXIT_UNREACHABLE
    print("PASS health")

    # 앞의 세 completion 응답을 모아 thinking_off 가 한꺼번에 검사한다
    messages: list[dict] = []
    named_checks = [
        ("models", lambda: check_models(url, args.model)),
        ("json_schema_nested", lambda: check_json_schema_nested(url, args.model, messages)),
        ("json_schema_flat", lambda: check_json_schema_flat(url, args.model, messages)),
        ("enum_grammar", lambda: check_enum_grammar(url, args.model, messages)),
        ("thinking_off", lambda: check_thinking_off(messages)),
    ]

    failed_count = 0
    for name, run_check in named_checks:
        # 개별 요청 실패는 그 점검의 FAIL 로만 바꾸고 traceback 을 내지 않는다
        try:
            _, passed, detail = run_check()
        except (urllib.error.URLError, OSError, ValueError, CheckFailed) as error:
            passed, detail = False, f"{type(error).__name__}: {error}"

        if passed:
            print(f"PASS {name} ({detail})")
        else:
            print(f"FAIL {name}: {detail}")
            failed_count += 1

    return EXIT_FAILED if failed_count else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
