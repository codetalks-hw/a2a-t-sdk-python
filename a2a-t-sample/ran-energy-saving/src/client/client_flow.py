from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from a2a.client.client import ClientCallContext
from a2a.types import Role, SendMessageRequest, TaskState
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
    NegotiationContext,
    NegotiationPerformative,
)
from a2a_t.core.standard_templates import (
    FEASIBILITY_NEGOTIATION_ACCEPT_REJECT_URI,
    FEASIBILITY_NEGOTIATION_PROPOSE_URI,
    INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI,
    INFORMATION_NEGOTIATION_PROPOSE_URI,
    TARGET_NEGOTIATION_ACCEPT_REJECT_URI,
    TARGET_NEGOTIATION_PROPOSE_URI,
)
from a2a_t.negotiation.content.enums import NegotiationConclusion
from a2a_t.negotiation.content.models import (
    FeasibilityEndingContent,
    InformationEndingContent,
    NegotiationEndingData,
    NegotiationItem,
    TargetEndingContent,
)
from common.logging_utils import format_payload_log, format_stage_log
from common.sse_event_consumer import normalize_event
from common.task_schema import TASK_PARAM_SCHEMA
from google.protobuf.json_format import MessageToDict

from client.scenario_data import (
    CHANNEL_DATA,
    CHANNEL_TEXT,
    ENERGY_SAVING_TEMPLATE_URI,
    NEGOTIATION_KIND_FEASIBILITY,
    NEGOTIATION_KIND_INFORMATION,
    NEGOTIATION_KIND_TARGET,
    negotiation_feasibility_summary,
    negotiation_target_confirmation,
)

if TYPE_CHECKING:
    from a2a_t.client.a2at_client import A2ATClient


def _require_prompt_text(prompt_content: object) -> tuple[str, str]:
    """Return ``(prompt_text, extension_uri)`` from a successful MetadataContent result."""
    prompt_text = getattr(prompt_content, "prompt_text", None)
    extension_uri = getattr(prompt_content, "extension_uri", None)
    if isinstance(prompt_text, str) and prompt_text.strip() and isinstance(extension_uri, str):
        return prompt_text, extension_uri
    raise ValueError("prompt generation did not produce text")


def _build_request_metadata(initial_input: dict[str, object]) -> str:
    return str(initial_input.get("scenario", ""))


def _parse_negotiation_context(metadata: dict[str, object]) -> NegotiationContext | None:
    """Parse the negotiation session context carried in one status metadata map (demo parity).

    The context travels as the nested ``negotiationContext`` map (``id`` / ``round`` /
    ``maxRounds`` / ``performative``); ``None`` when the metadata carries no parseable session
    (a context failing the ``NegotiationContext`` validation degrades like a missing one).
    """
    raw = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    performative = NegotiationPerformative.try_parse(str(raw.get("performative", "") or ""))
    if performative is None:
        return None
    try:
        return NegotiationContext(
            id=str(raw.get("id", "")),
            round=int(raw.get("round", 1)),
            max_rounds=int(raw.get("maxRounds", NegotiationContext.DEFAULT_MAX_ROUNDS)),
            performative=performative,
        )
    except ValueError:
        return None


#: Negotiation kinds the client-side accept generation dispatches on. The kind resolves from the
#: explicit flag when supplied, else from the propose's ``templateUri`` inside the INPUT_REQUIRED
#: metadata (dispatch on the wire); each kind addresses its own accept-reject template.
_NEGOTIATION_KIND_BY_PROPOSE_URI: dict[str, str] = {
    INFORMATION_NEGOTIATION_PROPOSE_URI: NEGOTIATION_KIND_INFORMATION,
    TARGET_NEGOTIATION_PROPOSE_URI: NEGOTIATION_KIND_TARGET,
    FEASIBILITY_NEGOTIATION_PROPOSE_URI: NEGOTIATION_KIND_FEASIBILITY,
}

_NEGOTIATION_ACCEPT_URI_BY_KIND: dict[str, str] = {
    NEGOTIATION_KIND_INFORMATION: INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI,
    NEGOTIATION_KIND_TARGET: TARGET_NEGOTIATION_ACCEPT_REJECT_URI,
    NEGOTIATION_KIND_FEASIBILITY: FEASIBILITY_NEGOTIATION_ACCEPT_REJECT_URI,
}


