from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence

from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.types import Task, TaskArtifactUpdateEvent, TaskState
from a2a_t.core.errors.exceptions import ContentValidationError, SlotValidationError
from a2a_t.core.metadata import NegotiationContext, NegotiationPerformative
from a2a_t.negotiation.content.models import (
    InformationProposeContent,
    NegotiationItem,
    NegotiationProposeData,
)
from common.a2a_adapter import build_metadata_artifact, build_status, emit_status_update
from common.logging_utils import format_payload_log, format_stage_log
from google.protobuf.json_format import MessageToDict

from server.constants_data import (
    ARTIFACT_SEND_INTERVAL_SECONDS,
    COMPLETED_MESSAGE,
    ENERGY_SAVING_TEMPLATE_URI,
    INPUT_REQUIRED_MESSAGE,
    NEGOTIATION_MISSING_ITEM_HINT,
    NEGOTIATION_PROPOSE_TEMPLATE_URI,
    NEGOTIATION_RELATIONSHIP,
    NEGOTIATION_T_EXTENSION_URI,
    SUBMITTED_MESSAGE,
    TASK_T_EXTENSION_URI,
    get_energy_saving_intent_report,
    get_energy_saving_progress_steps,
)
from server.task_schema import TASK_PARAM_SCHEMA

SleepFn = Callable[[float], Awaitable[None]]


def _require_task_extension(request_context: RequestContext) -> None:
    """Validate that the request carries the Task-T extension header.

    The A2A-Extensions header must contain the Task-T extension URI; raises ValueError if it is
    not present.
    """
    ext_set = request_context.call_context.requested_extensions
    if TASK_T_EXTENSION_URI not in ext_set:
        raise ValueError("a2a client extensions is not exist.")


def _extract_prompt_text(request_context: RequestContext) -> str:
    """Extract the prompt text from metadata under the Task-T extension URI."""
    if request_context.message is None or request_context.message.metadata is None:
        raise ValueError("Expected message metadata for Task-T prompt")
    metadata = MessageToDict(request_context.message.metadata)
    return str(metadata.get(TASK_T_EXTENSION_URI, ""))


def _is_negotiation_continuation(request_context: RequestContext) -> bool:
    """Report whether the inbound message metadata carries the Negotiation-T extension URI.

    The Negotiation-T URI in the message metadata marks the negotiation round-2 continuation: the
    client re-sends the Task-T prompt with the data the server's information propose asked for.
    """
    if request_context.message is None or request_context.message.metadata is None:
        return False
    metadata = MessageToDict(request_context.message.metadata)
    return NEGOTIATION_T_EXTENSION_URI in metadata


def _slot_description(schema: dict[str, object], slot_name: str) -> str | None:
    """Look up the description of one slot in the caller parameter schema (demo ``_slot_description``)."""
    properties = schema.get("properties")
    if isinstance(properties, dict):
        slot = properties.get(slot_name)
        if isinstance(slot, dict):
            description = slot.get("description")
            if isinstance(description, str) and description.strip():
                return description
    return None


def _missing_item(slot_name: str) -> NegotiationItem:
    """Build one missing-item entry from the schema description and the hint template."""
    description = _slot_description(TASK_PARAM_SCHEMA, slot_name)
    hint = NEGOTIATION_MISSING_ITEM_HINT.replace("{slot}", slot_name).replace("{description}", description or "")
    return NegotiationItem(name=slot_name, value=hint)


def _missing_items_from_errors(errors: Sequence[SlotValidationError] | None) -> list[NegotiationItem]:
    """Build the Negotiation-T missing-item list from the slot-named validation errors.

    Errors without a slot name (e.g. ``llm.invocation_failed``) contribute nothing; an empty
    result keeps the caller on the plain REJECTED branch (data-driven trigger, plan R3).
    """
    missing: list[NegotiationItem] = []
    for error in errors or []:
        slot_name = getattr(error, "slot_name", None)
        if isinstance(slot_name, str) and slot_name.strip():
            missing.append(_missing_item(slot_name))
    return missing


async def _emit_negotiation_propose(
    *,
    request_context: RequestContext,
    event_queue: EventQueue,
    prompt_server: object,
    missing_items: list[NegotiationItem],
    task_id: str,
    context_id: str,
    log_sink: object | None,
) -> None:
    """Generate the Negotiation-T information propose for the missing items and emit INPUT_REQUIRED.

    The propose travels on the INPUT_REQUIRED status event's message metadata
    (``propose.build_metadata_content()``); the executor then returns and the stream ends.
    """
    relationship = NEGOTIATION_RELATIONSHIP if len(missing_items) > 1 else None
    propose_context = NegotiationContext(
        id=str(uuid.uuid4()),
        round=1,
        max_rounds=NegotiationContext.DEFAULT_MAX_ROUNDS,
        performative=NegotiationPerformative.PROPOSE,
    )
    propose = prompt_server.generate_negotiation_propose_prompt_from_data(
        NegotiationProposeData(
            context=propose_context,
            content=InformationProposeContent(items=missing_items, relationship=relationship),
        ),
        NEGOTIATION_PROPOSE_TEMPLATE_URI,
    )
    if log_sink is not None:
        log_sink(
            format_stage_log(
                role="server",
                stage="negotiation-propose",
                detail=(
                    f"generated (round={propose_context.round}) "
                    f"items={[(item.name, item.value) for item in missing_items]}"
                ),
            )
        )
    await emit_status_update(
        request_context=request_context,
        event_queue=event_queue,
        context_id=context_id,
        task_id=task_id,
        state=TaskState.TASK_STATE_INPUT_REQUIRED,
        text=INPUT_REQUIRED_MESSAGE,
        metadata=propose.build_metadata_content(),
    )
    if log_sink is not None:
        log_sink(format_stage_log(role="server", stage="task-status", detail="TASK_STATE_INPUT_REQUIRED"))


