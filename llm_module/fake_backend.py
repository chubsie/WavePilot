"""서버 없이 끝단 경로를 돌리는 결정적 LLMBackend (`--backend fake`).

상태 JSON(user 프롬프트)을 읽어 규칙으로 영어 보고 JSON 을 만든다. 시계·난수를 쓰지 않으므로
같은 입력은 항상 같은 출력이다(--speed 0 결정론, E2E 재현성).
문법 강제를 흉내 내어 스키마의 track_ids enum 안에 있는 라벨만 인용하고, 문장 수·길이·인용 수 상한을 지킨다.

tests/fakes.py 의 FakeLLMBackend(정해 둔 응답 큐 + 호출 기록, 테스트 전용)와는 역할이 다르다 —
이쪽은 데모·E2E 에서 실제 LLM 자리에 끼우는 공개 구현이다.
"""

import json

from llm_module.backend import Completion
from llm_module.report_schema import MAX_CITED_TRACKS, MAX_SENTENCE_CHARS, MAX_SENTENCES

CHARS_PER_TOKEN_ESTIMATE = 3  # 토큰 수는 로그용 추정치
MAX_TRACKS_WITH_POSITION = 3  # 한 클래스 묶음이 이 이하면 좌표까지 쓰고, 많으면 라벨만 쓴다
LOW_CONFIDENCE = 0.5  # 이보다 낮거나 관측이 적으면 "unconfirmed" 로 쓴다 (AI-SPEC L6 기준과 같게)
MIN_OBSERVATIONS = 3


class FakeReportBackend:
    def complete_json(
        self, system_prompt: str, user_prompt: str, schema: dict, max_tokens: int
    ) -> Completion:
        state = json.loads(user_prompt)
        allowed_labels = set(_get_schema_track_enum(schema))
        tracks = [track for track in state["tracks"] if track["track_id"] in allowed_labels]

        sentences = _describe_active_tracks(tracks, state["omitted_track_count"])
        sentences += _suggest_closer_look(tracks)
        sentences += _describe_inactive_tracks(tracks)
        if not sentences:
            # 스키마가 문장 1개 이상을 요구하므로, 쓸 내용이 없어도 근거 있는 한 문장은 남긴다
            first_label = sorted(allowed_labels)[0]
            sentences = [_make_sentence("observation", f"Track {first_label} is in the state.", [first_label])]

        text = json.dumps({"sentences": sentences[:MAX_SENTENCES]}, ensure_ascii=False)

        return Completion(
            text=text,
            finish_reason="stop",
            prompt_tokens=len(system_prompt + user_prompt) // CHARS_PER_TOKEN_ESTIMATE,
            completion_tokens=len(text) // CHARS_PER_TOKEN_ESTIMATE,
            latency_sec=0.0,
        )

    def is_ready(self) -> bool:
        return True


# --- 문장 구성 ---


def _describe_active_tracks(tracks: list[dict], omitted_track_count: int) -> list[dict]:
    active_tracks = [track for track in tracks if track["is_active"]]
    class_names = sorted({track["class"] for track in active_tracks})

    sentences = []
    for class_name in class_names:
        class_tracks = [track for track in active_tracks if track["class"] == class_name]
        for chunk in _split_into_chunks(class_tracks, MAX_CITED_TRACKS):
            sentences.append(_describe_class_group(class_name, chunk))

    if sentences and omitted_track_count > 0:
        first = sentences[0]
        first["text"] = _limit_text(f"{first['text']} {omitted_track_count} more tracks not listed.")

    return sentences


def _describe_class_group(class_name: str, tracks: list[dict]) -> dict:
    labels = [track["track_id"] for track in tracks]
    noun = class_name.replace("_", " ")
    head = f"{len(tracks)} {noun} track{'s' if len(tracks) > 1 else ''}"

    if len(tracks) <= MAX_TRACKS_WITH_POSITION:
        positions = "; ".join(_format_position(track) for track in tracks)
        text = f"{head}: {positions}."
    else:
        text = f"{head}: {', '.join(labels)}."

    return _make_sentence("observation", _limit_text(text), labels)


def _suggest_closer_look(tracks: list[dict]) -> list[dict]:
    uncertain_tracks = [
        track for track in tracks
        if track["is_active"]
        and (track["mean_confidence"] < LOW_CONFIDENCE or track["observation_count"] < MIN_OBSERVATIONS)
    ][:MAX_CITED_TRACKS]
    if not uncertain_tracks:
        return []

    labels = [track["track_id"] for track in uncertain_tracks]
    class_names = " or ".join(sorted({track["class"].replace("_", " ") for track in uncertain_tracks}))
    text = (
        f"Possible {class_names}, unconfirmed: operator may look closer at "
        f"{', '.join(labels)} to confirm by observation."
    )

    return [_make_sentence("suggestion", _limit_text(text), labels)]


def _describe_inactive_tracks(tracks: list[dict]) -> list[dict]:
    inactive_tracks = [track for track in tracks if not track["is_active"]]
    return [
        _make_sentence(
            "observation",
            f"Last seen at {track['last_seen_sec']:.1f} s, not currently seen: {track['track_id']}.",
            [track["track_id"]],
        )
        for track in inactive_tracks
    ]


# --- 모듈 내부 도우미 ---


def _get_schema_track_enum(schema: dict) -> list[str]:
    return schema["properties"]["sentences"]["items"]["properties"]["track_ids"]["items"]["enum"]


def _format_position(track: dict) -> str:
    position = track["position_ned_m"]
    return f"{track['track_id']} at {position['north']:.1f} m N, {position['east']:.1f} m E"


def _split_into_chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[start:start + size] for start in range(0, len(items), size)]


def _limit_text(text: str) -> str:
    if len(text) <= MAX_SENTENCE_CHARS:
        return text
    return text[: MAX_SENTENCE_CHARS - 3].rstrip() + "..."


def _make_sentence(kind: str, text: str, labels: list[str]) -> dict:
    return {"kind": kind, "text": text, "track_ids": labels}
