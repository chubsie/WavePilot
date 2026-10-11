"""d1 서버 스모크용 /v1/systemone 요청 본문을 코드로 만든다 (IN-07).

RUNBOOK 6·7단계의 지연·토큰 실측은 실제로 보내는 요청과 같은 본문이어야 의미가 있다. 그래서 본문을 손으로
쓰지 않고, D1Selector 가 요청을 만드는 순서 그대로 기존 공개 함수만 조합한다:
1. 재생 픽스처를 읽는다(replay.load_ticks).
2. 고른 줄까지 SelectionTrigger 를 돌려 그 줄의 실제 사건을 얻는다. 사건이 없는 줄은 실제로 묻지 않으므로 거부한다.
3. filter_options → build_rule_mode_question 으로 질문을, serialize_request_state 로 상태 문자열을 만든다.
4. build_systemone_payload 로 본문을 만들고 SystemOneClient 와 같은 구분자로 직렬화한다 — 같은 바이트가 나간다.

기본 줄: target_found_mode_chain.jsonl 13번째 줄 (t=12.0s mode_ended — observe·T2 timeout, 활성 person track 3개).
대상이 있고 T2 가 조준 가능 거리 안이라 현재 보기 필터로 6개가 모두 남는다 — criteria 가 가장 길어 입력 토큰이
가장 많은 쪽의 요청이다. 보기 필터 규칙이 바뀌면 이 줄의 보기도 바뀔 수 있다(본문은 늘 필터 결과를 따른다).

실행 예시 (WavePilot 루트에서):
    python -m modules.decision.sample_request > /tmp/d1_smoke.json
    python -m modules.decision.sample_request --fixture modules/decision/fixtures/patrol_no_target.jsonl --line 26
"""

import argparse
import json
import sys
from pathlib import Path

from modules.decision.interface.state import SelectionEvent
from modules.decision.options import filter_options
from modules.decision.params import ParamDefaults
from modules.decision.prompt import (
    QUESTION_ID_RULE_MODE,
    build_rule_mode_question,
    serialize_request_state,
)
from modules.decision.replay import ReplayTick, load_ticks
from modules.decision.systemone_client import (
    REQUEST_JSON_SEPARATORS,
    build_systemone_payload,
)
from modules.decision.trigger import SelectionTrigger

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "target_found_mode_chain.jsonl"
DEFAULT_LINE_NO = 13

EXIT_OK = 0
EXIT_USAGE = 2


def get_event_tick(ticks: tuple[ReplayTick, ...], line_no: int) -> tuple[ReplayTick, SelectionEvent]:
    """line_no 줄의 틱과, 처음부터 그 줄까지 판정기를 돌렸을 때 그 줄에서 난 사건을 돌려준다."""
    trigger = SelectionTrigger()
    for tick in ticks:
        event = trigger.poll(tick.state.time_sec, tick.state, tick.mode_result)
        if tick.line_no != line_no:
            continue
        if event is None:
            raise ValueError(f"line {line_no} has no selection event, so the selector would not ask; pick an event line")
        return tick, event

    raise ValueError(f"line {line_no} is not a tick line in the fixture")


def build_sample_request(fixture_path: Path, line_no: int, defaults: ParamDefaults) -> dict:
    tick, event = get_event_tick(load_ticks(fixture_path), line_no)

    options = filter_options(tick.state, event, defaults.approach_stop_distance_m)
    instructions, criteria = build_rule_mode_question(options)
    state_text = serialize_request_state(tick.state, event)

    return build_systemone_payload(state_text, QUESTION_ID_RULE_MODE, instructions, criteria)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.decision.sample_request",
        description="Print the /v1/systemone request body the d1 selector would send for one fixture line.",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="path to a tick fixture JSONL file")
    parser.add_argument(
        "--line", type=int, default=DEFAULT_LINE_NO,
        help=f"1-based fixture line that has a selection event (default {DEFAULT_LINE_NO})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        payload = build_sample_request(args.fixture, args.line, ParamDefaults())
    except (OSError, ValueError) as error:
        print(f"sample_request: {error}", file=sys.stderr)
        return EXIT_USAGE

    print(json.dumps(payload, separators=REQUEST_JSON_SEPARATORS, allow_nan=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
