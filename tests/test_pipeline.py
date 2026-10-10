"""DronePipeline 소켓 없는 테스트 (MOCK-01, RPT-01, COMM-01).

InMemoryLink 가 CommLink 자리를 채운다. 보낸 메시지를 목록에 쌓고, 미리 넣어 둔 수신 메시지를
앞에서부터 꺼내 준다. 테스트 파일은 경계 규칙 대상이 아니므로 mocks·perception·llm_module 파사드를
여기서 직접 조립한다(실행 코드에서는 drone/run_scenario.py 만 이 일을 한다).
"""

import logging
import re
from pathlib import Path

import pytest

from drone.pipeline import DronePipeline, PipelineConfig, RunSummary
from interfaces.comm import MESSAGE_TYPES, LinkStatus, Message
from interfaces.flight_control import ReturnToHome
from interfaces.geometry import DEFAULT_CAMERA
from llm_module import LLMModuleConfig, create_llm_module
from mocks.clock import SimClock
from mocks.detection_source import ScenarioDetectionSource
from mocks.flight_control import MockFlightControl
from mocks.scenario import load_scenario
from perception.tracking import TrackAggregator

SENT_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$")
TRACK_LABEL_PATTERN = re.compile(r"^T\d+$")
CHECKPOINT_FRAME_COUNT = 401  # 40.0 s × 10 fps + t = 0 프레임
CHECKPOINT_TELEMETRY_COUNT = 41  # t = 0, 1, ..., 40
CHECKPOINT_REPORT_COUNT = 5  # t = 8, 16, 24, 32, 40
COMMAND_SENT_AT = "2026-10-10T12:00:00.000+09:00"


class InMemoryLink:
    """CommLink 를 만족하는 메모리 링크. receive 는 timeout 을 무시하고 바로 돌려준다."""

    def __init__(
        self, inbound: list[Message] | None = None, fail_after_send_count: int | None = None
    ) -> None:
        self.sent: list[Message] = []
        self.is_closed = False
        self._inbound = list(inbound or [])
        self._fail_after_send_count = fail_after_send_count

    def send(self, message: Message) -> None:
        is_failing = self._fail_after_send_count is not None and len(self.sent) >= self._fail_after_send_count
        if is_failing:
            raise ConnectionError("memory link broken")

        self.sent.append(message)

    def receive(self, timeout_sec: float | None = None) -> Message | None:
        if not self._inbound:
            return None
        return self._inbound.pop(0)

    def link_status(self) -> LinkStatus:
        return LinkStatus(connected=not self.is_closed, peer="memory", last_received_at=None)

    def close(self) -> None:
        self.is_closed = True


# --- 조립 도우미 ---


def _build_checkpoint_pipeline(
    scenarios_dir: Path, link: InMemoryLink
) -> tuple[DronePipeline, MockFlightControl]:
    clock = SimClock()
    flight_control = MockFlightControl(clock)
    scenario = load_scenario(scenarios_dir / "checkpoint.toml")
    detection_source = ScenarioDetectionSource(scenario, flight_control, DEFAULT_CAMERA, clock, fps=10.0)
    track_provider = TrackAggregator(DEFAULT_CAMERA)
    llm_module = create_llm_module(LLMModuleConfig(backend="fake"))

    pipeline = DronePipeline(
        detection_source, flight_control, track_provider, llm_module, link, config=PipelineConfig(), pace=None
    )
    return pipeline, flight_control


@pytest.fixture
def checkpoint_pipeline(scenarios_dir: Path):
    """호출할 때마다 새 조립을 돌려주는 팩토리 — 결정론 테스트가 두 번 돌린다."""

    def build(link: InMemoryLink) -> tuple[DronePipeline, MockFlightControl]:
        return _build_checkpoint_pipeline(scenarios_dir, link)

    return build


def _get_payloads(link: InMemoryLink, message_type: str) -> list[dict]:
    return [message.payload for message in link.sent if message.type == message_type]


def _get_report_sentences(link: InMemoryLink) -> list[dict]:
    return [
        sentence
        for payload in _get_payloads(link, "report")
        if payload["report"] is not None
        for sentence in payload["report"]["sentences"]
    ]


def _make_command_message(payload: dict) -> Message:
    return Message(type="command", seq=0, sent_at=COMMAND_SENT_AT, payload=payload)


def _run_checkpoint(checkpoint_pipeline, link: InMemoryLink) -> tuple[RunSummary, MockFlightControl]:
    pipeline, flight_control = checkpoint_pipeline(link)
    return pipeline.run(), flight_control


# --- 전체 실행 ---


def test_checkpoint_run_sends_messages_on_sim_schedule(checkpoint_pipeline) -> None:
    link = InMemoryLink()

    summary, _ = _run_checkpoint(checkpoint_pipeline, link)

    assert summary.frame_count == CHECKPOINT_FRAME_COUNT
    assert summary.final_time_sec == 40.0
    assert summary.telemetry_count == CHECKPOINT_TELEMETRY_COUNT
    assert summary.state_count == CHECKPOINT_REPORT_COUNT
    assert summary.report_count == CHECKPOINT_REPORT_COUNT
    assert summary.link_lost is False
    assert link.is_closed

    telemetry_times = [payload["time_sec"] for payload in _get_payloads(link, "telemetry")]
    assert telemetry_times == [float(second) for second in range(41)]
    state_times = [payload["time_sec"] for payload in _get_payloads(link, "state")]
    assert state_times == [8.0, 16.0, 24.0, 32.0, 40.0]


