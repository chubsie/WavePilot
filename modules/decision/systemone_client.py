"""llama.cpp /v1/systemone 에 선택형(choice) 질문을 보내는 표준 라이브러리 HTTP 클라이언트 (SEL-01, SEL-02, D-07).

지연 계측 (SEL-02, RESEARCH §2.3):
- /v1/systemone 응답에는 timings 필드도 처리 시간 헤더도 없다. 그래서 본 요청의 total_ms 는 클라이언트가
  time.monotonic 으로 재고, network_rtt 는 추론 없는 GET /health 왕복으로 추정한다.
- 두 요청 모두 urllib 가 매번 새 TCP 연결을 열기 때문에 연결 비용이 양쪽에 똑같이 들어가 차감하면 상쇄된다.
  orin_processing_ms = max(0, total_ms − network_rtt_ms) 차감 자체는 01-05 D1Selector 몫이다.
- /health probe timeout 은 min(HEALTH_TIMEOUT_SEC, request_timeout_sec) 이다 — 사용자가 준 요청 timeout 보다
  probe 가 오래 막지 않게 한다(WR-02).

예외 매핑 (modules.llm_report.interface 의 예외만 쓴다):
| 상황                                               | 예외                 |
|----------------------------------------------------|----------------------|
| 연결 거부, 연결 끊김, 503(모델 로딩 중)            | LLMUnavailableError  |
| connect 단계·본문 읽기 단계 timeout                | LLMTimeoutError      |
| HTTP 4xx/5xx(400·501 등), 비JSON, 1 MiB 초과, 모양 이상 | LLMResponseError |
| choice·확률 키가 보낸 보기 밖                      | OutOfMenuError       |

OutOfMenuError 는 LLMResponseError 의 하위 클래스라 01-05 는 OutOfMenuError 를 먼저 잡아야
fallback_reason 이 out_of_menu 와 bad_response 로 갈린다.

urllib 의 timeout 은 전체 요청이 아니라 소켓 연산(connect, read) 하나마다 걸리는 값이라 전체 시간이
timeout 을 조금 넘을 수 있다. 로깅은 하지 않는다 — 선택 기록은 01-06 재생 하네스가 남긴다.
HTTP 패턴은 modules/llm_report/llama_server_backend.py 를 복사·적응했다(import 경계 때문에 import 하지 않는다).
"""

