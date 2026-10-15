"""Offline tests of the server-side negotiation flow of the ran-energy-saving demo.

Drives ``execute_server_flow`` with the real A2A-T facades, the scripted mock LLM and a real
a2a-sdk ``EventQueue``: round 1 (missing region) answers SUBMITTED + INPUT_REQUIRED carrying the
Negotiation-T information propose metadata, round 2 (continuation) streams the UC1 result and
closes with COMPLETED, and a failure without slot-named errors keeps the plain REJECTED branch.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy, QueueShutDown
from a2a.types import Role, SendMessageRequest, Task, TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from a2a_t.client.a2at_client import A2ATClient
from a2a_t.core.errors.exceptions import ContentValidationError, SlotValidationError
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    TEMPLATE_URI_METADATA_KEY,
    MetadataContent,
    NegotiationContext,
    NegotiationPerformative,
)
from a2a_t.core.standard_templates import INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI
from a2a_t.negotiation.content.enums import NegotiationConclusion
from a2a_t.negotiation.content.models import (
    InformationEndingContent,
    NegotiationEndingData,
    NegotiationItem,
)
from a2a_t.server.a2at_server import A2ATServer
from google.protobuf.json_format import MessageToDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from client import scenario_data
from client.scenario_data import (
    ENERGY_SAVING_TEMPLATE_URI,
    build_prompt_input,
    negotiation_answers,
    negotiation_filled_params,
)
from common import mock_llm
from common.llm_logger import install_llm_logger
from server.constants_data import (
    COMPLETED_MESSAGE,
    INPUT_REQUIRED_MESSAGE,
    NEGOTIATION_RELATIONSHIP,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    get_energy_saving_progress_steps,
)
from server.server_flow import (
    _emit_negotiation_propose,
    _missing_item,
    _missing_items_from_errors,
    execute_server_flow,
)
from server.task_schema import TASK_PARAM_SCHEMA

#: An offline ``.env`` (no API key) written per test; the mock force-installs against it.
_OFFLINE_ENV_TEXT = "A2AT_LANGUAGE={language}\nA2AT_LLM_PROVIDER=openai\nA2AT_LLM_MODEL=mock-llm\nA2AT_LLM_API_KEY=\n"


def _write_offline_env(directory: Path, language: str) -> Path:
    """Write the offline ``.env`` of one language and return its path."""
    env_path = directory / ".env"
    env_path.write_text(_OFFLINE_ENV_TEXT.format(language=language), encoding="utf-8")
    return env_path


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force-install the scripted mock LLM (en-US) for every test, regardless of any real ``.env``."""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.delenv(scenario_data.SCENARIO_ENV_KEY, raising=False)
    env_path = _write_offline_env(tmp_path, "en-US")
    mock_llm.install_mock_llm(env_path=env_path)
    install_llm_logger(role="test", verbose=False)
    return env_path


async def _no_sleep(_seconds: float) -> None:
    """Stand-in for ``asyncio.sleep`` so the streamed progress steps run without pacing."""
    return None


def _build_request_context(
    *,
    metadata: dict[str, object],
    requested_extensions: set[str],
) -> RequestContext:
    """Build one RequestContext the way the a2a-sdk request handler does for a send-message call."""
    return RequestContext(
        call_context=ServerCallContext(requested_extensions=requested_extensions),
        request=_build_send_message_request(metadata=metadata),
    )


def _build_send_message_request(metadata: dict[str, object]) -> SendMessageRequest:
    """Build one SendMessageRequest whose message metadata carries the given map."""
    request = SendMessageRequest()
    request.message.message_id = str(uuid.uuid4())
    request.message.role = Role.ROLE_USER
    request.message.parts.add(text="create the RAN energy saving task")
    for key, value in metadata.items():
        request.message.metadata[key] = value
    return request


async def _run_flow(
    *,
    metadata: dict[str, object],
    prompt_server: object,
    requested_extensions: set[str] | None = None,
) -> list[object]:
    """Run one ``execute_server_flow`` round on a real EventQueue and return the drained events."""
    queue = EventQueueLegacy()
    request_context = _build_request_context(
        metadata=metadata,
        requested_extensions=requested_extensions if requested_extensions is not None else {TASK_T_EXTENSION_URI},
    )
    await execute_server_flow(
        request_context=request_context,
        event_queue=queue,
        prompt_server=prompt_server,
        sleep_fn=_no_sleep,
    )
    return await _drain_events(queue)