def _resolve_negotiation_kind(metadata: dict[str, object], negotiation_kind: str | None) -> str | None:
    """Resolve the accept's negotiation kind: the explicit flag wins, else the propose's templateUri.

    A ``None`` flag dispatches on the wire: the ``templateUri`` of the INPUT_REQUIRED metadata maps
    to the negotiation kind, so the client accepts whatever type the server proposed. An unknown
    flag, or a missing/unknown propose template URI when dispatching on the wire, returns ``None``
    (the caller keeps the unhandled path).
    """
    if negotiation_kind is not None:
        return negotiation_kind if negotiation_kind in _NEGOTIATION_ACCEPT_URI_BY_KIND else None
    propose_uri = metadata.get(TEMPLATE_URI_METADATA_KEY)
    if not isinstance(propose_uri, str):
        return None
    return _NEGOTIATION_KIND_BY_PROPOSE_URI.get(propose_uri)


def _find_negotiation_input_required(events: list[dict[str, object]]) -> dict[str, object] | None:
    """Find the INPUT_REQUIRED status event carrying a Negotiation-T propose.

    The propose travels in the status message metadata under the Negotiation-T extension URI
    together with the nested ``negotiationContext`` session map (any negotiation type; the
    ``templateUri`` inside selects the client's accept type); ``None`` keeps the plain
    single-round flow.
    """
    for event in events:
        if event.get("kind") != "status" or event.get("state") != TaskState.Name(TaskState.TASK_STATE_INPUT_REQUIRED):
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict) or NEGOTIATION_T_EXTENSION_URI not in metadata:
            continue
        return event
    return None


def _first_task_id(events: list[dict[str, object]]) -> str:
    """Return the first non-blank task id carried by the round-1 events."""
    for event in events:
        task_id = event.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id
    return ""


async def _send_and_consume(
    *,
    a2a_client: object,
    request: SendMessageRequest,
    call_context: ClientCallContext,
    send_log_detail: str,
    normalized_events: list[dict[str, object]],
    artifact_count: int,
    max_artifacts: int | None,
    log_sink: object | None,
) -> int:
    """Send one streaming request and append its normalized events to ``normalized_events``.

    Returns the updated artifact count (the stream is cut once ``max_artifacts`` is reached);
    ``response_started`` is tracked per stream, so each round logs its own ``response-inbound``.
    """
    if log_sink is not None:
        log_sink(format_stage_log(role="client", stage="a2a-send-message", detail=send_log_detail))

    response_started = False
    async for stream_response in a2a_client.send_message(request, context=call_context):
        if log_sink is not None:
            log_sink(
                format_stage_log(
                    role="client",
                    stage="a2a-event",
                    detail=json.dumps(MessageToDict(stream_response), ensure_ascii=False),
                )
            )
        event = normalize_event(stream_response)
        if not response_started:
            response_started = True
            if log_sink is not None:
                log_sink(format_stage_log(role="client", stage="response-inbound", detail="stream started"))
        if event["kind"] == "artifact" and max_artifacts is not None and artifact_count >= max_artifacts:
            break
        normalized_events.append(event)
        if event["kind"] == "artifact":
            artifact_count += 1
    return artifact_count


def _numbered_items(items: Iterable[tuple[str, str | None]]) -> str:
    """Render items as one numbered list: ``N. name：value；`` per item (the negotiation demo rule).

    The value is omitted when blank; the full-width separators keep the rendering identical to the
    ``a2a-t-sample/negotiation`` demo's ``_numbered_items`` for both languages.
    """
    lines: list[str] = []
    for index, (name, value) in enumerate(items, start=1):
        line = f"{index}. {name}"
        if value is not None and value.strip():
            line += f"：{value}"
        lines.append(f"{line}；")
    return "".join(lines)


def _negotiation_accept_content(
    negotiation_kind: str,
    *,
    negotiation_answers: dict[str, str],
    env_path: Path | None,
) -> InformationEndingContent | TargetEndingContent | FeasibilityEndingContent:
    """Build the typed accept content of one negotiation type (plan R3 scenario semantics).

    Information keeps the v1 path: the accept items come from the supplied answers. Target accepts
    the clarified target and feasibility confirms the feasibility evaluation, both with the
    scenario-owned text of the resolved language (the canned accept-extraction content).
    """
    if negotiation_kind == NEGOTIATION_KIND_TARGET:
        return TargetEndingContent(
            conclusion=NegotiationConclusion.ACCEPT,
            confirmed_intent=negotiation_target_confirmation(env_path=env_path),
            failure_reason=None,
        )
    if negotiation_kind == NEGOTIATION_KIND_FEASIBILITY:
        return FeasibilityEndingContent(
            conclusion=NegotiationConclusion.ACCEPT,
            feasibility_summary=negotiation_feasibility_summary(env_path=env_path),
        )
    return InformationEndingContent(
        conclusion=NegotiationConclusion.ACCEPT,
        items=[NegotiationItem(name=name, value=value) for name, value in negotiation_answers.items()],
    )


