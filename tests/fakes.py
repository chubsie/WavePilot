"""테스트 공용 가짜 객체와 픽스처 로더.

FakeLLMBackend 는 정해 둔 응답(또는 예외)을 큐 순서대로 돌려주고 받은 인자를 모두 기록한다.
"스키마 enum = 입력 ID", "재시도 때 max_tokens 2배", "무트랙이면 호출 0회" 를 단언하는 데 쓴다.
llm_module.fake_backend.FakeReportBackend(상태를 읽어 결정적 보고를 만드는 공개 구현)와는 역할이 다르다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from interfaces.track import Track
from llm_module.backend import Completion

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "states"
CHARS_PER_TOKEN_ESTIMATE = 3  # 토큰 수는 의미 없는 값이라 문자 수로 대충 만든다


@dataclass(frozen=True, slots=True)
class FakeResponse:
    text: str
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class FakeCall:
    system_prompt: str
    user_prompt: str
    schema: dict
    max_tokens: int


@dataclass
class FakeLLMBackend:
    responses: list[FakeResponse | Exception]
    ready: bool = True
    calls: list[FakeCall] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete_json(
        self, system_prompt: str, user_prompt: str, schema: dict, max_tokens: int
    ) -> Completion:
        self.calls.append(FakeCall(system_prompt, user_prompt, schema, max_tokens))
        if not self.responses:
            raise AssertionError("FakeLLMBackend called more times than responses were queued")

        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response

        return Completion(
            text=response.text,
            finish_reason=response.finish_reason,
            prompt_tokens=len(system_prompt + user_prompt) // CHARS_PER_TOKEN_ESTIMATE,
            completion_tokens=len(response.text) // CHARS_PER_TOKEN_ESTIMATE,
            latency_sec=0.0,
        )

    def is_ready(self) -> bool:
        return self.ready


def load_state_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def make_track(
    track_id: int,
    class_name: str = "car",
    north_m: float = 30.0,
    east_m: float = 0.0,
    *,
    first_seen_sec: float = 0.0,
    last_seen_sec: float = 10.0,
    observation_count: int = 20,
    mean_confidence: float = 0.85,
    is_active: bool = True,
) -> Track:
    return Track(
        track_id=track_id,
        class_name=class_name,
        north_m=north_m,
        east_m=east_m,
        first_seen_sec=first_seen_sec,
        last_seen_sec=last_seen_sec,
        observation_count=observation_count,
        mean_confidence=mean_confidence,
        is_active=is_active,
    )
