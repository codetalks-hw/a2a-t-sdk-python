"""Tests for the multi-round client flow over SSE and webhook transports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from a2a.client.client import ClientCallContext
from a2a.types import Message, Role, SendMessageRequest, StreamResponse
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
    MetadataContent,
    NegotiationContext,
)
from a2a_t.core.standard_templates import (
    FEASIBILITY_NEGOTIATION_PROPOSE_URI,
    INFORMATION_NEGOTIATION_PROPOSE_URI,
    NEGOTIATION_ABORT_URI,
    TARGET_NEGOTIATION_PROPOSE_URI,
)
from google.protobuf.json_format import MessageToDict
from protocol.client_flow import (
    TASK_REQUEST_LABEL,
    ClientFlowResult,
    SseClientTransport,
    WebhookClientTransport,
    normalize_webhook_payload,
    run_client_flow,
)
from protocol.decision_provider import ScriptedDecisionProvider
from protocol.messages import END_MESSAGE_TEXT, SAMPLE_PROPOSAL_METADATA_KEY
from shared.config import SampleConfig
from shared.models import NegotiationDecision
from shared.scenario import (
    FINAL_STATE_NEGOTIATION_ABORTED,
    FINAL_STATE_NEGOTIATION_ENDED,
    FINAL_STATE_TASK_COMPLETED,
    SCENARIO_ABORT_SPEC,
    SCENARIO_FEASIBILITY_SPEC,
    SCENARIO_INFORMATION_SPEC,
    SCENARIO_REJECT_SPEC,
    SCENARIO_TARGET_SPEC,
    Scenario,
)

CONTEXT_ID = "ctx-client-flow"

_PROPOSE_URIS: dict[str, str] = {
    "information": INFORMATION_NEGOTIATION_PROPOSE_URI,
    "target": TARGET_NEGOTIATION_PROPOSE_URI,
    "feasibility": FEASIBILITY_NEGOTIATION_PROPOSE_URI,
}


@dataclass(frozen=True)
class FakePromptClient:
    """Deterministic SDK client double recording every generation call."""

    task_prompts: list[str] = field(default_factory=list)
    accept_texts: list[str] = field(default_factory=list)
    reject_texts: list[str] = field(default_factory=list)
    abort_texts: list[str] = field(default_factory=list)
    propose_texts: list[str] = field(default_factory=list)
    refill_data: list[dict[str, object]] = field(default_factory=list)

    def generate_task_prompt_from_text(self, text: str, template_uri: str) -> MetadataContent:
        self.task_prompts.append(text)
        return MetadataContent(
            template_uri=template_uri,
            prompt_text=f"Task-T prompt for: {text}",
            extension_uri=TASK_T_EXTENSION_URI,
        )

    def generate_task_prompt_from_data_with_schema(
        self,
        data: Mapping[str, object],
        schema: Mapping[str, object],
        template_uri: str,
    ) -> MetadataContent:
        del schema
        self.refill_data.append(dict(data))
        return MetadataContent(
            template_uri=template_uri,
            prompt_text=f"Refilled Task-T prompt: {json.dumps(dict(data), sort_keys=True)}",
            extension_uri=TASK_T_EXTENSION_URI,
        )

    def generate_negotiation_accept_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        self.accept_texts.append(text or "")
        return _negotiation_content(f"Accept: {text}", context, template_uri)

    def generate_negotiation_reject_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        self.reject_texts.append(text or "")
        return _negotiation_content(f"Reject: {text}", context, template_uri)

    def generate_negotiation_abort_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        self.abort_texts.append(text or "")
        return _negotiation_content(f"Abort: {text}", context, template_uri)

    def generate_negotiation_propose_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent:
        self.propose_texts.append(text or "")
        return _negotiation_content(f"Propose: {text}", context, template_uri)


def _negotiation_content(text: str, context: NegotiationContext, template_uri: str) -> MetadataContent:
    return MetadataContent(
        template_uri=template_uri,
        prompt_text=text,
        extension_uri=NEGOTIATION_T_EXTENSION_URI,
        negotiation_context=context,
    )


class ScriptedTransport:
    """Fake transport replaying one event batch per send and recording the requests."""

    def __init__(self, batches: list[list[dict[str, object]]]) -> None:
        self._batches = list(batches)
        self.requests: list[SendMessageRequest] = []
        self.call_contexts: list[ClientCallContext] = []

    async def send(
        self,
        request: SendMessageRequest,
        call_context: ClientCallContext,
    ) -> list[dict[str, object]]:
        self.requests.append(request)
        self.call_contexts.append(call_context)
        if not self._batches:
            raise AssertionError("The scripted transport ran out of event batches")
        return self._batches.pop(0)


def _negotiation_metadata(
    *,
    performative: str,
    round_number: int,
    template_uri: str,
    content: str = "negotiation content",
) -> dict[str, object]:
    return {
        NEGOTIATION_T_EXTENSION_URI: content,
        TEMPLATE_URI_METADATA_KEY: template_uri,
        NEGOTIATION_CONTEXT_METADATA_KEY: {
            "id": CONTEXT_ID,
            "round": round_number,
            "maxRounds": 3,
            "performative": performative,
        },
    }


def propose_event(kind: str, round_number: int, *, content: str | None = None) -> dict[str, object]:
    return {
        "kind": "message",
        "text": "Negotiation proposal",
        "context_id": CONTEXT_ID,
        "message_id": f"propose-{round_number}",
        "metadata": _negotiation_metadata(
            performative="PROPOSE",
            round_number=round_number,
            template_uri=_PROPOSE_URIS[kind],
            content=content or f"Server proposal for round {round_number}",
        ),
    }


def abort_event(round_number: int) -> dict[str, object]:
    return {
        "kind": "message",
        "text": "Negotiation aborted",
        "context_id": CONTEXT_ID,
        "message_id": f"abort-{round_number}",
        "metadata": _negotiation_metadata(
            performative="ABORT",
            round_number=round_number,
            template_uri=NEGOTIATION_ABORT_URI,
            content="The negotiation reached its round limit.",
        ),
    }


def end_event() -> dict[str, object]:
    return {
        "kind": "message",
        "text": END_MESSAGE_TEXT,
        "context_id": CONTEXT_ID,
        "message_id": "end",
    }


def artifact_event() -> dict[str, object]:
    return {
        "kind": "artifact",
        "task_id": "task-1",
        "context_id": CONTEXT_ID,
        "artifact_id": "artifact-1",
        "name": "energy saving intent report",
        "data": {"summary": "Plan applied", "estimated_saving_kwh": 12.5},
    }


def status_event(state: str) -> dict[str, object]:
    return {
        "kind": "status",
        "task_id": "task-1",
        "context_id": CONTEXT_ID,
        "state": state,
    }


def _task_completed_batch() -> list[dict[str, object]]:
    return [
        status_event("TASK_STATE_WORKING"),
        artifact_event(),
        status_event("TASK_STATE_COMPLETED"),
    ]


def _client_negotiation_steps(requests: list[SendMessageRequest]) -> list[tuple[str, int]]:
    steps: list[tuple[str, int]] = []
    for request in requests:
        metadata = MessageToDict(request.message.metadata)
        context = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
        if isinstance(context, Mapping):
            steps.append((str(context["performative"]), int(context["round"])))
    return steps


async def _run(
    scenario_spec: Scenario,
    batches: list[list[dict[str, object]]],
    decisions: tuple[NegotiationDecision, ...],
) -> tuple[ClientFlowResult, ScriptedTransport, FakePromptClient]:
    prompt_client = FakePromptClient()
    transport = ScriptedTransport(batches)
    user_request = scenario_spec.user_request["en-US"]
    result = await run_client_flow(
        prompt_client=prompt_client,
        transport=transport,
        user_request=user_request,
        provider=ScriptedDecisionProvider(decisions),
        language="en-US",
    )
    return result, transport, prompt_client


async def test_information_scenario_accepts_and_completes_a_task() -> None:
    result, transport, prompt_client = await _run(
        SCENARIO_INFORMATION_SPEC,
        [[propose_event("information", 1)], _task_completed_batch()],
        SCENARIO_INFORMATION_SPEC.scripted_client_decisions,
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert result.task_id == "task-1"
    assert result.report == {"summary": "Plan applied", "estimated_saving_kwh": 12.5}
    assert _client_negotiation_steps(transport.requests) == [("ACCEPT", 1)]
    assert prompt_client.refill_data[0]["region"] == "Songshanhu Administration Committee"
    accept_metadata = MessageToDict(transport.requests[1].message.metadata)
    assert accept_metadata[TASK_T_EXTENSION_URI].startswith("Refilled Task-T prompt:")
    assert accept_metadata[TEMPLATE_URI_METADATA_KEY] == "Negotiation-T/information-negotiation/accept-reject/v1"


async def test_target_scenario_counters_in_round_one_then_accepts_round_two() -> None:
    result, transport, prompt_client = await _run(
        SCENARIO_TARGET_SPEC,
        [[propose_event("target", 1)], [propose_event("target", 2)], _task_completed_batch()],
        SCENARIO_TARGET_SPEC.scripted_client_decisions,
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert _client_negotiation_steps(transport.requests) == [("PROPOSE", 1), ("ACCEPT", 2)]
    assert len(prompt_client.propose_texts) == 1
    assert len(prompt_client.accept_texts) == 1


async def test_feasibility_scenario_accepts_the_alternative() -> None:
    result, transport, prompt_client = await _run(
        SCENARIO_FEASIBILITY_SPEC,
        [[propose_event("feasibility", 1)], _task_completed_batch()],
        SCENARIO_FEASIBILITY_SPEC.scripted_client_decisions,
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert _client_negotiation_steps(transport.requests) == [("ACCEPT", 1)]
    assert prompt_client.refill_data[0]["siteList"] == "Station 1,Station 2"


async def test_reject_scenario_ends_without_a_task() -> None:
    result, transport, prompt_client = await _run(
        SCENARIO_REJECT_SPEC,
        [
            [
                propose_event(
                    "information",
                    1,
                    content="Please provide the following missing items: region.",
                )
            ],
            [end_event()],
        ],
        SCENARIO_REJECT_SPEC.scripted_client_decisions,
    )

    assert result.final_state == FINAL_STATE_NEGOTIATION_ENDED
    assert result.task_id is None
    assert _client_negotiation_steps(transport.requests) == [("REJECT", 1)]
    assert "region:" in prompt_client.reject_texts[0]
    assert prompt_client.refill_data == []


async def test_abort_scenario_keeps_countering_until_the_server_aborts() -> None:
    result, transport, prompt_client = await _run(
        SCENARIO_ABORT_SPEC,
        [
            [propose_event("target", 1)],
            [propose_event("target", 2)],
            [propose_event("target", 3)],
            [abort_event(3)],
        ],
        SCENARIO_ABORT_SPEC.scripted_client_decisions,
    )

    assert result.final_state == FINAL_STATE_NEGOTIATION_ABORTED
    assert result.task_id is None
    assert _client_negotiation_steps(transport.requests) == [
        ("PROPOSE", 1),
        ("PROPOSE", 2),
        ("PROPOSE", 3),
    ]
    assert len(prompt_client.propose_texts) == 3
    assert prompt_client.refill_data == []


async def test_counter_carries_the_sample_proposal() -> None:
    decision = NegotiationDecision(
        action="counter",
        rationale="Move the window.",
        counter_proposal="Use cell-001 00:00~06:00 at 30%.",
        counter_params={"cellId": "cell-001", "window": "00:00~06:00", "targetRate": "30"},
    )
    transport = ScriptedTransport([[propose_event("target", 1)], [abort_event(1)]])

    result = await run_client_flow(
        prompt_client=FakePromptClient(),
        transport=transport,
        user_request="request",
        provider=ScriptedDecisionProvider([decision]),
        language="en-US",
    )

    counter_metadata = MessageToDict(transport.requests[1].message.metadata)
    assert counter_metadata[SAMPLE_PROPOSAL_METADATA_KEY] == {
        "cellId": "cell-001",
        "window": "00:00~06:00",
        "targetRate": "30",
    }
    assert counter_metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["round"] == 1
    assert result.final_state == FINAL_STATE_NEGOTIATION_ABORTED


async def test_initial_request_matches_the_old_message_shape() -> None:
    _, transport, _ = await _run(
        SCENARIO_INFORMATION_SPEC,
        [[propose_event("information", 1)], _task_completed_batch()],
        SCENARIO_INFORMATION_SPEC.scripted_client_decisions,
    )

    request = transport.requests[0]
    metadata = MessageToDict(request.message.metadata)
    assert request.message.parts[0].text == TASK_REQUEST_LABEL
    assert metadata[TASK_T_EXTENSION_URI].startswith("Task-T prompt for:")
    assert metadata[TEMPLATE_URI_METADATA_KEY] == "Task-T/network-layer/ran-energy-saving/v1"
    service_parameters = transport.call_contexts[0].service_parameters
    assert service_parameters is not None
    assert service_parameters["A2A-Extensions"] == TASK_T_EXTENSION_URI


def test_webhook_payload_normalization_matches_sse_shape() -> None:
    message = Message(message_id="msg-1", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    message.parts.add(text="hello")
    message.metadata[NEGOTIATION_T_EXTENSION_URI] = "content"
    payload = MessageToDict(StreamResponse(message=message))

    event = normalize_webhook_payload(payload)

    assert event == {
        "kind": "message",
        "text": "hello",
        "context_id": CONTEXT_ID,
        "message_id": "msg-1",
        "metadata": {NEGOTIATION_T_EXTENSION_URI: "content"},
    }


class _FakeA2AClient:
    """Async-generator ``send_message`` double serving pre-built StreamResponse objects."""

    def __init__(self, responses: list[StreamResponse]) -> None:
        self._responses = responses

    async def send_message(
        self,
        request: SendMessageRequest,
        *,
        context: ClientCallContext,
    ) -> AsyncIterator[StreamResponse]:
        del request, context
        for response in self._responses:
            yield response


async def test_sse_transport_stops_at_the_first_message() -> None:
    message = Message(message_id="msg-1", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    message.parts.add(text=END_MESSAGE_TEXT)
    client = _FakeA2AClient([StreamResponse(message=message)])

    events = await SseClientTransport(client).send(SendMessageRequest(), ClientCallContext())

    assert [event["kind"] for event in events] == ["message"]


async def test_webhook_transport_returns_the_rpc_reply_without_draining_the_queue() -> None:
    message = Message(message_id="msg-1", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    message.parts.add(text="proposal")
    client = _FakeA2AClient([StreamResponse(message=message)])
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    queue.put_nowait({"message": MessageToDict(message)})

    events = await WebhookClientTransport(
        client,
        queue=queue,
        push_url="http://127.0.0.1:9000/webhook",
        push_token="token",
    ).send(SendMessageRequest(), ClientCallContext())

    assert [event["kind"] for event in events] == ["message"]
    assert queue.qsize() == 1


async def test_webhook_transport_consumes_pushed_events_until_a_terminal_state() -> None:
    client = _FakeA2AClient([])
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    for event in _task_completed_batch():
        queue.put_nowait(_to_webhook_payload(event))

    events = await WebhookClientTransport(
        client,
        queue=queue,
        push_url="http://127.0.0.1:9000/webhook",
        push_token="token",
        idle_timeout=0.5,
    ).send(SendMessageRequest(), ClientCallContext())

    assert [event["kind"] for event in events] == ["status", "artifact"]
    assert events[0]["state"] == "TASK_STATE_WORKING"
    assert queue.qsize() == 1  # the terminal event after the report artifact is not consumed


class _PerSendFakeA2AClient:
    """Fake a2a client yielding one response turn per ``send_message`` call."""

    def __init__(self, turns: list[list[StreamResponse]]) -> None:
        self._turns = list(turns)

    async def send_message(
        self,
        request: SendMessageRequest,
        *,
        context: ClientCallContext,
    ) -> AsyncIterator[StreamResponse]:
        del request, context
        for response in self._turns.pop(0):
            yield response


async def test_webhook_mode_accept_reports_task_completed_after_the_artifact() -> None:
    """The final state must come from the last phase, not from the earlier proposal message."""
    proposal = Message(message_id="propose-1", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    proposal.parts.add(text="proposal")
    proposal.metadata[NEGOTIATION_T_EXTENSION_URI] = "Please provide the region."
    proposal.metadata[TEMPLATE_URI_METADATA_KEY] = INFORMATION_NEGOTIATION_PROPOSE_URI
    proposal.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = {
        "id": CONTEXT_ID,
        "round": 1,
        "maxRounds": 3,
        "performative": "PROPOSE",
    }
    client = _PerSendFakeA2AClient([[StreamResponse(message=proposal)], []])
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    for event in (status_event("TASK_STATE_WORKING"), artifact_event()):
        queue.put_nowait(_to_webhook_payload(event))
    transport = WebhookClientTransport(
        client,
        queue=queue,
        push_url="http://127.0.0.1:9000/webhook",
        push_token="token",
        idle_timeout=0.5,
    )

    result = await run_client_flow(
        prompt_client=FakePromptClient(),
        transport=transport,
        user_request=SCENARIO_INFORMATION_SPEC.user_request["en-US"],
        provider=ScriptedDecisionProvider(SCENARIO_INFORMATION_SPEC.scripted_client_decisions),
        language="en-US",
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert result.task_id == "task-1"
    assert result.report is not None
    assert result.report["text"] == "report"


def _to_webhook_payload(event: dict[str, object]) -> dict[str, object]:
    """Convert one fake normalized event into a webhook JSON body via the protobuf types."""
    if event["kind"] == "status":
        from a2a.types import TaskStatus, TaskStatusUpdateEvent

        return MessageToDict(
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id=str(event["task_id"]),
                    context_id=str(event["context_id"]),
                    status=TaskStatus(state=str(event["state"])),
                )
            )
        )
    if event["kind"] == "artifact":
        from a2a.types import Artifact, TaskArtifactUpdateEvent

        artifact = Artifact(artifact_id=str(event["artifact_id"]), name=str(event["name"]))
        artifact.parts.add(text="report")
        return MessageToDict(
            StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    task_id=str(event["task_id"]),
                    context_id=str(event["context_id"]),
                    artifact=artifact,
                )
            )
        )
    raise AssertionError(f"Unsupported fake event: {event['kind']}")


async def test_real_sdk_client_runs_the_information_flow_offline(
    offline_config: SampleConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow works with the real A2ATClient and the scripted LLM (no network)."""
    from a2a_t.client.a2at_client import A2ATClient
    from protocol.server_app import prepare_sdk_env_path
    from shared.scripted_llm import register_scripted_provider

    register_scripted_provider()
    # The scripted provider resolves its language from the process environment (the demo launcher
    # sets it per process); the test pins it to the language of the offline configuration.
    monkeypatch.setenv("A2AT_LANGUAGE", offline_config.language)
    env_path: Path = prepare_sdk_env_path(offline_config)
    prompt_client = A2ATClient(env_path=env_path)
    transport = ScriptedTransport(
        [
            [propose_event("information", 1)],
            _task_completed_batch(),
        ]
    )

    result = await run_client_flow(
        prompt_client=prompt_client,
        transport=transport,
        user_request=SCENARIO_INFORMATION_SPEC.user_request["en-US"],
        provider=ScriptedDecisionProvider(SCENARIO_INFORMATION_SPEC.scripted_client_decisions),
        language="en-US",
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert _client_negotiation_steps(transport.requests) == [("ACCEPT", 1)]
    initial_metadata = MessageToDict(transport.requests[0].message.metadata)
    assert initial_metadata[TASK_T_EXTENSION_URI]
    accept_metadata = MessageToDict(transport.requests[1].message.metadata)
    assert accept_metadata[TASK_T_EXTENSION_URI]


async def test_event_sink_sees_every_event_and_the_decision_hook_the_proposal() -> None:
    seen: list[dict[str, object]] = []
    requested: list[tuple[str, int]] = []

    result = await run_client_flow(
        prompt_client=FakePromptClient(),
        transport=ScriptedTransport([[propose_event("information", 1)], _task_completed_batch()]),
        user_request="request",
        provider=ScriptedDecisionProvider(SCENARIO_INFORMATION_SPEC.scripted_client_decisions),
        language="en-US",
        event_sink=seen.append,
        on_decision_requested=lambda propose, session: requested.append((propose.kind, session.round)),
    )

    assert seen == result.events
    assert requested == [("information", 1)]


async def test_sse_transport_forwards_events_to_its_sink_as_they_arrive() -> None:
    proposal = Message(message_id="propose-1", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    proposal.parts.add(text="proposal")
    proposal.metadata[NEGOTIATION_T_EXTENSION_URI] = "Please provide the region."
    proposal.metadata[TEMPLATE_URI_METADATA_KEY] = INFORMATION_NEGOTIATION_PROPOSE_URI
    proposal.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = {
        "id": CONTEXT_ID,
        "round": 1,
        "maxRounds": 3,
        "performative": "PROPOSE",
    }
    end = Message(message_id="end", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    end.parts.add(text=END_MESSAGE_TEXT)
    seen: list[dict[str, object]] = []
    client = _PerSendFakeA2AClient([[StreamResponse(message=proposal)], [StreamResponse(message=end)]])
    transport = SseClientTransport(client)

    result = await run_client_flow(
        prompt_client=FakePromptClient(),
        transport=transport,
        user_request="request",
        provider=ScriptedDecisionProvider(SCENARIO_INFORMATION_SPEC.scripted_client_decisions),
        language="en-US",
        event_sink=seen.append,
    )

    assert seen == result.events
    assert [event["kind"] for event in seen] == ["message", "message"]
    assert result.final_state == FINAL_STATE_NEGOTIATION_ENDED
