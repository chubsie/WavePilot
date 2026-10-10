"""임무 구조화 로그 — 모드 전환·선택·탐지·하트·복귀·안전·링크·임무 시작/끝을 시각과 함께 남긴다 (LOG-01).

한 번의 record 가 두 곳에 같은 내용을 남긴다.
- 파일: JSON Lines (한 줄 = {"t", "kind", ...필드}). 줄마다 flush 해 임무 중 프로세스가 죽어도 그때까지 남는다.
- 콘솔: "kind t=12.3 key=value ..." 한 줄. dict/list 값은 압축 JSON.

05-05 의 임무 루프가 kind 8종을 기록하고, 05-06 의 run_mission 이 default_log_path 로
logs/missions/<시작시각>.jsonl 파일을 연다. path=None 이면 파일 없이 메모리와 콘솔에만 남는다.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from types import TracebackType

KIND_MISSION = "mission"
KIND_MODE_TRANSITION = "mode_transition"
KIND_SELECTION = "selection"
KIND_DETECTION = "detection"
KIND_HEART = "heart"
KIND_RETURN = "return"
KIND_SAFETY = "safety"
KIND_LINK = "link"
LOG_KINDS = (
    KIND_MISSION,
    KIND_MODE_TRANSITION,
    KIND_SELECTION,
    KIND_DETECTION,
    KIND_HEART,
    KIND_RETURN,
    KIND_SAFETY,
    KIND_LINK,
)

DEFAULT_LOG_DIR = Path("logs") / "missions"

logger = logging.getLogger("modules.mission.mission_log")


class MissionLog:
    def __init__(self, path: Path | None = None, console: logging.Logger | None = None) -> None:
        self._console = console if console is not None else logger
        self._records: list[dict] = []

        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")

    @property
    def records(self) -> tuple[dict, ...]:
        return tuple(self._records)

    def record(self, kind: str, t: float, **fields) -> dict:
        if kind not in LOG_KINDS:
            raise ValueError(f"unknown log kind {kind!r}")

        # 쓰기 전에 직렬화한다 — 직렬화할 수 없는 값이면 반쪽 줄을 남기지 않고 여기서 TypeError
        entry = {"t": t, "kind": kind, **fields}
        line = json.dumps(entry, ensure_ascii=False)

        if self._file is not None:
            self._file.write(line + "\n")
            # 줄마다 flush — 임무 중 프로세스가 죽어도 그때까지의 기록이 파일에 남는다
            self._file.flush()

        field_text = "".join(f" {key}={_format_console_value(value)}" for key, value in fields.items())
        self._console.info("%s t=%.1f%s", kind, t, field_text)

        self._records.append(entry)

        return entry

    def close(self) -> None:
        """파일을 닫는다. 두 번 불러도 안전하고, 닫은 뒤 record 는 메모리·콘솔에만 남는다."""
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "MissionLog":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def default_log_path(log_dir: Path, started_at: datetime) -> Path:
    return log_dir / f"{started_at:%Y%m%d-%H%M%S}.jsonl"


def _format_console_value(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return str(value)