def _negotiation_accept_text(
    negotiation_kind: str,
    *,
    negotiation_answers: dict[str, str],
    env_path: Path | None,
) -> str:
    """Render the natural-language input of the from-text accept generation for one type.

    The rendering carries the same content as the typed accept builders (the scenario accessors
    mirror the canned extraction responses), so the extraction step of the from-text channel
    returns the canonical content and both channels produce equivalent messages.
    """
    if negotiation_kind == NEGOTIATION_KIND_TARGET:
        return f"Accept. {negotiation_target_confirmation(env_path=env_path)}"
    if negotiation_kind == NEGOTIATION_KIND_FEASIBILITY:
        return f"Accept. {negotiation_feasibility_summary(env_path=env_path)}"
    return f"Accept. {_numbered_items(negotiation_answers.items())}"


def _negotiation_accept_log_detail(
    negotiation_kind: str,
    *,
    negotiation_answers: dict[str, str],
    env_path: Path | None,
) -> str:
    """Summarize the accept content of one negotiation type for the ``negotiation-accept`` log."""
    if negotiation_kind == NEGOTIATION_KIND_TARGET:
        return f"confirmed_intent={negotiation_target_confirmation(env_path=env_path)}"
    if negotiation_kind == NEGOTIATION_KIND_FEASIBILITY:
        return f"feasibility_summary={negotiation_feasibility_summary(env_path=env_path)}"
    return f"items={[(name, value) for name, value in negotiation_answers.items()]}"


async def _run_negotiation_accept_round(
    *,
    prompt_client: A2ATClient,
    a2a_client: object,
    initial_input: dict[str, object],
    session: NegotiationContext,
    task_id: str,
    context_id: str,
    negotiation_kind: str,
    channel: str,
    negotiation_params: dict[str, object],
    negotiation_answers: dict[str, str],
    env_path: Path | None,
    normalized_events: list[dict[str, object]],
    artifact_count: int,
    max_artifacts: int | None,
    log_sink: object | None,
) -> int:
    """Answer the server's INPUT_REQUIRED propose with the filled Task-T prompt and the accept.

    Round 2 of the negotiation flow (demo ``run_four_message_flow`` message 3 over the real
    streaming path): the session context parsed from the propose metadata drives the accept
    (round advanced by one, performative ACCEPT) of the resolved negotiation type, the Task-T
    prompt is regenerated from the filled parameters, and both travel in the round-2 message
    metadata under their extension URIs. The channel switches only the accept generation call:
    ``data`` builds the typed content and calls the deterministic from-data API (zero LLM calls),
    ``text`` renders the same content as a natural-language message and calls the from-text API,
    whose extraction step parses it back into the canonical typed content (one LLM call). Returns
    the updated artifact count after consuming the round-2 stream into ``normalized_events``.
    """
    # Filled Task-T prompt: the parameters the propose asked for, regenerated from data. All types
    # complete the same four parameters, so one regeneration serves every negotiation kind.
    filled_prompt = prompt_client.generate_task_prompt_from_data_with_schema(
        dict(negotiation_params), TASK_PARAM_SCHEMA, ENERGY_SAVING_TEMPLATE_URI
    )
    filled_text, _ = _require_prompt_text(filled_prompt)

    # Negotiation-T accept continuing the server's session (round advanced, performative ACCEPT).
    accept_context = session.next_round().with_performative(NegotiationPerformative.ACCEPT)
    accept_template_uri = _NEGOTIATION_ACCEPT_URI_BY_KIND[negotiation_kind]
    if channel == CHANNEL_TEXT:
        accept = prompt_client.generate_negotiation_accept_prompt_from_text(
            _negotiation_accept_text(negotiation_kind, negotiation_answers=negotiation_answers, env_path=env_path),
            accept_context,
            accept_template_uri,
        )
    else:
        accept = prompt_client.generate_negotiation_accept_prompt_from_data(
            NegotiationEndingData(
                context=accept_context,
                content=_negotiation_accept_content(
                    negotiation_kind, negotiation_answers=negotiation_answers, env_path=env_path
                ),
            ),
            accept_template_uri,
        )
    accept_text, _ = _require_prompt_text(accept)
    accept_template_uri = str(getattr(accept, "template_uri", None) or accept_template_uri)
    accept_negotiation_context = getattr(accept, "negotiation_context", None)

    if log_sink is not None:
        log_sink(
            format_stage_log(
                role="client",
                stage="negotiation-accept",
                detail=(
                    f"generated (round={session.round + 1}) "
                    f"template_uri={accept_template_uri} "
                    + _negotiation_accept_log_detail(
                        negotiation_kind, negotiation_answers=negotiation_answers, env_path=env_path
                    )
                ),
            )
        )

    round2_request = SendMessageRequest()
    round2_request.message.message_id = str(uuid.uuid4())
    round2_request.message.role = Role.ROLE_USER
    round2_request.message.task_id = task_id
    round2_request.message.context_id = context_id
    round2_request.message.parts.add().text = _build_request_metadata(initial_input)
    round2_request.message.metadata[TASK_T_EXTENSION_URI] = filled_text
    round2_request.message.metadata[NEGOTIATION_T_EXTENSION_URI] = accept_text
    round2_request.message.metadata[TEMPLATE_URI_METADATA_KEY] = accept_template_uri
    round2_request.message.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = {
        "id": session.id,
        "round": session.round + 1,
        "maxRounds": session.max_rounds,
        "performative": (
            accept_negotiation_context.performative.value
            if accept_negotiation_context is not None
            else NegotiationPerformative.ACCEPT.value
        ),
    }

    # The A2A-Extensions header carries both extension URIs as the comma-joined string the
    # a2a-sdk service-parameter helper emits (see with_a2a_extensions).
    extension_uris = sorted({TASK_T_EXTENSION_URI, NEGOTIATION_T_EXTENSION_URI})
    round2_call_context = ClientCallContext(service_parameters={"A2A-Extensions": ",".join(extension_uris)})

    return await _send_and_consume(
        a2a_client=a2a_client,
        request=round2_request,
        call_context=round2_call_context,
        send_log_detail=(
            f"round=2 streaming text_part={_build_request_metadata(initial_input)} "
            f"metadata_keys=[{TASK_T_EXTENSION_URI}, {NEGOTIATION_T_EXTENSION_URI}, "
            f"{TEMPLATE_URI_METADATA_KEY}, {NEGOTIATION_CONTEXT_METADATA_KEY}] "
            f"header[A2A-Extensions]={','.join(extension_uris)}"
        ),
        normalized_events=normalized_events,
        artifact_count=artifact_count,
        max_artifacts=max_artifacts,
        log_sink=log_sink,
    )