async def _drain_events(queue: EventQueueLegacy) -> list[object]:
    """Close the queue and collect every enqueued event, in order, until the queue shuts down.

    ``close()`` waits for the queue to drain (one ``task_done`` per dequeued event), so it runs
    as a background task while the events are collected.
    """
    closer = asyncio.create_task(queue.close())
    events: list[object] = []
    while True:
        try:
            events.append(await queue.dequeue_event())
            queue.task_done()
        except QueueShutDown:
            break
    await closer
    return events


def _event_state(event: object) -> TaskState | None:
    """Return the task state carried by a Task / TaskStatusUpdateEvent, ``None`` for other events."""
    status = getattr(event, "status", None)
    if status is None:
        return None
    return getattr(status, "state", None)


def _event_states(events: list[object]) -> list[TaskState]:
    """Return the ordered task states of the status-bearing events of one drained stream."""
    return [state for state in (_event_state(event) for event in events) if state is not None]


def _message_metadata(event: object) -> dict[str, object]:
    """Return the metadata map of one event's status message as a plain dict."""
    status = getattr(event, "status", None)
    assert status is not None and status.message is not None
    return MessageToDict(status.message.metadata)


def _missing_region_prompt(client: A2ATClient, env_path: Path) -> MetadataContent:
    """Generate the round-1 Task-T prompt (missing region) through the real client facade."""
    return client.generate_task_prompt_from_text(
        build_prompt_input(env_path=env_path, scenario=scenario_data.SCENARIO_NEGOTIATION),
        ENERGY_SAVING_TEMPLATE_URI,
    )


def _run_two_round_flow(env_path: Path) -> tuple[list[object], list[object], dict[str, object]]:
    """Drive the two negotiation rounds (missing region, then the filled continuation).

    Returns the round-1 events, the round-2 events and the round-1 propose context as a dict.
    """
    client = A2ATClient(env_path=env_path)
    server = A2ATServer(env_path=env_path)

    # Round 1: the client submits the missing-region prompt, the server proposes.
    round1_prompt = _missing_region_prompt(client, env_path)
    round1_events = asyncio.run(
        _run_flow(
            metadata={
                round1_prompt.extension_uri: round1_prompt.prompt_text,
                TEMPLATE_URI_METADATA_KEY: ENERGY_SAVING_TEMPLATE_URI,
            },
            prompt_server=server,
        )
    )
    propose_context = dict(_message_metadata(round1_events[-1])[NEGOTIATION_CONTEXT_METADATA_KEY])

    # Round 2: the client answers with the accept prompt and the filled Task-T prompt.
    accept_context = NegotiationContext(
        id=str(propose_context["id"]),
        round=int(propose_context["round"]) + 1,
        max_rounds=int(propose_context["maxRounds"]),
        performative=NegotiationPerformative.ACCEPT,
    )
    accept = client.generate_negotiation_accept_prompt_from_data(
        NegotiationEndingData(
            context=accept_context,
            content=InformationEndingContent(
                conclusion=NegotiationConclusion.ACCEPT,
                items=[
                    NegotiationItem(name=name, value=value)
                    for name, value in negotiation_answers(env_path=env_path).items()
                ],
            ),
        ),
        INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI,
    )
    filled_prompt = client.generate_task_prompt_from_data_with_schema(
        negotiation_filled_params(env_path=env_path), TASK_PARAM_SCHEMA, ENERGY_SAVING_TEMPLATE_URI
    )
    round2_events = asyncio.run(
        _run_flow(
            metadata={
                TASK_T_EXTENSION_URI: filled_prompt.prompt_text,
                NEGOTIATION_T_EXTENSION_URI: accept.prompt_text,
                TEMPLATE_URI_METADATA_KEY: ENERGY_SAVING_TEMPLATE_URI,
                NEGOTIATION_CONTEXT_METADATA_KEY: {
                    "id": accept_context.id,
                    "round": accept_context.round,
                    "maxRounds": accept_context.max_rounds,
                    "performative": accept_context.performative.value,
                },
            },
            prompt_server=server,
            requested_extensions={TASK_T_EXTENSION_URI, NEGOTIATION_T_EXTENSION_URI},
        )
    )
    return round1_events, round2_events, propose_context