async def _stream_uc1_result(
    *,
    request_context: RequestContext,
    event_queue: EventQueue,
    progress_steps: list[str],
    report: dict[str, str],
    task_id: str,
    context_id: str,
    sleep_fn: SleepFn,
    artifact_send_interval_seconds: float,
    log_sink: object | None,
) -> None:
    """Stream the UC1 progress steps, then the energy-saving intent report artifact.

    Shared by the default success path (the task then stays long-running — no terminal state is
    emitted here) and the negotiation continuation path (which emits COMPLETED afterwards).
    """
    try:
        for step_text in progress_steps:
            await emit_status_update(
                request_context=request_context,
                event_queue=event_queue,
                context_id=context_id,
                task_id=task_id,
                state=TaskState.TASK_STATE_WORKING,
                text=step_text,
            )
            if log_sink is not None:
                log_sink(format_stage_log(role="server", stage="task-progress", detail=step_text))
            # Pace the stream so the client observes the task progressing step by step.
            await sleep_fn(artifact_send_interval_seconds)

        report_name = str(report.get("name", "energy saving intent report"))
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=build_metadata_artifact(
                    name=report_name,
                    text=str(report.get("text", report_name)),
                    extension_uri=TASK_T_EXTENSION_URI,
                    metadata_text=str(report.get("metadata", "")),
                ),
                last_chunk=True,
            )
        )
        if log_sink is not None:
            log_sink(
                format_stage_log(
                    role="server",
                    stage="artifact-pushed",
                    detail=f"name={report_name} metadata[{TASK_T_EXTENSION_URI}]",
                )
            )
            log_sink(json.dumps(dict(report), ensure_ascii=False))
    except Exception as exc:
        if log_sink is not None:
            log_sink(format_stage_log(role="server", stage="task-status", detail=f"TASK_STATE_FAILED: {exc}"))
        await emit_status_update(
            request_context=request_context,
            event_queue=event_queue,
            context_id=context_id,
            task_id=task_id,
            state=TaskState.TASK_STATE_FAILED,
            text=f"Mock energy saving stream failed: {exc}",
        )
        raise