async def run_client_flow(
    *,
    prompt_client: A2ATClient,
    a2a_client: object,
    initial_input: dict[str, object],
    input_text: str,
    max_artifacts: int | None = None,
    log_sink: object | None = None,
    negotiation_params: dict[str, object] | None = None,
    negotiation_answers: dict[str, str] | None = None,
    negotiation_kind: str | None = None,
    channel: str = CHANNEL_DATA,
    env_path: Path | None = None,
) -> list[dict[str, object]]:
    """Generate a Task-T prompt with the A2A-T client SDK, then stream the A2A server's artifacts.

    Pipeline and its logged boundaries:
      1. raw natural-language input                            -> ``[client] input-raw``
      2. ``A2ATClient.generate_task_prompt_from_text``         -> ``[client] sdk-call`` /
                                                                   ``[client] sdk-output-prompt``
      3. A2A SDK ``send_message`` (streaming) to the server    -> ``[client] a2a-send-message``
      4. each received A2A event, as raw JSON                 -> ``[client] a2a-event``
      5. Negotiation round trip: when the server answers INPUT_REQUIRED with a Negotiation-T
         propose (status message metadata) and negotiation answers are supplied, resolve the
         negotiation kind (the explicit flag, else the propose's ``templateUri`` — dispatch on
         the wire), regenerate the Task-T prompt from the filled parameters, render the
         Negotiation-T accept of that type for the parsed session (from typed data, or from a
         natural-language rendering plus one extraction LLM call per the configured channel),
         and re-send over a second streaming request
                                                            -> ``[client] negotiation-accept`` /
                                                               ``[client] a2a-send-message`` (round=2)

    Without the negotiation answers — or when the propose carries no parseable session context or
    resolves to no negotiation kind — an INPUT_REQUIRED propose is logged as unhandled and the
    flow ends after round 1; the default flow (no INPUT_REQUIRED) is unchanged.

    Message-body convention:
      * text part                 -> task name (request metadata)
      * metadata[Task-T/v1]       -> generated prompt text
      * metadata[Negotiation-T/v1]-> accept prompt text (round 2 only)
      * header A2A-Extensions     -> Task-T/v1 (plus Negotiation-T/v1 on round 2)
    """
    if log_sink is not None:
        log_sink(format_payload_log(role="client", stage="scenario-data", payload=initial_input))

    # (1) natural-language input handed to the A2A-T client SDK (supplied by the caller)
    if log_sink is not None:
        log_sink(format_stage_log(role="client", stage="input-raw", detail=input_text))

    # (2) A2A-T client SDK prompt generation (one slot-extraction LLM call)
    if log_sink is not None:
        log_sink(
            format_stage_log(
                role="client",
                stage="sdk-call",
                detail=(
                    "A2ATClient.generate_task_prompt_from_text("
                    f"text={input_text}, template_uri={ENERGY_SAVING_TEMPLATE_URI})"
                ),
            )
        )
    prompt_content = prompt_client.generate_task_prompt_from_text(input_text, ENERGY_SAVING_TEMPLATE_URI)
    prompt_text, extension_uri = _require_prompt_text(prompt_content)

    if log_sink is not None:
        log_sink(format_stage_log(role="client", stage="sdk-output-prompt", detail=prompt_text))
        log_sink(
            format_stage_log(
                role="client",
                stage="sdk-output-meta",
                detail=f"template_uri={getattr(prompt_content, 'template_uri', None)} extension_uri={extension_uri}",
            )
        )

    # (3) build the A2A task message and send it (streaming) to the A2A server
    request = SendMessageRequest()
    request.message.message_id = str(uuid.uuid4())
    request.message.role = Role.ROLE_USER
    request.message.parts.add().text = _build_request_metadata(initial_input)
    request.message.metadata[extension_uri] = prompt_text
    request.message.metadata["templateUri"] = (
        getattr(prompt_content, "template_uri", None) or ENERGY_SAVING_TEMPLATE_URI
    )

    call_context = ClientCallContext(
        service_parameters={"A2A-Extensions": extension_uri},
    )

    # (4) consume the streamed status/artifact events
    normalized_events: list[dict[str, object]] = []
    artifact_count = 0
    artifact_count = await _send_and_consume(
        a2a_client=a2a_client,
        request=request,
        call_context=call_context,
        send_log_detail=(
            f"streaming text_part={_build_request_metadata(initial_input)} "
            f"metadata_keys=[{extension_uri}, templateUri] "
            f"header[A2A-Extensions]={extension_uri}"
        ),
        normalized_events=normalized_events,
        artifact_count=artifact_count,
        max_artifacts=max_artifacts,
        log_sink=log_sink,
    )

    # (5) negotiation round trip: answer the server's INPUT_REQUIRED propose with the accept
    input_required_event = _find_negotiation_input_required(normalized_events)
    if input_required_event is not None and (negotiation_params is None or negotiation_answers is None):
        if log_sink is not None:
            log_sink(
                format_stage_log(
                    role="client",
                    stage="negotiation-unhandled",
                    detail=(
                        "Negotiation-T INPUT_REQUIRED received; no negotiation answers supplied, staying on round 1"
                    ),
                )
            )
    elif input_required_event is not None:
        metadata = input_required_event.get("metadata")
        session = _parse_negotiation_context(metadata) if isinstance(metadata, dict) else None
        if session is not None and isinstance(metadata, dict):
            negotiation_type = _resolve_negotiation_kind(metadata, negotiation_kind)
            if negotiation_type is not None:
                artifact_count = await _run_negotiation_accept_round(
                    prompt_client=prompt_client,
                    a2a_client=a2a_client,
                    initial_input=initial_input,
                    session=session,
                    task_id=_first_task_id(normalized_events),
                    context_id=str(input_required_event.get("context_id", "")),
                    negotiation_kind=negotiation_type,
                    channel=channel,
                    negotiation_params=negotiation_params,
                    negotiation_answers=negotiation_answers,
                    env_path=env_path,
                    normalized_events=normalized_events,
                    artifact_count=artifact_count,
                    max_artifacts=max_artifacts,
                    log_sink=log_sink,
                )
            elif log_sink is not None:
                log_sink(
                    format_stage_log(
                        role="client",
                        stage="negotiation-unhandled",
                        detail=(
                            "Negotiation-T INPUT_REQUIRED received; the negotiation request could not be used "
                            "(unrecognized propose templateUri), staying on round 1"
                        ),
                    )
                )
        elif log_sink is not None:
            log_sink(
                format_stage_log(
                    role="client",
                    stage="negotiation-unhandled",
                    detail=(
                        "Negotiation-T INPUT_REQUIRED received; the negotiation request could not be used "
                        "(malformed/missing session context), staying on round 1"
                    ),
                )
            )

    if log_sink is not None:
        log_sink(
            format_stage_log(
                role="client",
                stage="stream-completed",
                detail=f"events={len(normalized_events)} artifacts={artifact_count}",
            )
        )
    return normalized_events
