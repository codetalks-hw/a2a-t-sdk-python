"""Offline tests of the client-side negotiation round-trip of the ran-energy-saving demo.

Drives ``run_client_flow`` against a scripted fake a2a client (real protobuf StreamResponse events)
and a fake prompt client: round 1 sends the Task-T prompt and receives the Negotiation-T propose on
INPUT_REQUIRED, round 2 re-sends the filled Task-T prompt together with the Negotiation-T accept and
consumes the UC1 result; the default path stays single-round and its stage logs are pinned.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from a2a.client.client import ClientCallContext
from a2a.types import (
    Artifact,
    Message,
    Role,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
    MetadataContent,
    NegotiationContext,
    NegotiationPerformative,
)
from a2a_t.core.standard_templates import (
    INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI,
    INFORMATION_NEGOTIATION_PROPOSE_URI,
)
from a2a_t.negotiation.content.enums import NegotiationConclusion
from a2a_t.negotiation.content.models import NegotiationEndingData
from google.protobuf.json_format import MessageToDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from client.client_flow import run_client_flow
from client.scenario_data import ENERGY_SAVING_TEMPLATE_URI
from common.sse_event_consumer import normalize_event

#: The round-1 task identity the scripted server stream answers with.
_TASK_ID = "task-round-1"
_CONTEXT_ID = "context-round-1"

#: Filled Task-T parameters and accept answers handed to the flow (negotiation round 2).
_NEGOTIATION_PARAMS: dict[str, object] = {
    "operationType": "create",
    "region": "Songshanhu Administration Committee",
    "energySavingGoal": "Maximize energy saving",
    "guaranteedThroughputTarget": "{00:00~07:00,2Mbps}",
}
_NEGOTIATION_ANSWERS: dict[str, str] = {"region": "Songshanhu Administration Committee"}


class _ScriptedA2AClient:
    """Fake a2a-sdk client: records every (request, context) and replays scripted streams."""

    def __init__(self, rounds: list[list[StreamResponse]]) -> None:
        self._rounds = rounds
        self.calls: list[tuple[SendMessageRequest, ClientCallContext | None]] = []

    async def send_message(
        self, request: SendMessageRequest, *, context: ClientCallContext | None = None
    ) -> AsyncIterator[StreamResponse]:
        self.calls.append((request, context))
        for event in self._rounds[len(self.calls) - 1]:
            yield event


class _ScriptedPromptClient:
    """Fake A2ATClient: deterministic MetadataContent results, recording every generation call."""

    def __init__(self) -> None:
        self.from_text_calls: list[tuple[str, str]] = []
        self.from_data_calls: list[tuple[dict[str, object], str]] = []
        self.accept_calls: list[NegotiationEndingData] = []

    def generate_task_prompt_from_text(self, text: str, template_uri: str) -> MetadataContent:
        self.from_text_calls.append((text, str(template_uri)))
        return MetadataContent(
            template_uri=str(template_uri),
            prompt_text=f"rendered prompt for: {text}",
            extension_uri=TASK_T_EXTENSION_URI,
        )

    def generate_task_prompt_from_data_with_schema(
        self, data: dict[str, object], schema: object, template_uri: str
    ) -> MetadataContent:
        self.from_data_calls.append((dict(data), str(template_uri)))
        return MetadataContent(
            template_uri=str(template_uri),
            prompt_text="filled task prompt",
            extension_uri=TASK_T_EXTENSION_URI,
        )

    def generate_negotiation_accept_prompt_from_data(
        self, data: NegotiationEndingData, template_uri: str
    ) -> MetadataContent:
        self.accept_calls.append(data)
        return MetadataContent(
            template_uri=str(template_uri),
            prompt_text="accept prompt",
            extension_uri=NEGOTIATION_T_EXTENSION_URI,
            negotiation_context=NegotiationContext(
                id=data.context.id,
                round=data.context.round,
                max_rounds=data.context.max_rounds,
                performative=NegotiationPerformative.ACCEPT,
            ),
        )


def _propose_metadata() -> dict[str, object]:
    """Build the round-1 propose metadata the server emits (``build_metadata_content`` shape)."""
    context = NegotiationContext(
        id=str(uuid.uuid4()),
        round=1,
        max_rounds=NegotiationContext.DEFAULT_MAX_ROUNDS,
        performative=NegotiationPerformative.PROPOSE,
    )
    propose = MetadataContent(
        template_uri=INFORMATION_NEGOTIATION_PROPOSE_URI,
        prompt_text="Please provide region: Coverage region of the energy-saving task.",
        extension_uri=NEGOTIATION_T_EXTENSION_URI,
        negotiation_context=context,
    )
    return propose.build_metadata_content()


def _agent_message(*, text: str, metadata: dict[str, object] | None = None) -> Message:
    """Build one agent status message with a text part and an optional metadata map."""
    message = Message(context_id=_CONTEXT_ID, task_id=_TASK_ID, role=Role.ROLE_AGENT)
    message.parts.add(text=text)
    for key, value in (metadata or {}).items():
        message.metadata[key] = value
    return message


def _status_update(*, state: TaskState, text: str = "", metadata: dict[str, object] | None = None) -> StreamResponse:
    """Build one streamed status update event (the wire shape the a2a-sdk client yields)."""
    return StreamResponse(
        status_update=TaskStatusUpdateEvent(
            task_id=_TASK_ID,
            context_id=_CONTEXT_ID,
            status=TaskStatus(state=state, message=_agent_message(text=text, metadata=metadata)),
        )
    )


def _task_event(state: TaskState) -> StreamResponse:
    """Build one streamed Task event carrying the task's current status."""
    return StreamResponse(task=Task(id=_TASK_ID, context_id=_CONTEXT_ID, status=TaskStatus(state=state)))


