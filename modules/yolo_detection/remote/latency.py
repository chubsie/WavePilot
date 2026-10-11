"""원격 YOLO 프레임 지연을 Orin 처리와 네트워크로 나누고 분포 표를 만든다 (SIM-05, D-04).

orin_ms 는 서버가 잰 server_ms.total, network_ms 는 클라이언트 왕복에서 그것을 뺀 나머지다(음수는 0).
표준 라이브러리만 쓴다 — client.py 와 bench.py 가 함께 쓰고, base 환경에서 import 할 수 있어야 한다.
modules.decision.replay 의 백분위 함수는 import 경계 때문에 쓰지 않고 따로 둔다.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

TABLE_PERCENTILES = (50, 95)


@dataclass(frozen=True, slots=True)
class FrameLatency:
    timestamp_sec: float
    total_ms: float  # 클라이언트가 잰 요청 + 응답 본문 읽기 왕복
    orin_ms: float  # 서버 server_ms.total
    network_ms: float  # max(0, total_ms - orin_ms)

    @classmethod
    def from_timing(cls, timestamp_sec: float, total_ms: float, orin_ms: float) -> "FrameLatency":
        # 두 시계가 달라 orin_ms 가 왕복보다 조금 클 수 있다 — 네트워크 몫은 음수가 되지 않게 자른다
        network_ms = max(0.0, total_ms - orin_ms)
        return cls(timestamp_sec=timestamp_sec, total_ms=total_ms, orin_ms=orin_ms, network_ms=network_ms)


def calculate_percentile(values: Sequence[float], q: float) -> float:
    """q 번째 백분위(0~100) — 정렬한 값 사이를 선형 보간한다."""
    if not values:
        raise ValueError("values must not be empty")
    if not 0 <= q <= 100:
        raise ValueError(f"q must be in [0, 100], got {q}")

    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower_index = math.floor(position)
    upper_index = math.ceil(position)

    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def format_latency_table(latencies: Sequence[FrameLatency], failures: int) -> list[str]:
    """`n=<n> failures=<f>` 머리줄 뒤에 total·orin·net 행마다 p50/p95/max(ms)를 붙인다. 표본이 없으면 머리줄만."""
    header = f"n={len(latencies)} failures={failures}"
    if not latencies:
        return [header]

    metric_values = {
        "total": [latency.total_ms for latency in latencies],
        "orin": [latency.orin_ms for latency in latencies],
        "net": [latency.network_ms for latency in latencies],
    }
    column_titles = [f"p{q}" for q in TABLE_PERCENTILES] + ["max"]
    lines = [header, f"{'ms':<6}" + "".join(f"{title:>10}" for title in column_titles)]

    for name, values in metric_values.items():
        cells = [calculate_percentile(values, q) for q in TABLE_PERCENTILES] + [max(values)]
        lines.append(f"{name:<6}" + "".join(f"{cell:>10.1f}" for cell in cells))

    return lines
