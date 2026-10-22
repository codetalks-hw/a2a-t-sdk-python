"""Tests for the negotiation executor and its ADK integration seams."""

from __future__ import annotations

from typing import cast

import pytest
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext, UnauthenticatedUser
from a2a.server.events import Event as A2AEvent
from a2a.server.events.event_queue import EventQueue
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a_t.core.errors.catalog import ErrorCatalog
from a2a_t.core.errors.exceptions import ContentValidationError, SlotValidationError
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.executor_context import ExecutorContext
from google.adk.events.event import Event as AdkEvent
from google.adk.runners import Runner
from google.genai import types
from protocol.messages import SAMPLE_PROPOSAL_METADATA_KEY, TASK_TEMPLATE_URI
from protocol.negotiation_gate import GateOutcome, NegotiationContentServer, NegotiationGate
from protocol.server_app import build_agent_card, build_registration_payload
from protocol.server_executor import (
    STATE_NEGOTIATION_CONTEXT,
    STATE_SAMPLE_PROPOSAL,
    STATE_TASK_PROMPT,
    STATE_TASK_TEMPLATE_URI,
    NegotiationAgentExecutor,
    NegotiationArtifactInterceptor,
    build_negotiation_executor,
    build_request_converter,
)
from shared import ran_data
from shared.adk_fake_model import build_fake_model
from shared.config import SampleConfig

CONTEXT_ID = "ctx-executor"
TASK_PROMPT = "Create a RAN energy saving task in the Songshanhu area."


class RecordingEventQueue(EventQueue):
    """Collect events enqueued by an executor."""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def enqueue_event(self, event: A2AEvent) -> None:
        self.events.append(event)


class RecordingExecutor(AgentExecutor):
    """Inner executor test double recording delegation."""

    def __init__(self) -> None:
        self.contexts: list[RequestContext] = []
        self.cancelled: list[RequestContext] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        del event_queue
        self.contexts.append(context)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        del event_queue
        self.cancelled.append(context)


class FixedGate:
    """Gate test double returning one fixed outcome."""

    def __init__(self, outcome: GateOutcome) -> None:
        self.outcome = outcome
        self.contexts: list[RequestContext] = []

    async def handle(self, context: RequestContext) -> GateOutcome:
        self.contexts.append(context)
        return self.outcome


class RejectingPromptServer:
    """Prompt-server double whose Task-T validation fails with an unmapped error code."""

    def validate_task_prompt_and_data_filling(
        self, prompt: str, schema: object, template_uri: str
    ) -> object:
        del prompt, schema, template_uri
        raise ContentValidationError(
            ErrorCatalog.CONTENT_SEMANTIC_CONFLICT,
            {"section_label": "task_context", "reason": "rejected"},
            errors=[SlotValidationError(slot_name="region", code="content.format_error", message="bad")],
        )


def _make_context(
    *,
    task_prompt: str = TASK_PROMPT,
    negotiation: dict[str, object] | None = None,
    sample: dict[str, object] | None = None,
    context_id: str = CONTEXT_ID,
    add_label_part: bool = True,
) -> RequestContext:
    message = Message(message_id="message-1", role=Role.ROLE_USER, context_id=context_id)
    if add_label_part:
        message.parts.add(text="short label")
    message.metadata[TASK_T_EXTENSION_URI] = task_prompt
    message.metadata[TEMPLATE_URI_METADATA_KEY] = TASK_TEMPLATE_URI
    if negotiation is not None:
        message.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = negotiation
    if sample is not None:
        message.metadata[SAMPLE_PROPOSAL_METADATA_KEY] = sample
    request = SendMessageRequest(message=message)
    call_context = ServerCallContext(
        state={},
        user=UnauthenticatedUser(),
        requested_extensions={TASK_T_EXTENSION_URI},
    )
    return RequestContext(call_context=call_context, request=request)


def _executor_context(context: RequestContext) -> ExecutorContext:
    return ExecutorContext(
        app_name="test-app",
        user_id="test-user",
        session_id=context.context_id or "",
        runner=cast(Runner, object()),
    )


def _text_artifact_event() -> TaskArtifactUpdateEvent:
    return TaskArtifactUpdateEvent(
        task_id="task-1",
        context_id=CONTEXT_ID,
        last_chunk=True,
        append=False,
        artifact=Artifact(artifact_id="artifact-1", parts=[Part(text="energy saving report")]),
    )