import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from modules.llm_report.interface import (
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

SYSTEMONE_PATH = "/v1/systemone"
HEALTH_PATH = "/health"
HEALTH_TIMEOUT_SEC = 0.5  # RTT probe 가 선택을 오래 막지 않게 보고용(2.0)보다 짧게 (RESEARCH §4)
MAX_RESPONSE_BYTES = 1_048_576
ERROR_DETAIL_MAX_CHARS = 300
SHAPE_SUMMARY_MAX_CHARS = 200
ALLOWED_URL_SCHEMES = ("http", "https")
PROBABILITY_SUM_TOLERANCE = 1e-6  # 서버 확률은 반올림하지 않으므로 부동소수 오차만 허용한다
CHOICE_QUESTION_TYPE = "choice"
MS_PER_SEC = 1000.0
REQUEST_JSON_SEPARATORS = (",", ":")


class OutOfMenuError(LLMResponseError):
    """응답의 choice 나 확률 키가 보낸 보기(criteria 키) 밖에 있다.

    정상 서버는 보낸 criteria 키 중 argmax 만 고르므로 실서버로는 재현되지 않는다. 응답 변조나
    서버 버전 차이에 대비한 방어 코드다(RESEARCH §4). 01-05 는 이 오류를 규칙 선택으로 대체한다.
    """


@dataclass(frozen=True, slots=True)
class ChoiceReply:
    choice: str
    probabilities: dict[str, float]  # 보낸 보기 순서로 재배열된 새 dict
    confidence: float  # 서버 정의: max(0, (p_max − 1/n) / (1 − 1/n)), [0, 1]
    input_tokens: int | None  # usage.input_tokens. 없거나 이상하면 None
    total_ms: float  # /v1/systemone 왕복 + JSON 디코드 (클라이언트 time.monotonic)


# --- 요청 모양과 응답 검증 (순수 함수) ---


def build_systemone_payload(state_text: str, question_id: str, instructions: str, criteria: dict[str, str]) -> dict:
    """/v1/systemone 요청 본문의 정본 모양(RESEARCH §2.1). files/images 는 넣지 않는다(--mmproj 없이 501)."""
    if not state_text:
        raise ValueError("state_text must not be empty")
    if not criteria:
        raise ValueError("criteria must not be empty")

    question = {"type": CHOICE_QUESTION_TYPE, "instructions": instructions, "criteria": dict(criteria)}

    return {"state": state_text, "questions": {question_id: question}}


def parse_choice_response(body: dict, question_id: str, allowed: tuple[str, ...], total_ms: float) -> ChoiceReply:
    """서버 응답을 신뢰하지 않고 모양·보기·값을 모두 다시 확인한다(T-01-04-02)."""
    if not allowed:
        raise ValueError("allowed must not be empty")
    if not _is_finite_number(total_ms) or total_ms < 0:
        raise ValueError(f"total_ms must be a finite number >= 0, got {total_ms!r}")

    answers = body.get("answers") if isinstance(body, dict) else None
    answer = answers.get(question_id) if isinstance(answers, dict) else None
    is_valid_shape = (
        isinstance(answer, dict)
        and answer.get("type") == CHOICE_QUESTION_TYPE
        and isinstance(answer.get("choice"), str)
        and isinstance(answer.get("probabilities"), dict)
        and "confidence" in answer
    )
    if not is_valid_shape:
        raise LLMResponseError(f"unexpected systemone answer shape: {_summarize_body(body)}")
    choice = answer["choice"]
    probabilities = answer["probabilities"]
    confidence = answer["confidence"]

    outside_keys = sorted(str(key) for key in probabilities if key not in allowed)
    if choice not in allowed:
        raise OutOfMenuError(f"choice {choice!r} is outside the offered options {allowed}")
    if outside_keys:
        raise OutOfMenuError(f"probabilities contain keys outside the offered options: {outside_keys}")

    _validate_choice_values(choice, probabilities, confidence)

    ordered_probabilities = {mode: float(probabilities[mode]) for mode in allowed if mode in probabilities}
    usage = body.get("usage")
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None

    return ChoiceReply(
        choice=choice,
        probabilities=ordered_probabilities,
        confidence=float(confidence),
        input_tokens=input_tokens if _is_token_count(input_tokens) else None,
        total_ms=total_ms,
    )


# --- HTTP 클라이언트 ---


class SystemOneClient:
    """d1-3B llama-server 의 /v1/systemone 에 choice 질문 1개를 보내고, /health 로 RTT 를 잰다."""

    __slots__ = ("_base_url", "_request_timeout_sec", "_health_timeout_sec", "_opener")

    def __init__(self, base_url: str, request_timeout_sec: float) -> None:
        _validate_base_url(base_url)
        _validate_timeout(request_timeout_sec)

        self._base_url = base_url.rstrip("/")
        self._request_timeout_sec = request_timeout_sec
        self._health_timeout_sec = min(HEALTH_TIMEOUT_SEC, request_timeout_sec)
        # 빈 프록시 표를 준 opener: d1 서버는 LAN(Orin)이라 프록시가 필요 없다.
        # 개발 머신의 HTTP_PROXY 가 상태 JSON 을 밖으로 내보내는 것을 막는다(T-01-04-03)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:
        return f"SystemOneClient(base_url={self._base_url!r}, request_timeout_sec={self._request_timeout_sec})"

    def is_ready(self) -> bool:
        """GET /health 가 200 이면 True. 모든 실패가 LLMBackendError 로 바뀌므로 예외를 던지지 않는다."""
        request = self._build_request(HEALTH_PATH, payload=None)

        try:
            self._fetch_json(request, self._health_timeout_sec)
        except LLMBackendError:
            return False

        return True

    def probe_rtt_ms(self) -> float:
        """추론 없는 GET /health 왕복 시간(ms) — 네트워크 몫 추정치(rtt_method health_probe).

        실패하면 ask_choice 와 같은 LLMBackendError 계열을 그대로 올린다 — 01-05 D1Selector 가 원인별로
        timeout·unavailable·bad_response 를 가르고, 실패한 probe 뒤에는 POST 를 보내지 않는다(WR-02).
        """
        request = self._build_request(HEALTH_PATH, payload=None)

        started_sec = time.monotonic()
        self._fetch_json(request, self._health_timeout_sec)
        elapsed_sec = time.monotonic() - started_sec

        return elapsed_sec * MS_PER_SEC

    def measure_rtt_ms(self) -> float | None:
        """probe_rtt_ms 와 같되 실패하면 None 을 돌려준다(원인이 필요 없는 진단·라이브 테스트용)."""
        try:
            return self.probe_rtt_ms()
        except LLMBackendError:
            return None

    def ask_choice(self, state_text: str, question_id: str, instructions: str, criteria: dict[str, str]) -> ChoiceReply:
        """choice 질문 1개를 보내 검증된 ChoiceReply 를 돌려준다. 보기는 criteria 의 키다(D-02).

        total_ms 는 HTTP 왕복과 JSON 디코드만 포함하고, 응답 검증 시간은 포함하지 않는다.
        """
        payload = build_systemone_payload(state_text, question_id, instructions, criteria)
        request = self._build_request(SYSTEMONE_PATH, payload)

        started_sec = time.monotonic()
        body = self._fetch_json(request, self._request_timeout_sec)
        total_ms = (time.monotonic() - started_sec) * MS_PER_SEC

        return parse_choice_response(body, question_id, tuple(criteria), total_ms)

    def _build_request(self, path: str, payload: dict | None) -> urllib.request.Request:
        url = f"{self._base_url}{path}"

        if payload is None:
            return urllib.request.Request(url, method="GET")

        data = json.dumps(payload, separators=REQUEST_JSON_SEPARATORS).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        return urllib.request.Request(url, data=data, headers=headers, method="POST")

    def _fetch_json(self, request: urllib.request.Request, timeout_sec: float) -> dict:
        """요청을 보내 JSON 객체를 돌려준다. 모든 실패를 LLMBackendError 계열로 바꾼다.

        except 순서가 의미를 가진다 — HTTPError 는 URLError 의 하위 클래스라 먼저 잡는다.
        """
        target = f"{self._base_url}{urllib.parse.urlsplit(request.full_url).path}"

        try:
            with self._opener.open(request, timeout=timeout_sec) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_body) > MAX_RESPONSE_BYTES:
                raise LLMResponseError(f"response body exceeds {MAX_RESPONSE_BYTES} bytes from {target}")
            body = json.loads(raw_body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise LLMUnavailableError(f"server is loading the model (503) at {target}") from error
            detail = self._read_error_detail(error)
            raise LLMResponseError(f"HTTP {error.code} from {target}: {detail}") from error
        # 연결 거부·DNS 실패·연결 단계 타임아웃
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise LLMTimeoutError(f"timed out connecting to {target}") from error
            raise LLMUnavailableError(f"cannot reach {self._base_url}: {error.reason}") from error
        # 본문을 읽는 도중의 타임아웃은 URLError 로 감싸지지 않는다
        except TimeoutError as error:
            raise LLMTimeoutError(f"timed out waiting for {target}") from error
        # 서버가 응답 도중 죽거나 연결을 끊음
        except ConnectionError as error:
            raise LLMUnavailableError(f"connection lost to {target}") from error
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


# --- 모듈 내부 도우미 ---


def _validate_choice_values(choice: str, probabilities: dict, confidence: object) -> None:
    """확률·확신도 범위, 합 1, choice 가 확률표의 argmax 인지 확인한다."""
    bad_probability_keys = sorted(str(key) for key, value in probabilities.items() if not _is_unit_interval_number(value))
    if bad_probability_keys:
        raise LLMResponseError(f"probabilities must be numbers in [0, 1], bad keys: {bad_probability_keys}")
    if not _is_unit_interval_number(confidence):
        raise LLMResponseError(f"confidence must be a number in [0, 1], got {confidence!r}")

    probability_sum = math.fsum(probabilities.values())
    if abs(probability_sum - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise LLMResponseError(f"probabilities must sum to 1, got {probability_sum}")

    if choice not in probabilities:
        raise LLMResponseError(f"choice {choice!r} has no probability")
    if probabilities[choice] < max(probabilities.values()):
        raise LLMResponseError(f"choice {choice!r} is not the argmax of probabilities")


def _summarize_body(body: object) -> str:
    # NaN·직렬화 불가 값이 섞여도 오류 메시지 만들기에서 다시 터지지 않게 repr 로 대체한다
    try:
        summary = json.dumps(body, allow_nan=False)
    except (TypeError, ValueError):
        summary = repr(body)

    return summary[:SHAPE_SUMMARY_MAX_CHARS]


def _validate_base_url(base_url: str) -> None:
    """urllib 은 file:// 등도 열 수 있으므로 http/https + 호스트가 있는 URL 만 허용한다(T-01-04-01).

    설정 오타가 매 요청 unavailable("서버 다운")로 보이지 않게 생성 시점에 거른다(WR-06).
    공백은 strip 하지 않고 거부한다 — 환경 변수 값 정리는 factory 몫이다.
    """
    if base_url != base_url.strip():
        raise ValueError(f"base_url must not have leading or trailing whitespace, got {base_url!r}")

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ALLOWED_URL_SCHEMES or not parts.hostname:
        raise ValueError(f"base_url must be an http(s) URL with a host, got {base_url!r}")
    # 포트가 숫자가 아니거나 범위 밖이면 port 속성 접근에서 ValueError 가 난다
    try:
        parts.port
    except ValueError as error:
        raise ValueError(f"base_url has an invalid port: {base_url!r}") from error


def _validate_timeout(request_timeout_sec: float) -> None:
    """urlopen 의 기본 timeout 은 무한 대기라, 유한한 양수만 받는다(T-01-04-04)."""
    if not _is_finite_number(request_timeout_sec) or request_timeout_sec <= 0:
        raise ValueError(f"request_timeout_sec must be a finite number > 0, got {request_timeout_sec!r}")


def _is_finite_number(value: object) -> bool:
    # bool 은 int 의 하위 클래스라 따로 걸러낸다
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_unit_interval_number(value: object) -> bool:
    return _is_finite_number(value) and 0.0 <= value <= 1.0


def _is_token_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