def test_sent_messages_follow_envelope_contract(checkpoint_pipeline) -> None:
    link = InMemoryLink()

    _run_checkpoint(checkpoint_pipeline, link)

    sent_types = {message.type for message in link.sent}
    assert sent_types <= set(MESSAGE_TYPES)
    assert "command" not in sent_types
    assert [message.seq for message in link.sent] == list(range(len(link.sent)))
    for message in link.sent:
        assert Message.from_dict(message.to_dict()) == message
        assert SENT_AT_PATTERN.match(message.sent_at), message.sent_at


def test_each_state_is_followed_by_its_report(checkpoint_pipeline) -> None:
    link = InMemoryLink()

    _run_checkpoint(checkpoint_pipeline, link)

    for index, message in enumerate(link.sent):
        if message.type != "state":
            continue
        report_message = link.sent[index + 1]
        assert report_message.type == "report"
        assert {"schema_version", "drone", "tracks"} <= set(message.payload)
        assert report_message.payload["decision"]["verdict"] in {"act", "escalate"}
        assert report_message.payload["time_sec"] == message.payload["time_sec"]


def test_report_sentences_cite_track_ids_from_preceding_state(checkpoint_pipeline) -> None:
    link = InMemoryLink()

    _run_checkpoint(checkpoint_pipeline, link)

    cited_sentence_count = 0
    for index, message in enumerate(link.sent):
        if message.type != "report" or message.payload["report"] is None:
            continue
        state_labels = {track["track_id"] for track in link.sent[index - 1].payload["tracks"]}
        for sentence in message.payload["report"]["sentences"]:
            for label in sentence["track_ids"]:
                assert TRACK_LABEL_PATTERN.match(label)
                assert label in state_labels
            cited_sentence_count += bool(sentence["track_ids"])

    assert cited_sentence_count >= 1


def test_speed_zero_run_is_deterministic(checkpoint_pipeline) -> None:
    first_link = InMemoryLink()
    second_link = InMemoryLink()

    _run_checkpoint(checkpoint_pipeline, first_link)
    _run_checkpoint(checkpoint_pipeline, second_link)

    assert _get_payloads(first_link, "telemetry") == _get_payloads(second_link, "telemetry")
    assert _get_payloads(first_link, "state") == _get_payloads(second_link, "state")
    assert _get_report_sentences(first_link) == _get_report_sentences(second_link)


# --- 운용자 명령 ---


def test_operator_command_reaches_flight_control(checkpoint_pipeline) -> None:
    link = InMemoryLink(inbound=[_make_command_message(ReturnToHome().to_dict())])

    summary, flight_control = _run_checkpoint(checkpoint_pipeline, link)

    assert len(flight_control.commands_received) == 1
    assert isinstance(flight_control.commands_received[0][1], ReturnToHome)
    assert summary.command_count == 1
    assert summary.telemetry_count == CHECKPOINT_TELEMETRY_COUNT
    telemetry_payloads = _get_payloads(link, "telemetry")
    assert all(payload["flight_mode"] == "returning" for payload in telemetry_payloads)


def test_invalid_command_is_rejected_and_run_continues(checkpoint_pipeline, caplog) -> None:
    link = InMemoryLink(inbound=[_make_command_message({"kind": "fly"})])

    with caplog.at_level(logging.WARNING):
        summary, flight_control = _run_checkpoint(checkpoint_pipeline, link)

    assert summary.rejected_command_count == 1
    assert summary.command_count == 0
    assert flight_control.commands_received == ()
    assert summary.frame_count == CHECKPOINT_FRAME_COUNT
    assert any("rejected command" in record.getMessage() for record in caplog.records)


def test_non_command_inbound_message_is_ignored(checkpoint_pipeline, caplog) -> None:
    inbound_report = Message(type="report", seq=0, sent_at=COMMAND_SENT_AT, payload={})
    link = InMemoryLink(inbound=[inbound_report])

    with caplog.at_level(logging.WARNING):
        summary, _ = _run_checkpoint(checkpoint_pipeline, link)

    assert summary.frame_count == CHECKPOINT_FRAME_COUNT
    assert summary.command_count == 0
    assert any("ignored inbound report message" in record.getMessage() for record in caplog.records)


# --- 링크 두절 ---


def test_link_loss_stops_run_without_raising(checkpoint_pipeline) -> None:
    link = InMemoryLink(fail_after_send_count=3)

    summary, _ = _run_checkpoint(checkpoint_pipeline, link)

    assert summary.link_lost is True
    assert summary.frame_count < CHECKPOINT_FRAME_COUNT
    assert len(link.sent) == 3
    assert link.is_closed