def _adk_text_event() -> AdkEvent:
    return AdkEvent(
        author="ran_energy_service_agent",
        content=types.Content(role="model", parts=[types.Part(text="energy saving report")]),
    )


def test_request_converter_reads_message_metadata_and_builds_the_agent_prompt() -> None:
    negotiation = {"id": CONTEXT_ID, "round": 2, "maxRounds": 3, "performative": "ACCEPT"}
    context = _make_context(negotiation=negotiation, sample={"cellId": "cell-001"})
    converter = build_request_converter(task_template_uri=TASK_TEMPLATE_URI)

    run_request = converter(context, cast(object, None))  # part converter is unused

    assert run_request.session_id == CONTEXT_ID
    assert run_request.user_id == f"A2A_USER_{CONTEXT_ID}"
    assert run_request.new_message is not None
    assert run_request.new_message.parts is not None
    assert run_request.new_message.parts[0].text == TASK_PROMPT
    assert run_request.state_delta is not None
    assert run_request.state_delta[STATE_TASK_PROMPT] == TASK_PROMPT
    assert run_request.state_delta[STATE_TASK_TEMPLATE_URI] == TASK_TEMPLATE_URI
    assert run_request.state_delta[STATE_NEGOTIATION_CONTEXT] == negotiation
    assert run_request.state_delta[STATE_SAMPLE_PROPOSAL] == {"cellId": "cell-001"}


def test_request_converter_rejects_a_request_without_a_prompt() -> None:
    context = _make_context(task_prompt="", add_label_part=False)
    converter = build_request_converter()

    with pytest.raises(ValueError, match="Task-T prompt"):
        converter(context, cast(object, None))


async def test_interceptor_stamps_task_t_metadata_on_the_report_artifact() -> None:
    context = _make_context(
        negotiation={"id": CONTEXT_ID, "round": 1, "maxRounds": 3, "performative": "PROPOSE"}
    )
    interceptor = NegotiationArtifactInterceptor(task_template_uri=TASK_TEMPLATE_URI)
    await interceptor.before_agent(context)

    event = _text_artifact_event()
    returned = await interceptor.after_event(
        _executor_context(context), event, _adk_text_event()
    )

    assert returned is event
    assert event.artifact.metadata[TASK_T_EXTENSION_URI] == "energy saving report"
    assert event.artifact.metadata[TEMPLATE_URI_METADATA_KEY] == TASK_TEMPLATE_URI
    assert event.artifact.metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["id"] == CONTEXT_ID
    assert event.artifact.metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["round"] == 1
    assert event.artifact.name == "energy saving intent report"


async def test_interceptor_leaves_tool_events_untouched() -> None:
    context = _make_context()
    interceptor = NegotiationArtifactInterceptor()
    await interceptor.before_agent(context)
    event = _text_artifact_event()
    tool_event = AdkEvent(
        author="ran_energy_service_agent",
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="get_cell_energy_profile"))],
        ),
    )

    await interceptor.after_event(_executor_context(context), event, tool_event)

    assert TASK_T_EXTENSION_URI not in event.artifact.metadata


async def test_interceptor_attaches_the_negotiation_context_to_the_terminal_status() -> None:
    context = _make_context(
        negotiation={"id": CONTEXT_ID, "round": 2, "maxRounds": 3, "performative": "ACCEPT"}
    )
    interceptor = NegotiationArtifactInterceptor()
    await interceptor.before_agent(context)
    final_event = TaskStatusUpdateEvent(
        task_id="task-1",
        context_id=CONTEXT_ID,
        status=TaskStatus(),
    )

    returned = await interceptor.after_agent(_executor_context(context), final_event)

    assert returned is final_event
    assert final_event.metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["round"] == 2
    assert interceptor._audit == {}


async def test_executor_short_circuits_without_calling_the_inner_executor() -> None:
    message = Message(message_id="propose", role=Role.ROLE_AGENT, context_id=CONTEXT_ID)
    message.parts.add(text="propose")
    gate = FixedGate(GateOutcome.short(message))
    inner = RecordingExecutor()
    executor = NegotiationAgentExecutor(
        gate=cast(NegotiationGate, gate), inner=inner
    )
    queue = RecordingEventQueue()

    await executor.execute(_make_context(), queue)

    assert queue.events == [message]
    assert inner.contexts == []