class _SlotlessRejectingPromptServer:
    """Prompt-server stub whose content validation fails without slot-named errors."""

    def validate_task_prompt_and_data_filling(self, *, prompt: str, schema: object, template_uri: object) -> object:
        raise ContentValidationError(
            "llm.invocation_failed",
            errors=[SlotValidationError(slot_name=None, code="llm.invocation_failed", message="mock llm failure")],
        )


# --- round 1: missing params -> Negotiation-T propose on INPUT_REQUIRED ---------------------


def test_round_1_missing_region_answers_input_required_carrying_the_propose(_offline_env: Path) -> None:
    mock_llm.reset_call_log()
    client = A2ATClient(env_path=_offline_env)
    prompt = _missing_region_prompt(client, _offline_env)

    events = asyncio.run(
        _run_flow(
            metadata={
                prompt.extension_uri: prompt.prompt_text,
                TEMPLATE_URI_METADATA_KEY: ENERGY_SAVING_TEMPLATE_URI,
            },
            prompt_server=A2ATServer(env_path=_offline_env),
        )
    )

    assert _event_states(events) == [TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_INPUT_REQUIRED]
    input_required = events[-1]
    assert isinstance(input_required, TaskStatusUpdateEvent)
    # The status text part stays a short human line; the propose travels in the metadata.
    assert input_required.status.message.parts[0].text == INPUT_REQUIRED_MESSAGE
    metadata = _message_metadata(input_required)
    assert NEGOTIATION_T_EXTENSION_URI in metadata
    assert metadata[TEMPLATE_URI_METADATA_KEY] == "Negotiation-T/information-negotiation/propose/v1"
    context = metadata[NEGOTIATION_CONTEXT_METADATA_KEY]
    assert uuid.UUID(str(context["id"]))  # a fresh negotiation session id
    assert context["round"] == 1
    assert context["maxRounds"] == NegotiationContext.DEFAULT_MAX_ROUNDS
    assert context["performative"] == "PROPOSE"
    # The propose text asks for the missing region slot with the schema description fragment.
    propose_text = str(metadata[NEGOTIATION_T_EXTENSION_URI])
    assert "region" in propose_text
    assert "Coverage region" in propose_text


# --- round 2: continuation -> validate -> progress -> artifact -> COMPLETED -----------------


def test_round_2_continuation_streams_the_uc1_result_and_completes(_offline_env: Path) -> None:
    mock_llm.reset_call_log()
    _, round2_events, _ = _run_two_round_flow(_offline_env)

    expected_steps = get_energy_saving_progress_steps()
    assert _event_states(round2_events) == [TaskState.TASK_STATE_WORKING] * len(expected_steps) + [
        TaskState.TASK_STATE_COMPLETED
    ]
    # No SUBMITTED event on the continuation round.
    assert not any(isinstance(event, Task) for event in round2_events)
    # One artifact event, arriving between the last WORKING step and COMPLETED.
    artifacts = [event for event in round2_events if isinstance(event, TaskArtifactUpdateEvent)]
    assert len(artifacts) == 1
    artifact_index = round2_events.index(artifacts[0])
    assert _event_state(round2_events[artifact_index - 1]) == TaskState.TASK_STATE_WORKING
    assert _event_state(round2_events[artifact_index + 1]) == TaskState.TASK_STATE_COMPLETED
    assert artifacts[0].artifact.name == "energy saving intent report"
    completed = round2_events[-1]
    assert isinstance(completed, TaskStatusUpdateEvent)
    assert completed.status.message.parts[0].text == COMPLETED_MESSAGE


def test_two_round_sequence_pins_the_mock_call_log(_offline_env: Path) -> None:
    mock_llm.reset_call_log()

    _run_two_round_flow(_offline_env)

    # Round 1: extraction of the missing input + its failing content validation; round 2: the
    # from-data filled-params extraction (no scenario input) + its passing content validation.
    assert mock_llm.llm_calls == [
        mock_llm.STAGE_SLOT_EXTRACTION_MISSING,
        mock_llm.STAGE_CONTENT_VALIDATION_MISSING,
        mock_llm.STAGE_SLOT_EXTRACTION,
        mock_llm.STAGE_CONTENT_VALIDATION,
    ]