async def execute_server_flow(
    *,
    request_context: RequestContext,
    event_queue: EventQueue,
    prompt_server: object,
    progress_steps: list[str] | None = None,
    intent_report: dict[str, str] | None = None,
    sleep_fn: SleepFn | None = None,
    artifact_send_interval_seconds: float = ARTIFACT_SEND_INTERVAL_SECONDS,
    log_sink: object | None = None,
) -> None:
    """Validate the incoming Task-T prompt with A2A-T SDK, then stream the UC1 energy-saving flow.

    Flow (RAN energy-saving interface spec, UC1):
      1. Validate the A2A-Extensions header (Task-T) and extract the prompt text
      2. ``A2ATServer.validate_task_prompt_and_data_filling`` (A2A-T server SDK) validates the
         prompt and extracts the filled parameters
      3. Negotiation continuation (Negotiation-T URI in the message metadata): no SUBMITTED —
         validate, stream the UC1 progress messages and report artifact, then COMPLETED
      4. Default flow: emit SUBMITTED, then on failure REJECTED — or, when the failure carries
         slot-named errors, a Negotiation-T information propose on an INPUT_REQUIRED status
         update (data-driven trigger, the server reads no scenario flag)
      5. Default success: stream the UC1 progress messages as ``TASK_STATE_WORKING`` status
         updates, then the energy-saving intent report as one ``TaskArtifactUpdateEvent`` (the
         report body travels in ``artifact.metadata[Task-T/v1]``); the task then stays
         long-running — no terminal state is emitted
      6. Exception during pushing -> emit FAILED
    """
    resolved_sleep = sleep_fn or asyncio.sleep
    resolved_progress_steps = get_energy_saving_progress_steps() if progress_steps is None else progress_steps
    resolved_report = get_energy_saving_intent_report() if intent_report is None else intent_report
    resolved_task_id = request_context.task_id or ""
    resolved_context_id = request_context.context_id or ""

    _require_task_extension(request_context)

    negotiation_continuation = _is_negotiation_continuation(request_context)
    if negotiation_continuation and log_sink is not None:
        log_sink(
            format_stage_log(
                role="server",
                stage="negotiation-continuation",
                detail="Negotiation-T message detected, continuing the accepted propose",
            )
        )

    prompt_text = _extract_prompt_text(request_context)
    if log_sink is not None:
        log_sink(
            format_payload_log(
                role="server",
                stage="request-inbound",
                payload={"prompt_text": prompt_text},
            )
        )
        log_sink(
            format_stage_log(
                role="server",
                stage="sdk-call",
                detail=(
                    "A2ATServer.validate_task_prompt_and_data_filling("
                    f"template_uri={ENERGY_SAVING_TEMPLATE_URI}, prompt_chars={len(prompt_text)})"
                ),
            )
        )

    validation_failure: str | None = None
    missing_items: list[NegotiationItem] = []
    try:
        filled_params = prompt_server.validate_task_prompt_and_data_filling(
            prompt=prompt_text,
            schema=TASK_PARAM_SCHEMA,
            template_uri=ENERGY_SAVING_TEMPLATE_URI,
        )
        if log_sink is not None:
            log_sink(
                format_stage_log(
                    role="server",
                    stage="sdk-result",
                    detail=f"success extracted_params={getattr(filled_params, 'data', {})}",
                )
            )
    except ContentValidationError as exc:
        validation_failure = str(exc)
        missing_items = _missing_items_from_errors(exc.errors)
        if log_sink is not None:
            log_sink(format_stage_log(role="server", stage="sdk-result", detail=f"failure {validation_failure}"))
    except ValueError as exc:
        validation_failure = str(exc)
        if log_sink is not None:
            log_sink(format_stage_log(role="server", stage="sdk-result", detail=f"invalid input {validation_failure}"))

    # Negotiation continuation (round 2): no SUBMITTED — the completed Task-T prompt is validated,
    # the UC1 result is streamed, and the task closes with COMPLETED.
    if negotiation_continuation:
        if validation_failure is not None:
            await emit_status_update(
                request_context=request_context,
                event_queue=event_queue,
                context_id=resolved_context_id,
                task_id=resolved_task_id,
                state=TaskState.TASK_STATE_REJECTED,
                text=f"Prompt validation failed: {validation_failure}",
            )
            if log_sink is not None:
                log_sink(format_stage_log(role="server", stage="task-status", detail="TASK_STATE_REJECTED"))
            return
        await _stream_uc1_result(
            request_context=request_context,
            event_queue=event_queue,
            progress_steps=resolved_progress_steps,
            report=resolved_report,
            task_id=resolved_task_id,
            context_id=resolved_context_id,
            sleep_fn=resolved_sleep,
            artifact_send_interval_seconds=artifact_send_interval_seconds,
            log_sink=log_sink,
        )
        await emit_status_update(
            request_context=request_context,
            event_queue=event_queue,
            context_id=resolved_context_id,
            task_id=resolved_task_id,
            state=TaskState.TASK_STATE_COMPLETED,
            text=COMPLETED_MESSAGE,
        )
        if log_sink is not None:
            log_sink(format_stage_log(role="server", stage="task-status", detail="TASK_STATE_COMPLETED"))
        return

    # 1. SUBMITTED — always emitted before the validation result (default flow)
    task = Task(
        id=resolved_task_id,
        context_id=resolved_context_id,
        status=build_status(
            context_id=resolved_context_id,
            task_id=resolved_task_id,
            state=TaskState.TASK_STATE_SUBMITTED,
            text=SUBMITTED_MESSAGE,
        ),
    )
    request_context.current_task = Task()
    request_context.current_task.CopyFrom(task)
    await event_queue.enqueue_event(task)
    if log_sink is not None:
        log_sink(format_stage_log(role="server", stage="task-status", detail="TASK_STATE_SUBMITTED"))

    # 2. On failure → Negotiation-T propose when the errors name slots, otherwise REJECTED
    if validation_failure is not None:
        if missing_items:
            await _emit_negotiation_propose(
                request_context=request_context,
                event_queue=event_queue,
                prompt_server=prompt_server,
                missing_items=missing_items,
                task_id=resolved_task_id,
                context_id=resolved_context_id,
                log_sink=log_sink,
            )
            return
        await emit_status_update(
            request_context=request_context,
            event_queue=event_queue,
            context_id=resolved_context_id,
            task_id=resolved_task_id,
            state=TaskState.TASK_STATE_REJECTED,
            text=f"Prompt validation failed: {validation_failure}",
        )
        if log_sink is not None:
            log_sink(format_stage_log(role="server", stage="task-status", detail="TASK_STATE_REJECTED"))
        return

    # 3. On success → stream the UC1 progress messages (default flow stays long-running)
    await _stream_uc1_result(
        request_context=request_context,
        event_queue=event_queue,
        progress_steps=resolved_progress_steps,
        report=resolved_report,
        task_id=resolved_task_id,
        context_id=resolved_context_id,
        sleep_fn=resolved_sleep,
        artifact_send_interval_seconds=artifact_send_interval_seconds,
        log_sink=log_sink,
    )