async def test_executor_delegates_the_gate_approved_request() -> None:
    context = _make_context()
    gate = FixedGate(GateOutcome.execution(context))
    inner = RecordingExecutor()
    executor = NegotiationAgentExecutor(
        gate=cast(NegotiationGate, gate), inner=inner
    )
    queue = RecordingEventQueue()

    await executor.execute(context, queue)

    assert queue.events == []
    assert inner.contexts == [context]


async def test_executor_enqueues_a_rejected_task_for_an_unmapped_validation_failure() -> None:
    server = cast(NegotiationContentServer, RejectingPromptServer())
    gate = NegotiationGate(server)
    inner = RecordingExecutor()
    executor = NegotiationAgentExecutor(gate=gate, inner=inner)
    context = _make_context()
    queue = RecordingEventQueue()

    await executor.execute(context, queue)

    assert len(queue.events) == 1
    task = queue.events[0]
    assert isinstance(task, Task)
    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert task.context_id == CONTEXT_ID
    assert task.status.message.parts[0].text
    assert inner.contexts == []


async def test_executor_cancel_delegates_to_the_inner_executor() -> None:
    context = _make_context()
    gate = FixedGate(GateOutcome.execution(context))
    inner = RecordingExecutor()
    executor = NegotiationAgentExecutor(
        gate=cast(NegotiationGate, gate), inner=inner
    )

    await executor.cancel(context, RecordingEventQueue())

    assert inner.cancelled == [context]


def test_build_negotiation_executor_wraps_the_adk_executor(offline_config: SampleConfig) -> None:
    gate = cast(NegotiationGate, FixedGate(GateOutcome.execution(_make_context())))

    executor = build_negotiation_executor(
        gate=gate,
        config=offline_config,
        model=build_fake_model(),
    )

    assert isinstance(executor, NegotiationAgentExecutor)
    assert isinstance(executor.inner, A2aAgentExecutor)
    assert executor.gate is gate


async def test_full_executor_runs_the_agent_and_stamps_the_report_artifact(
    offline_config: SampleConfig,
) -> None:
    ran_data.reset()
    context = _make_context(
        negotiation={"id": CONTEXT_ID, "round": 1, "maxRounds": 3, "performative": "ACCEPT"}
    )
    gate = FixedGate(GateOutcome.execution(context))
    executor = build_negotiation_executor(
        gate=cast(NegotiationGate, gate),
        config=offline_config,
        model=build_fake_model(),
    )
    queue = RecordingEventQueue()

    await executor.execute(context, queue)

    tasks = [event for event in queue.events if isinstance(event, Task)]
    artifacts = [event for event in queue.events if isinstance(event, TaskArtifactUpdateEvent)]
    final_statuses = [
        event
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and event.status.state == TaskState.TASK_STATE_COMPLETED
    ]
    reports = [
        event
        for event in artifacts
        if TASK_T_EXTENSION_URI in event.artifact.metadata
    ]

    assert len(tasks) == 1
    assert final_statuses
    assert len(reports) == 1
    report = reports[0]
    assert report.artifact.name == "energy saving intent report"
    assert report.artifact.metadata[TEMPLATE_URI_METADATA_KEY] == TASK_TEMPLATE_URI
    assert report.artifact.metadata[NEGOTIATION_CONTEXT_METADATA_KEY]["round"] == 1
    assert report.artifact.metadata[TASK_T_EXTENSION_URI]
    assert ran_data.list_plan_ids() == ("plan-cell-001-001",)
    ran_data.reset()


def test_agent_card_declares_the_task_and_negotiation_extensions() -> None:
    card = build_agent_card(host="127.0.0.1", port=8000)

    assert card.name
    assert card.supported_interfaces[0].url == "http://127.0.0.1:8000"
    assert str(card.supported_interfaces[0].protocol_binding) == "HTTP+JSON"
    extension_uris = {extension.uri for extension in card.capabilities.extensions}
    assert TASK_T_EXTENSION_URI in extension_uris
    assert NEGOTIATION_T_EXTENSION_URI in extension_uris


def test_registration_payload_is_a_registry_card_list() -> None:
    payload = build_registration_payload(build_agent_card(host="127.0.0.1", port=8000))

    cards = cast(list[dict[str, object]], payload["agentCards"])
    assert len(cards) == 1
    assert cards[0]["name"] == "RAN Domain Agent"
    assert "supportedInterfaces" in cards[0]