# --- default flow: success stays long-running, slotless failure rejects ---------------------


def test_default_success_streams_the_uc1_result_and_stays_long_running(_offline_env: Path) -> None:
    """The default (non-negotiation) success path keeps the UC1 semantics: no terminal state."""
    mock_llm.reset_call_log()
    client = A2ATClient(env_path=_offline_env)
    prompt = client.generate_task_prompt_from_text(
        build_prompt_input(env_path=_offline_env, scenario=scenario_data.SCENARIO_DEFAULT),
        ENERGY_SAVING_TEMPLATE_URI,
    )

    events = asyncio.run(
        _run_flow(
            metadata={
                prompt.extension_uri: prompt.prompt_text,
                TEMPLATE_URI_METADATA_KEY: ENERGY_SAVING_TEMPLATE_URI,
            },
            prompt_server=A2ATServer(env_path=_offline_env),
        )
    )

    expected_steps = get_energy_saving_progress_steps()
    assert _event_states(events) == [TaskState.TASK_STATE_SUBMITTED] + [TaskState.TASK_STATE_WORKING] * len(
        expected_steps
    )
    # The stream ends on the artifact event; no terminal status update is emitted.
    assert isinstance(events[-1], TaskArtifactUpdateEvent)
    assert mock_llm.llm_calls == [mock_llm.STAGE_SLOT_EXTRACTION, mock_llm.STAGE_CONTENT_VALIDATION]


def test_default_failure_without_slot_errors_answers_rejected(_offline_env: Path) -> None:
    events = asyncio.run(
        _run_flow(metadata={TASK_T_EXTENSION_URI: "any prompt text"}, prompt_server=_SlotlessRejectingPromptServer())
    )

    assert _event_states(events) == [TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_REJECTED]
    rejected = events[-1]
    assert isinstance(rejected, TaskStatusUpdateEvent)
    assert "Prompt validation failed" in rejected.status.message.parts[0].text
    # No Negotiation-T propose metadata travels on the plain rejection.
    assert NEGOTIATION_T_EXTENSION_URI not in _message_metadata(rejected)


# --- missing-item building and the relationship rule ----------------------------------------


def test_missing_items_are_built_from_slot_named_errors_only() -> None:
    errors = [
        SlotValidationError(slot_name="region", code="content.param_missing", message="m1"),
        SlotValidationError(slot_name=None, code="llm.invocation_failed", message="m2"),
    ]

    items = _missing_items_from_errors(errors)

    assert [(item.name, item.value) for item in items] == [
        (
            "region",
            "Please provide region: Coverage region (task object) of the energy-saving task; "
            "a concrete physical area name..",
        )
    ]
    assert _missing_items_from_errors(None) == []
    assert (
        _missing_items_from_errors([SlotValidationError(slot_name=None, code="llm.invocation_failed", message="m")])
        == []
    )


def test_propose_carries_the_relationship_only_for_multiple_items(_offline_env: Path) -> None:
    async def _emit(items: list[NegotiationItem]) -> list[object]:
        queue = EventQueueLegacy()
        request = _build_send_message_request(metadata={TASK_T_EXTENSION_URI: "prompt"})
        request_context = RequestContext(
            call_context=ServerCallContext(requested_extensions={TASK_T_EXTENSION_URI}),
            request=request,
        )
        await _emit_negotiation_propose(
            request_context=request_context,
            event_queue=queue,
            prompt_server=A2ATServer(env_path=_offline_env),
            missing_items=items,
            task_id=request_context.task_id or "",
            context_id=request_context.context_id or "",
            log_sink=None,
        )
        return await _drain_events(queue)

    single = asyncio.run(_emit([_missing_item("region")]))
    multiple = asyncio.run(_emit([_missing_item("region"), _missing_item("energySavingGoal")]))

    assert _event_states(single) == [TaskState.TASK_STATE_INPUT_REQUIRED]
    assert _event_states(multiple) == [TaskState.TASK_STATE_INPUT_REQUIRED]
    single_text = str(_message_metadata(single[-1])[NEGOTIATION_T_EXTENSION_URI])
    multiple_text = str(_message_metadata(multiple[-1])[NEGOTIATION_T_EXTENSION_URI])
    assert NEGOTIATION_RELATIONSHIP not in single_text
    assert NEGOTIATION_RELATIONSHIP in multiple_text
