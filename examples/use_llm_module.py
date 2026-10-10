"""외부 프로젝트가 llm_module 을 쓰는 최소 예. 공개 파사드만 import 한다 (SC7, IF-06, D-03).

상태 JSON dict 하나를 process_state 에 넣고, 판단(DECISION)과 track ID 를 인용한 보고(REPORT)를 출력한다.

실행 예 (WavePilot 루트에서):
    PYTHONPATH=. uv run python examples/use_llm_module.py
    PYTHONPATH=. uv run python examples/use_llm_module.py --backend llama_server
"""

import argparse
import json
import os
import sys
from pathlib import Path

from llm_module import LLMBackendError, LLMModuleConfig, create_llm_module

DEFAULT_BACKEND_URL = "http://127.0.0.1:8080"
DEFAULT_READY_TIMEOUT_SEC = 120.0  # 모델 로딩이 수십 초 걸린다
EXIT_INVALID_STATE = 1
EXIT_BACKEND_UNAVAILABLE = 2

# 상태 JSON v1 예시: 감시 위치 상공 40 m 에서 호버 중인 드론 1대와 track 3개
SAMPLE_STATE = {
    "schema_version": 1,
    "time_sec": 24.0,
    "drone": {
        "position_ned_m": {"north": 0.0, "east": 0.0, "down": -40.0},
        "altitude_m": 40.0,
        "attitude_deg": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "gimbal_deg": {"pitch": -55.0, "yaw": 0.0},
        "battery_pct": 87.5,
        "flight_mode": "hover",
    },
    "tracks": [
        {
            "track_id": "T1",
            "class": "car",
            "position_ned_m": {"north": 32.4, "east": -6.1},
            "first_seen_sec": 3.0,
            "last_seen_sec": 24.0,
            "observation_count": 180,
            "mean_confidence": 0.86,
            "is_active": True,
        },
        {
            "track_id": "T2",
            "class": "person",
            "position_ned_m": {"north": 27.9, "east": 4.3},
            "first_seen_sec": 10.5,
            "last_seen_sec": 24.0,
            "observation_count": 95,
            "mean_confidence": 0.71,
            "is_active": True,
        },
        {
            "track_id": "T3",
            "class": "truck",
            "position_ned_m": {"north": 41.0, "east": 12.7},
            "first_seen_sec": 1.0,
            "last_seen_sec": 15.2,
            "observation_count": 120,
            "mean_confidence": 0.9,
            "is_active": False,
        },
    ],
    "counts_by_class": {"car": 1, "person": 1, "truck": 1},
    "omitted_track_count": 0,
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feed a state JSON to llm_module and print its decision and report.")
    parser.add_argument("--backend", choices=["fake", "llama_server"], default="fake", help="report LLM backend")
    parser.add_argument(
        "--backend-url", default=os.environ.get("LLAMA_SERVER_URL", DEFAULT_BACKEND_URL), help="llama-server base URL"
    )
    parser.add_argument(
        "--ready-timeout-sec", type=float, default=DEFAULT_READY_TIMEOUT_SEC,
        help="how long to wait for llama-server to become ready",
    )
    parser.add_argument("--state", type=Path, help="state JSON v1 file (default: built-in sample)")
    parser.add_argument("--json", action="store_true", help="print the raw report payload as JSON")
    return parser.parse_args(argv)


def load_state(path: Path | None) -> dict:
    if path is None:
        return SAMPLE_STATE

    return json.loads(path.read_text(encoding="utf-8"))


def create_module(args: argparse.Namespace):
    config = LLMModuleConfig(backend=args.backend, backend_url=args.backend_url)
    module = create_llm_module(config)

    if args.backend == "llama_server":
        module.wait_until_ready(args.ready_timeout_sec)

    return module


def print_output(payload: dict) -> None:
    # 내부 dataclass 속성명에 기대지 않도록 공개 payload dict 만 읽는다. 태그 모양은 운용자 콘솔과 같다
    decision = payload["decision"]
    print(f"DECISION verdict={decision['verdict']} reason={decision['reason']}")
    for answer in decision["answers"]:
        print(f"ANSWER {answer['question_id']}={answer['value']} confidence={answer['confidence']}")

    report = payload["report"]
    if report is not None:
        for sentence in report["sentences"]:
            print(f"REPORT   [{','.join(sentence['track_ids'])}] ({sentence['kind']}) {sentence['text']}")
        for flag in report["flags"]:
            print(f"FLAG {flag}")

    if payload["report_error"]:
        print(f"REPORT_ERROR {payload['report_error']}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    try:
        state = load_state(args.state)
    except (OSError, ValueError) as error:
        print(f"could not read state file {args.state}: {error}", file=sys.stderr)
        return EXIT_INVALID_STATE

    try:
        module = create_module(args)
    except LLMBackendError as error:
        print(f"llama-server not ready at {args.backend_url}: {error}", file=sys.stderr)
        print("start it with: scripts/run_llama_server_mac.sh", file=sys.stderr)
        return EXIT_BACKEND_UNAVAILABLE

    # 상태 dict 검증 실패는 ValueError 로 올라온다 — 외부 입력이므로 traceback 대신 한 줄로 알린다
    try:
        output = module.process_state(state)
    except ValueError as error:
        print(f"invalid state JSON: {error}", file=sys.stderr)
        return EXIT_INVALID_STATE

    payload = output.to_report_payload()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_output(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