def _artifact_event() -> StreamResponse:
    """Build one streamed intent-report artifact event."""
    return StreamResponse(
        artifact_update=TaskArtifactUpdateEvent(
            task_id=_TASK_ID,
            context_id=_CONTEXT_ID,
            artifact=Artifact(artifact_id=str(uuid.uuid4()), name="energy saving intent report"),
            last_chunk=True,
        )
    )


def _round1_events(*, metadata: dict[str, object] | None = None) -> list[StreamResponse]:
    """Round-1 script: the SUBMITTED Task event, then INPUT_REQUIRED carrying the propose."""
    return [
        _task_event(TaskState.TASK_STATE_SUBMITTED),
        _status_update(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            text="Additional information is required to start the energy saving task",
            metadata=metadata,
        ),
    ]


def _round2_events() -> list[StreamResponse]:
    """Round-2 script: WORKING steps, the intent report artifact, then COMPLETED."""
    return [
        _status_update(state=TaskState.TASK_STATE_WORKING, text="progress step 1"),
        _status_update(state=TaskState.TASK_STATE_WORKING, text="progress step 2"),
        _artifact_event(),
        _status_update(state=TaskState.TASK_STATE_COMPLETED, text="Energy saving task completed"),
    ]


def _default_events() -> list[StreamResponse]:
    """Default-path script: SUBMITTED, one WORKING step, then the stream ends on the artifact."""
    return [
        _task_event(TaskState.TASK_STATE_SUBMITTED),
        _status_update(state=TaskState.TASK_STATE_WORKING, text="progress step 1"),
        _artifact_event(),
    ]


async def _run_flow(
    a2a_client: _ScriptedA2AClient,
    prompt_client: _ScriptedPromptClient,
    *,
    negotiation_params: dict[str, object] | None = None,
    negotiation_answers: dict[str, str] | None = None,
    log_sink: object | None = None,
) -> list[dict[str, object]]:
    """Run ``run_client_flow`` once against the scripted clients."""
    return await run_client_flow(
        prompt_client=prompt_client,
        a2a_client=a2a_client,
        initial_input={"scenario": "create ran energy saving task"},
        input_text="Create a RAN energy saving task to maximize energy saving.",
        negotiation_params=negotiation_params,
        negotiation_answers=negotiation_answers,
        log_sink=log_sink,
    )


def _event_states(events: list[dict[str, object]]) -> list[str]:
    """Return the ordered states of the status events of one normalized stream."""
    return [str(event["state"]) for event in events if event.get("kind") == "status"]


def _request_metadata(request: SendMessageRequest) -> dict[str, object]:
    """Return one request message's metadata as a plain dict."""
    return MessageToDict(request.message.metadata)


def _stage_names(logs: list[str]) -> list[str]:
    """Extract the ordered stage names from the ``[client] <stage>: <detail>`` log lines."""
    prefix = "[client] "
    return [line[len(prefix) :].split(":", 1)[0] for line in logs if line.startswith(prefix)]


# --- negotiation round trip: propose -> filled prompt + accept -> COMPLETED ------------------


def test_negotiation_round_trip_answers_the_propose_and_completes() -> None:
    propose_metadata = _propose_metadata()
    session_id = propose_metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["id"]
    prompt_client = _ScriptedPromptClient()
    a2a_client = _ScriptedA2AClient([_round1_events(metadata=propose_metadata), _round2_events()])

    events = asyncio.run(
        _run_flow(
            a2a_client,
            prompt_client,
            negotiation_params=dict(_NEGOTIATION_PARAMS),
            negotiation_answers=dict(_NEGOTIATION_ANSWERS),
        )
    )

    # Exactly two streaming sends: the round-1 task prompt, then the negotiation continuation.
    assert len(a2a_client.calls) == 2
    round1_request, round1_context = a2a_client.calls[0]
    assert round1_context is not None
    assert round1_context.service_parameters == {"A2A-Extensions": TASK_T_EXTENSION_URI}
    assert set(_request_metadata(round1_request)) == {TASK_T_EXTENSION_URI, TEMPLATE_URI_METADATA_KEY}

    round2_request, round2_context = a2a_client.calls[1]
    assert round2_request.message.task_id == _TASK_ID
    assert round2_request.message.context_id == _CONTEXT_ID
    assert round2_request.message.role == Role.ROLE_USER
    assert round2_request.message.parts[0].text == "create ran energy saving task"
    metadata = _request_metadata(round2_request)
    assert set(metadata) == {
        TASK_T_EXTENSION_URI,
        NEGOTIATION_T_EXTENSION_URI,
        TEMPLATE_URI_METADATA_KEY,
        NEGOTIATION_CONTEXT_METADATA_KEY,
    }
    assert metadata[TEMPLATE_URI_METADATA_KEY] == INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI
    context = metadata[NEGOTIATION_CONTEXT_METADATA_KEY]
    assert context["id"] == session_id
    assert context["round"] == 2
    assert context["maxRounds"] == NegotiationContext.DEFAULT_MAX_ROUNDS
    assert context["performative"] == "ACCEPT"

    # The A2A-Extensions header of round 2 carries both extension URIs.
    assert round2_context is not None
    header = str((round2_context.service_parameters or {}).get("A2A-Extensions", ""))
    assert {part.strip() for part in header.split(",") if part.strip()} == {
        TASK_T_EXTENSION_URI,
        NEGOTIATION_T_EXTENSION_URI,
    }

    # The filled Task-T prompt was regenerated from the supplied parameters and schema.
    assert prompt_client.from_data_calls == [(_NEGOTIATION_PARAMS, ENERGY_SAVING_TEMPLATE_URI)]
    # The accept carries conclusion ACCEPT, the advanced session context and the region answer.
    assert len(prompt_client.accept_calls) == 1
    accept_data = prompt_client.accept_calls[0]
    assert accept_data.content.conclusion == NegotiationConclusion.ACCEPT
    assert accept_data.context.id == session_id
    assert accept_data.context.round == 2
    assert accept_data.context.performative == NegotiationPerformative.ACCEPT
    assert [(item.name, item.value) for item in accept_data.content.items] == list(_NEGOTIATION_ANSWERS.items())

    # The consumed stream spans both rounds and closes with the COMPLETED state.
    assert _event_states(events) == [
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_WORKING",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
    ]
    assert any(event["kind"] == "artifact" for event in events)


def test_round2_reuses_the_task_and_context_ids_of_round_1() -> None:
    prompt_client = _ScriptedPromptClient()
    a2a_client = _ScriptedA2AClient([_round1_events(metadata=_propose_metadata()), _round2_events()])

    events = asyncio.run(
        _run_flow(
            a2a_client,
            prompt_client,
            negotiation_params=dict(_NEGOTIATION_PARAMS),
            negotiation_answers=dict(_NEGOTIATION_ANSWERS),
        )
    )

    input_required = [event for event in events if event.get("state") == "TASK_STATE_INPUT_REQUIRED"][0]
    assert input_required["context_id"] == _CONTEXT_ID
    assert input_required["metadata"][NEGOTIATION_T_EXTENSION_URI]
    # The round-2 request carries exactly the round-1 identity (task id from any round-1 event,
    # context id from the INPUT_REQUIRED status event).
    round2_request = a2a_client.calls[1][0]
    assert round2_request.message.task_id == input_required["task_id"]
    assert round2_request.message.context_id == input_required["context_id"]


# --- INPUT_REQUIRED without negotiation answers / default path -------------------------------


def test_input_required_without_answers_is_logged_unhandled_and_stays_on_round_1() -> None:
    prompt_client = _ScriptedPromptClient()
    a2a_client = _ScriptedA2AClient([_round1_events(metadata=_propose_metadata())])

    logs: list[str] = []
    events = asyncio.run(_run_flow(a2a_client, prompt_client, log_sink=logs.append))

    assert len(a2a_client.calls) == 1
    assert prompt_client.accept_calls == []
    assert prompt_client.from_data_calls == []
    assert _event_states(events) == ["TASK_STATE_SUBMITTED", "TASK_STATE_INPUT_REQUIRED"]
    assert any("negotiation-unhandled" in line for line in logs)


def test_default_path_without_input_required_sends_exactly_once_and_keeps_the_stage_logs() -> None:
    prompt_client = _ScriptedPromptClient()
    a2a_client = _ScriptedA2AClient([_default_events()])

    logs: list[str] = []
    events = asyncio.run(_run_flow(a2a_client, prompt_client, log_sink=logs.append))

    assert len(a2a_client.calls) == 1
    assert prompt_client.accept_calls == []
    assert _event_states(events) == ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"]
    assert any(event["kind"] == "artifact" for event in events)
    # The default path's stage logs are unchanged (byte-identical flow: no negotiation stages).
    assert _stage_names(logs) == [
        "scenario-data",
        "input-raw",
        "sdk-call",
        "sdk-output-prompt",
        "sdk-output-meta",
        "a2a-send-message",
        "a2a-event",
        "response-inbound",
        "a2a-event",
        "a2a-event",
        "stream-completed",
    ]


def test_negotiation_round_trip_logs_the_accept_and_round2_send() -> None:
    prompt_client = _ScriptedPromptClient()
    a2a_client = _ScriptedA2AClient([_round1_events(metadata=_propose_metadata()), _round2_events()])

    logs: list[str] = []
    asyncio.run(
        _run_flow(
            a2a_client,
            prompt_client,
            negotiation_params=dict(_NEGOTIATION_PARAMS),
            negotiation_answers=dict(_NEGOTIATION_ANSWERS),
            log_sink=logs.append,
        )
    )

    accept_logs = [line for line in logs if line.startswith("[client] negotiation-accept:")]
    assert len(accept_logs) == 1
    assert "Negotiation-T/information-negotiation/accept-reject/v1" in accept_logs[0]
    assert "('region', 'Songshanhu Administration Committee')" in accept_logs[0]
    round2_send_logs = [line for line in logs if line.startswith("[client] a2a-send-message: round=2")]
    assert len(round2_send_logs) == 1
    assert "header[A2A-Extensions]=" in round2_send_logs[0]


# --- sse_event_consumer: metadata surfacing and context_id -----------------------------------


def test_status_event_surfaces_the_message_metadata_and_context_id() -> None:
    event = normalize_event(
        _status_update(state=TaskState.TASK_STATE_INPUT_REQUIRED, text="need more", metadata={"key": "value"})
    )

    assert event["kind"] == "status"
    assert event["task_id"] == _TASK_ID
    assert event["context_id"] == _CONTEXT_ID
    assert event["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert event["text"] == "need more"
    assert event["metadata"] == {"key": "value"}


def test_status_event_without_metadata_keeps_the_old_shape() -> None:
    event = normalize_event(_status_update(state=TaskState.TASK_STATE_WORKING, text="working"))

    assert event == {
        "kind": "status",
        "task_id": _TASK_ID,
        "context_id": _CONTEXT_ID,
        "state": "TASK_STATE_WORKING",
        "text": "working",
    }
    assert "metadata" not in event


def test_task_event_gains_the_context_id() -> None:
    event = normalize_event(_task_event(TaskState.TASK_STATE_SUBMITTED))

    assert event == {
        "kind": "status",
        "task_id": _TASK_ID,
        "context_id": _CONTEXT_ID,
        "state": "TASK_STATE_SUBMITTED",
    }
