"""A2A and session routes of the NMS simulator.

Both transports share one Task store and one negotiation state machine:

* ``POST /a2a/json/message:stream`` streams the first event and keeps the vendor's never-closing
  SSE stream in strict mode.
* ``POST /a2a/json/message:send`` returns the first event as JSON and POSTs every later
  ``statusUpdate``/``artifactUpdate`` to the registered push-notification URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
)
from a2a_t.core.plan_binding import CanonicalFormError, canonical_digest
from shared.agreement import (
    AGREEMENT_METADATA_KEY,
    REQUEST_ID_METADATA_KEY,
    next_revision,
    read_agreement,
    read_agreement_ref,
)
from shared.agreement import (
    REASON_EXPIRED as AGREEMENT_EXPIRED,
)
from shared.agreement import (
    REASON_MISMATCH as AGREEMENT_MISMATCH,
)
from shared.agreement import (
    REASON_STALE as AGREEMENT_STALE,
)
from shared.agreement import (
    REASON_UNKNOWN as AGREEMENT_UNKNOWN,
)
from shared.approval import (
    APPROVAL_METADATA_KEY,
    OUTCOME_REJECT,
    REASON_EXPIRED,
    VERIFY_EXPIRED,
    VERIFY_UNKNOWN,
    ApprovalDecision,
    plan_digest,
    read_approval_decision,
    read_approval_request,
    verify_decision,
)
from shared.assurance import read_hops
from shared.change_order import (
    BASELINE_ERROR_METADATA_KEY,
    REASON_MISSING,
    REASON_STALE,
    read_baseline,
)
from shared.readback import Readback, compare_plans
from shared.reversal import (
    OPERATION_RESTORE,
    REVERSAL_METADATA_KEY,
    Reversal,
    read_reversal,
    restoration_for,
)
from shared.typed_payload import (
    TYPED_TASK_T_METADATA_KEY,
    read_statement,
    statement_metadata,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from demo import ran_data
from demo.nms_sim.approval import (
    APPROVAL_PENDING_TEXT,
    build_request,
    intent_from_plan,
    plan_for,
)
from demo.nms_sim.config import (
    BASELINE_REQUIRED,
    CARRIER_TASK,
    PURE_FAILURE_COMPLETED,
    PURE_FAILURE_REJECTED,
    PURE_QUESTION_CONFIRM,
    WINDOW_POLICY_ANY,
    SimConfig,
)
from demo.nms_sim.executor import apply_intent, report_artifact, structured_output
from demo.nms_sim.intent import (
    ExtractedContent,
    ParsedIntent,
    SimRequestError,
    accepted_intent,
    declares_negotiation,
    extension_header_value,
    extract_content,
    message_text,
    parse_message_request,
    read_message_request,
    select_issue,
)
from demo.nms_sim.metrics import (
    OUTCOME_ABORTED,
    OUTCOME_ACCEPTED,
    OUTCOME_INVALID,
    OUTCOME_REFUSED,
    OUTCOME_REJECTED,
    PROTOCOL_A2A,
    PROTOCOL_A2A_T,
    ServerMetrics,
)
from demo.nms_sim.missing_items import missing_items_metadata
from demo.nms_sim.negotiation import (
    PERFORMATIVE_ABORT,
    PERFORMATIVE_ACCEPT,
    PERFORMATIVE_REJECT,
    NegotiationContext,
    NegotiationIssue,
    NegotiationSession,
    NegotiationStore,
    abort_metadata,
    negotiation_message,
    propose_metadata,
)
from demo.nms_sim.push import PushConfig, PushConfigExistsError, PushRegistry, PushSender
from demo.nms_sim.sse import message_stream, status_stream, task_stream
from demo.nms_sim.tasks import (
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INPUT_REQUIRED,
    TASK_REJECTED,
    TASK_SUBMITTED,
    TASK_WORKING,
    TERMINAL_STATES,
    SimTask,
    TaskStore,
)
from demo.nms_sim.token import TokenStore

_LOGGER = logging.getLogger("ran_energy_saving.nms_sim")

#: Metadata key a caller uses to declare the hops a request already passed through.
DELEGATION_METADATA_KEY: Final[str] = "a2at-sample/delegation"
#: Name this server appends to the chain.
DELEGATION_HOP: Final[str] = "nms"

#: SSE media type.
EVENT_STREAM: str = "text/event-stream"

#: Cancel reason that asks the peer to execute the reversal the agreement carries. It is the
#: default when a cancel request has no body: an abort with no stated reason is still an abort.
CANCEL_REASON_ABORT: Final[str] = "abort"

#: The fixed plain-A2A question the real vendor asks on every negotiation round. Its A2A front
#: end does not render the business issue: the operator answers in free text and the vendor merges
#: the reply into its intent before validating again.
PURE_CONFIRM_TEXT: str = "Not feasible, please confirm"

#: Explicit refusal in a plain-A2A answer. The vendor skill terminates only on a clear refusal;
#: the simulator must do the same, otherwise an automatic client's "Reject: ..." would be read as
#: another answer and the Task would keep asking until the round budget ran out.
PURE_REJECTION: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:reject|rejected|refuse|decline)\b|\bnot\s+acceptable\b|拒绝|不接受",
    re.IGNORECASE,
)


@dataclass
class SimRuntime:
    """Stores and background workers shared by both transports.

    Attributes:
        store: Tasks served by both endpoints.
        tokens: Session tokens.
        negotiations: Negotiation state machine keyed by negotiation id.
        pushes: Background webhook sender.
        registry: Per-Task push configs.
        executions: Request identity -> Task that already answers it.
        agreements: Agreement id -> current revision.
        revisions: Agreement id -> revision -> document, every revision this server kept.
    """

    store: TaskStore
    tokens: TokenStore
    negotiations: NegotiationStore
    pushes: PushSender
    registry: PushRegistry
    #: Request identity -> Task that already answers it. A retry returns that Task instead of
    #: executing the plan a second time.
    executions: dict[str, str] = field(default_factory=dict)
    #: Agreement id -> latest revision. An agreement outlives the Task that first produced it, so
    #: this table -- not the Task list -- decides what remains executable.
    agreements: dict[str, dict[str, object]] = field(default_factory=dict)
    #: Agreement id -> revision -> document. Every revision is kept: a change order names one, and
    #: a client must be able to see why a superseded reference was refused.
    revisions: dict[str, dict[int, dict[str, object]]] = field(default_factory=dict)


def build_runtime(
    config: SimConfig,
    *,
    push_transport: httpx.AsyncBaseTransport | None = None,
) -> SimRuntime:
    """Build the simulator runtime for one app instance."""
    return SimRuntime(
        store=TaskStore(limit=config.task_limit, shape=config.task_shape),
        tokens=TokenStore(config),
        negotiations=NegotiationStore(ttl_seconds=config.negotiation_ttl_seconds),
        pushes=PushSender(transport=push_transport),
        registry=PushRegistry(),
    )


def _bearer_token(request: Request) -> str:
    """Return the bearer token of one request, from either accepted header."""
    header_token = request.headers.get("x-auth-token", "").strip()
    if header_token:
        return header_token
    authorization = request.headers.get("authorization", "").strip()
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _json_body(request: Request) -> Mapping[str, object] | None:
    """Read a JSON object body, or ``None`` when it is malformed."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, Mapping) else None


def build_routes(
    config: SimConfig,
    *,
    runtime: SimRuntime | None = None,
) -> tuple[list[BaseRoute], SimRuntime]:
    """Build the simulator routes and their runtime."""
    resolved = runtime or build_runtime(config)
    # Server-side work counters for the A2A-T vs A2A comparison; they observe, never decide.
    metrics = ServerMetrics()
    # A count without its switches is not evidence: the same batch under a stricter rate or window
    # policy fails differently, so the snapshot states the profile it was produced under.
    metrics.describe(
        {
            "fidelity": config.fidelity,
            "window_policy": config.window_policy,
            "rate_policy": config.rate_policy,
            "long_outage_feasibility": config.long_outage_feasibility,
            "feasibility_rate_mbps": config.feasibility_rate_mbps,
            "pure_question": config.pure_question,
            "missing_items": config.missing_items,
            "approval": config.approval,
            "approval_ttl_seconds": config.approval_ttl_seconds,
            "approval_tamper": config.approval_tamper,
            "baseline": config.baseline,
            "baseline_stale": config.baseline_stale,
            "negotiation_ttl_seconds": config.negotiation_ttl_seconds,
            "max_saving_rate": config.max_saving_rate,
            "window": config.window,
        }
    )
    resolved_store = resolved.store
    resolved_tokens = resolved.tokens
    negotiations = resolved.negotiations
    pushes = resolved.pushes
    registry = resolved.registry

    def unauthorized() -> JSONResponse:
        return JSONResponse(
            {"error": {"code": 401, "message": "A valid session token is required"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def authorized(request: Request) -> bool:
        token = _bearer_token(request)
        return bool(token) and resolved_tokens.valid(token)

    def negotiation_error(message: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": 422, "status": "INVALID_ARGUMENT", "message": message}},
            status_code=422,
        )

    async def sessions(request: Request) -> Response:
        """Issue one session token."""
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if str(body.get("grantType", "")) != "password":
            return JSONResponse({"error": "unsupported grantType"}, status_code=400)
        token = resolved_tokens.issue(
            user=str(body.get("userName", "")),
            value=str(body.get("value", "")),
        )
        if token is None:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        return JSONResponse(
            {"accessSession": token, "expires": config.token_ttl_seconds},
            status_code=200,
        )

    # ---------------------------------------------------------------- execution

    def _prepare_report(
        task: SimTask,
        intent: ParsedIntent,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        """Render the report of one accepted intent and stash its structured Task message."""
        output = structured_output(intent)
        resolved_store.update(
            task.task_id,
            state=TASK_SUBMITTED,
            message=f"Task creation succeeded{_json_compact(output)}",
        )
        try:
            report = apply_intent(
                intent, allow_any_window=config.window_policy == WINDOW_POLICY_ANY
            )
        except ran_data.RanDataError as exc:
            _LOGGER.warning("simulator execution failed: %s", exc)
            resolved_store.update(task.task_id, state=TASK_FAILED, message=str(exc))
            return None
        readback = _readback_for(intent, config, task)
        return report, report_artifact(task.task_id, report, readback=readback)

    def _stream_completion(
        task: SimTask,
        intent: ParsedIntent,
        *,
        hold: bool = False,
    ) -> Response:
        """Stream one accepted request from execution to its completed report."""
        prepared = _prepare_report(task, intent)
        if prepared is None:
            return JSONResponse(
                {"error": {"code": 500, "message": task.message}},
                status_code=500,
            )
        report, artifact = prepared
        return StreamingResponse(
            task_stream(task, report=report, artifact=artifact, config=config, hold=hold),
            media_type=EVENT_STREAM,
            headers={"Cache-Control": "no-cache"},
        )

    async def _execute_task(
        task: SimTask,
        artifact: dict[str, object],
        *,
        hold: bool,
        push: PushConfig | None,
    ) -> None:
        """Advance one Task and POST each transition to its webhook."""
        try:
            await asyncio.sleep(config.working_delay)
            if task.state == TASK_CANCELED:
                await pushes.send(push, task.status_frame())
                return
            resolved_store.update(
                task.task_id,
                state=TASK_WORKING,
                message="Task is executing the energy-saving plan.",
            )
            await pushes.send(push, task.status_frame())
            await asyncio.sleep(config.completion_delay)
            if task.state == TASK_CANCELED:
                await pushes.send(push, task.status_frame())
                return
            resolved_store.add_artifact(task.task_id, artifact)
            await pushes.send(push, task.artifact_frame())
            if hold:
                resolved_store.update(
                    task.task_id,
                    state=TASK_WORKING,
                    message="Task is running; ready to be modified.",
                )
                await pushes.send(push, task.status_frame())
                return
            resolved_store.update(task.task_id, state=TASK_COMPLETED, message="Task completed.")
            await pushes.send(push, task.status_frame())
        except Exception:  # noqa: BLE001 - a background push must never crash the server
            _LOGGER.warning("simulated task execution failed for %s", task.task_id, exc_info=True)

    def _json_completion(
        task: SimTask,
        intent: ParsedIntent,
        *,
        hold: bool,
        push: PushConfig | None,
    ) -> Response:
        """Return the first event as JSON and push the rest in the background."""
        prepared = _prepare_report(task, intent)
        if prepared is None:
            return JSONResponse(
                {"error": {"code": 500, "message": task.message}},
                status_code=500,
            )
        _report, artifact = prepared
        pushes.run(_execute_task(task, artifact, hold=hold, push=push))
        return JSONResponse({"task": task.to_dict(embedded=True)}, status_code=200)

    # ---------------------------------------------------------------- push config

    def _push_config_for(
        task: SimTask,
        request_config: PushConfig | None,
        *,
        explicit_task: bool,
        context_id: str = "",
    ) -> PushConfig | None:
        """Attach one request's config to a Task, or return the existing/pending one.

        A config registered by the message that started the negotiation is held by context id; a
        Modify continues a Task whose own context predates this run, so both ids are tried.
        """
        if request_config is None:
            return (
                registry.for_task(task.task_id)
                or registry.take_pending(task.context_id)
                or registry.take_pending(context_id)
            )
        registry.register(task.task_id, request_config, explicit=explicit_task)
        return request_config

    def _register_config_error(exc: PushConfigExistsError) -> JSONResponse:
        """Return the vendor's 500 answer for a second registration."""
        metrics.outcome(PROTOCOL_A2A_T, OUTCOME_INVALID)
        return JSONResponse(
            {"error": {"code": 500, "status": "INTERNAL", "message": str(exc)}},
            status_code=500,
        )

    # ---------------------------------------------------------------- tasks

    def _requested_task_id(body: Mapping[str, object]) -> str | None:
        """Return the Task id named by a message, when it carries one."""
        message = body.get("message")
        if not isinstance(message, Mapping):
            return None
        for key in ("taskId", "task_id"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _resolve_task(
        body: Mapping[str, object],
        *,
        context_id: str,
        fallback_task_id: str | None = None,
    ) -> tuple[SimTask, bool]:
        """Return the Task a request continues, or create a new one.

        Returns:
            The Task and whether it was created by this request.
        """
        requested = _requested_task_id(body) or fallback_task_id
        task = resolved_store.get(requested) if requested else None
        if task is None:
            return resolved_store.create(context_id=context_id), True
        resolved_store.update(task.task_id, state=TASK_SUBMITTED, message="Task creation succeeded")
        return task, False

    # ---------------------------------------------------------------- negotiation

    def _new_negotiation(
        content: ExtractedContent,
        issue: NegotiationIssue,
        *,
        context_id: str,
        task_id: str | None,
        structured: bool,
    ) -> NegotiationSession:
        """Start one negotiation at round 1."""
        return negotiations.create(
            kind=issue.kind,
            max_rounds=config.max_rounds,
            context_id=context_id,
            task_id=task_id if structured and config.negotiation_carrier == CARRIER_TASK else None,
            baseline=content,
        )

    def _propose(
        session: NegotiationSession,
        issue: NegotiationIssue,
        *,
        structured: bool,
        task: SimTask | None,
        stream: bool,
        context_id: str,
        request_config: PushConfig | None = None,
        explicit_task: bool = False,
    ) -> Response:
        """Answer one request with a Negotiation-T proposal."""
        metrics.question(PROTOCOL_A2A_T, issue.kind)
        metrics.round(PROTOCOL_A2A_T)
        context = negotiations.context(session, performative="PROPOSE")
        if not structured:
            # Root-cause path: the vendor answers an undeclared negotiation as a bare message.
            if stream:
                return StreamingResponse(
                    message_stream(issue.body(), context_id=context_id, config=config, structured=False),
                    media_type=EVENT_STREAM,
                    headers={"Cache-Control": "no-cache"},
                )
            return JSONResponse(
                {"message": negotiation_message(context_id=context_id, body=issue.body(), metadata={})},
                status_code=200,
            )
        metadata = propose_metadata(
            issue,
            context,
            extra=missing_items_metadata(issue, config) if config.missing_items else None,
        )
        if config.negotiation_carrier != CARRIER_TASK:
            if stream:
                return StreamingResponse(
                    message_stream(
                        issue.body(),
                        context_id=context_id,
                        config=config,
                        structured=True,
                        metadata=metadata,
                    ),
                    media_type=EVENT_STREAM,
                    headers={"Cache-Control": "no-cache"},
                )
            return JSONResponse(
                {"message": negotiation_message(context_id=context_id, body=issue.body(), metadata=metadata)},
                status_code=200,
            )
        if task is None:
            task = resolved_store.create(context_id=context_id)
            session.task_id = task.task_id
        else:
            session.task_id = task.task_id
        try:
            _push_config_for(
                task,
                request_config,
                explicit_task=explicit_task,
                context_id=context_id,
            )
        except PushConfigExistsError as exc:
            return _register_config_error(exc)
        resolved_store.update(
            task.task_id,
            state=TASK_INPUT_REQUIRED,
            message=issue.body(),
            metadata=metadata,
        )
        if stream:
            # Close the stream: the client answers from the status message, not from a heartbeat.
            return StreamingResponse(
                status_stream(task, config=config, keep_open=False),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse({"task": task.to_dict(embedded=True)}, status_code=200)

    def _finish_accept(
        session: NegotiationSession,
        content: ExtractedContent,
        body: Mapping[str, object],
        request_config: PushConfig | None,
        *,
        stream: bool,
    ) -> Response:
        """Complete an accepted negotiation, continuing the carried Task when there is one."""
        metrics.outcome(PROTOCOL_A2A_T, OUTCOME_ACCEPTED)
        intent = accepted_intent(content, session.baseline, config)
        context_id = _context_id(body) or session.context_id
        task, is_new = _resolve_task(body, context_id=context_id, fallback_task_id=session.task_id)
        _remember_plan(task, intent, _message_metadata(body))
        negotiations.close(session)
        try:
            push = _push_config_for(
                task,
                request_config,
                explicit_task=bool(_requested_task_id(body)),
                context_id=context_id,
            )
        except PushConfigExistsError as exc:
            return _register_config_error(exc)
        hold = config.hold_working and is_new
        if config.approval:
            return _request_approval(task, intent, stream=stream, hold=hold)
        if stream:
            return _stream_completion(task, intent, hold=hold)
        return _json_completion(task, intent, hold=hold, push=push)

    # ---------------------------------------------------------------- approval (S6)

    def _request_approval(
        task: SimTask,
        intent: ParsedIntent,
        *,
        stream: bool,
        hold: bool = False,
    ) -> Response:
        """Park an accepted plan in ``INPUT_REQUIRED`` until a plan-bound decision arrives."""
        request = build_request(
            intent,
            task_id=task.task_id,
            ttl_seconds=config.approval_ttl_seconds,
            tamper=config.approval_tamper,
        )
        task.approval_request = request.to_dict()
        task.approval_plan = dict(plan_for(intent))
        task.approval_decision = None
        task.pure_hold = hold
        resolved_store.update(
            task.task_id,
            state=TASK_INPUT_REQUIRED,
            message=APPROVAL_PENDING_TEXT,
            metadata=request.to_metadata(),
        )
        return _approval_response(task, stream=stream)

    def _approval_response(task: SimTask, *, stream: bool) -> Response:
        """Return the current state of a Task that is waiting for (or finished) an approval."""
        if stream:
            return StreamingResponse(
                status_stream(task, config=config, keep_open=False),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse({"task": task.to_dict(embedded=True)}, status_code=200)

    def _handle_approval_decision(
        body: Mapping[str, object],
        decision: ApprovalDecision,
        *,
        stream: bool,
        protocol: str,
    ) -> Response:
        """Apply one decision to the Task that asked for it.

        Identity and plan are checked before the clock, and a decision that does not match leaves
        the Task waiting: executing it would apply an approval to something nobody approved.
        """
        requested = _requested_task_id(body)
        context_id = _context_id(body)
        task = (
            resolved_store.get(requested)
            if requested
            else resolved_store.find_by_context(context_id) if context_id else None
        )
        if task is None:
            metrics.outcome(protocol, OUTCOME_INVALID)
            return JSONResponse(
                {"error": {"code": 409, "message": VERIFY_UNKNOWN}}, status_code=409
            )
        if task.approval_decision is not None:
            # A repeated decision is idempotent: the Task keeps the terminal state it reached.
            return _approval_response(task, stream=stream)
        if not isinstance(task.approval_request, Mapping):
            metrics.outcome(protocol, OUTCOME_INVALID)
            return JSONResponse(
                {"error": {"code": 409, "message": VERIFY_UNKNOWN}}, status_code=409
            )
        request = read_approval_request({APPROVAL_METADATA_KEY: task.approval_request})
        if request is None:
            metrics.outcome(protocol, OUTCOME_INVALID)
            return JSONResponse(
                {"error": {"code": 409, "message": VERIFY_UNKNOWN}}, status_code=409
            )
        failure = verify_decision(request, decision)
        if failure is not None:
            task.approval_decision = ApprovalDecision(
                approval_id=request.approval_id,
                plan_digest=request.plan_digest,
                outcome=OUTCOME_REJECT,
                reason_code=REASON_EXPIRED if failure == VERIFY_EXPIRED else failure,
                reason=f"The approval could not be applied: {failure}.",
            ).to_dict()
            if failure == VERIFY_EXPIRED:
                # An expired request is terminal: the plan must be re-requested, not resumed.
                task.approval_request = None
                resolved_store.update(
                    task.task_id,
                    state=TASK_REJECTED,
                    message="The approval request expired before a decision arrived.",
                    metadata={APPROVAL_METADATA_KEY: task.approval_decision},
                )
                metrics.outcome(protocol, OUTCOME_REJECTED)
            else:
                task.approval_decision = None
                resolved_store.update(
                    task.task_id,
                    state=TASK_INPUT_REQUIRED,
                    message=f"{APPROVAL_PENDING_TEXT} ({failure})",
                    metadata={
                        **request.to_metadata(),
                        "a2at-sample/approval-error": failure,
                    },
                )
                metrics.outcome(protocol, OUTCOME_INVALID)
            return _approval_response(task, stream=stream)
        task.approval_decision = decision.to_dict()
        if decision.outcome == OUTCOME_REJECT:
            task.approval_request = None
            resolved_store.update(
                task.task_id,
                state=TASK_REJECTED,
                message=decision.reason or "The plan was rejected.",
                metadata={APPROVAL_METADATA_KEY: task.approval_decision},
            )
            metrics.outcome(protocol, OUTCOME_REJECTED)
            return _approval_response(task, stream=stream)
        plan = task.approval_plan if isinstance(task.approval_plan, Mapping) else {}
        task.approval_request = None
        intent = intent_from_plan(plan)
        hold = config.hold_working and task.pure_hold
        if stream:
            return _stream_completion(task, intent, hold=hold)
        return _json_completion(
            task, intent, hold=hold, push=registry.for_task(task.task_id)
        )

    def _finish_reject(
        session: NegotiationSession,
        *,
        stream: bool,
        context_id: str,
    ) -> Response:
        """End a negotiation with ``TASK_STATE_REJECTED`` (no Task is created)."""
        metrics.outcome(PROTOCOL_A2A_T, OUTCOME_REJECTED)
        task = resolved_store.get(session.task_id) if session.task_id else None
        negotiations.close(session)
        if task is not None:
            resolved_store.update(task.task_id, state=TASK_REJECTED, message="The negotiation was rejected.")
            if stream:
                return StreamingResponse(
                    status_stream(task, config=config, keep_open=False),
                    media_type=EVENT_STREAM,
                    headers={"Cache-Control": "no-cache"},
                )
            return JSONResponse({"task": task.to_dict(embedded=True)}, status_code=200)
        message = negotiation_message(
            context_id=context_id,
            body="The negotiation was rejected.",
            metadata={},
        )
        if stream:
            return StreamingResponse(
                _single_message_stream(message),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse({"message": message}, status_code=200)

    def _finish_abort(
        session: NegotiationSession,
        *,
        stream: bool,
        context_id: str,
    ) -> Response:
        """End a negotiation that exhausted its round budget."""
        metrics.outcome(PROTOCOL_A2A_T, OUTCOME_ABORTED)
        task = resolved_store.get(session.task_id) if session.task_id else None
        context = negotiations.context(session)
        metadata = abort_metadata(context)
        negotiations.close(session)
        if task is not None:
            resolved_store.update(
                task.task_id,
                state=TASK_REJECTED,
                message="The negotiation exceeded its round budget.",
            )
        message = negotiation_message(context_id=context_id, body="", metadata=metadata)
        if stream:
            return StreamingResponse(
                _single_message_stream(message),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse({"message": message}, status_code=200)

    def _negotiated_request(
        body: Mapping[str, object],
        request: Request,
        parsed_context: NegotiationContext,
        *,
        stream: bool,
    ) -> Response:
        """Handle a message that answers an open negotiation."""
        session, expired = negotiations.lookup(parsed_context.id)
        if session is None and expired:
            # The exchange was open and is now out of budget. Answering "unknown id" would make a
            # late client retry forever; the exchange is over, so the client must start again.
            return negotiation_error(
                f"negotiationContext.id '{parsed_context.id}' has expired; start a new request"
            )
        if session is None:
            # The real NMS rejects a negotiation id it does not own; using the A2A context id as
            # the negotiation id is the concrete mistake this reproduces.
            return negotiation_error(
                f"negotiationContext.id '{parsed_context.id}' does not match an active negotiation; "
                "use the negotiation id returned by the server, not the A2A context id"
            )
        try:
            content = read_message_request(body, request.headers)
        except SimRequestError as exc:
            return JSONResponse(exc.payload, status_code=exc.status_code)
        _count_read_path(content)
        context_id = _context_id(body) or session.context_id
        if parsed_context.performative == PERFORMATIVE_REJECT:
            return _finish_reject(session, stream=stream, context_id=context_id)
        if parsed_context.performative == PERFORMATIVE_ABORT:
            return _finish_abort(session, stream=stream, context_id=context_id)
        request_config = PushConfig.from_request(body)
        if parsed_context.performative == PERFORMATIVE_ACCEPT:
            return _finish_accept(session, content, body, request_config, stream=stream)
        effective = content.merged_with(session.baseline)
        issue = select_issue(effective, config)
        if issue is None:
            return _finish_accept(session, content, body, request_config, stream=stream)
        if session.round >= session.max_rounds:
            return _finish_abort(session, stream=stream, context_id=context_id)
        negotiations.advance(session, kind=issue.kind)
        task = resolved_store.get(session.task_id) if session.task_id else None
        return _propose(
            session,
            issue,
            structured=declares_negotiation(request.headers),
            task=task,
            stream=stream,
            context_id=context_id,
            request_config=request_config,
            explicit_task=bool(_requested_task_id(body)),
        )

    # ---------------------------------------------------------------- change order (P3)

    def _count_read_path(content: ExtractedContent) -> None:
        """Record whether the business values came from data or from parsed prose."""
        metrics.path(
            PROTOCOL_A2A_T, "typed" if content.source.startswith("typed:") else "text"
        )

    def _request_identity(metadata: Mapping[str, object]) -> str | None:
        """Return the identity of the request.

        A client may state it explicitly (``a2at-sample/request-id``); that is how it asks for a
        *new* execution of the same agreement. Otherwise it is computed from the document: the
        typed statement, or the rendered Task-T as a fallback, so a retry after a crash (new
        message id, no Task) still names the same request.
        """
        explicit = metadata.get(REQUEST_ID_METADATA_KEY)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        try:
            statement = read_statement(metadata)
        except ValueError:
            return None
        if statement is not None:
            return canonical_digest(statement_metadata(statement))
        prompt = metadata.get(TASK_T_EXTENSION_URI)
        if isinstance(prompt, str) and prompt.strip():
            return canonical_digest({"taskT": prompt})
        return None

    def _has_task_payload(metadata: Mapping[str, object]) -> bool:
        """Return whether the request carries its own Task-T document or statement."""
        return (
            TASK_T_EXTENSION_URI in metadata
            or TYPED_TASK_T_METADATA_KEY in metadata
        )

    def _declared_hops(metadata: Mapping[str, object]) -> tuple[str, ...]:
        """Return the hop names the request declares, in either wire form.

        A carrying hop writes the v2 envelope; a hand-written request still uses a bare list. Both
        are read through :func:`shared.assurance.read_hops`, so the server never has to know which
        producer it is talking to.
        """
        return tuple(hop.hop for hop in read_hops(metadata.get(DELEGATION_METADATA_KEY)))

    def _delegation_chain(metadata: Mapping[str, object]) -> tuple[str, ...]:
        """Return the hops the request declares, plus this server."""
        hops = _declared_hops(metadata)
        return (*hops, DELEGATION_HOP) if hops else ()

    def _remember_plan(
        task: SimTask, intent: ParsedIntent, metadata: Mapping[str, object]
    ) -> None:
        """Record the plan and the agreement revision this Task now represents.

        Re-accepting the same plan keeps the revision (an idempotent restatement); a different plan
        bumps it. That is what makes a change order expressible as "target revision N".

        The state this revision replaces is captured here, before it is overwritten: a fresh Create
        has none, so its reversal is the baseline. A restatement keeps the original revision and,
        with it, the original reversal -- a restatement is not a new promise.
        """
        previous_plan = dict(task.plan) if task.plan is not None else None
        plan = dict(plan_for(intent))
        previous = (
            read_agreement({AGREEMENT_METADATA_KEY: task.agreement})
            if task.agreement is not None
            else None
        )
        expires_at = None
        if config.agreement_ttl_seconds is not None:
            expires_at = (
                (datetime.now(UTC) + timedelta(seconds=config.agreement_ttl_seconds))
                .isoformat()
                .replace("+00:00", "Z")
            )
        reversal = restoration_for(
            plan, previous_plan, baseline_rate=config.restore_rate_mbps
        )
        agreement = next_revision(
            previous,
            plan=plan,
            scopes=({"window": intent.window, "rateMbps": intent.rate_mbps},),
            expires_at=expires_at,
            reversal=reversal.to_dict(),
        )
        document = agreement.to_dict()
        task.plan = plan
        task.agreement = document
        # Read the reversal back off the stored revision: on a restatement that is the original
        # one, not the freshly computed value, so the Task executes what the agreement promises.
        task.reversal = dict(agreement.reversal) if agreement.reversal is not None else None
        resolved.agreements[agreement.agreement_id] = document
        resolved.revisions.setdefault(agreement.agreement_id, {})[agreement.revision] = document
        identity = _request_identity(metadata)
        if identity is not None:
            task.request_id = identity
            resolved.executions.setdefault(identity, task.task_id)
        if not task.execution_id:
            task.execution_id = f"exe-{uuid.uuid4().hex[:12]}"
        task.delegation = _declared_hops(metadata)
        task.delegation_chain = _delegation_chain(metadata)

    def _agreement_for(ref: object) -> tuple[dict[str, object] | None, str | None]:
        """Return the stored revision a client reference names, or the reason it cannot run."""
        agreement_id = getattr(ref, "agreement_id", "")
        revision = getattr(ref, "revision", None)
        raw = resolved.agreements.get(str(agreement_id))
        if raw is None:
            return None, AGREEMENT_UNKNOWN
        if revision is not None and raw.get("revision") != revision:
            return None, AGREEMENT_STALE
        expires = raw.get("expiresAt")
        if isinstance(expires, str):
            try:
                instant = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                # An unreadable lifetime cannot be honoured; authority fails closed.
                return None, AGREEMENT_EXPIRED
            if instant.tzinfo is None:
                # A stored instant without an offset can only mean this host's clock.
                instant = instant.astimezone()
            if datetime.now(UTC) >= instant:
                return None, AGREEMENT_EXPIRED
        return raw, None

    def _agreement_error(reason: str) -> JSONResponse:
        """Refuse a request that names an agreement revision it cannot execute."""
        return JSONResponse(
            {"error": {"code": 409, "status": "FAILED_PRECONDITION", "message": reason}},
            status_code=409,
        )

    def _content_from_agreement(raw: Mapping[str, object]) -> ExtractedContent:
        """Return the business content of an agreement revision, so it can be executed again."""
        scopes = raw.get("scopes")
        windows: list[str] = []
        rate: float | None = None
        if isinstance(scopes, list):
            for scope in scopes:
                if not isinstance(scope, Mapping):
                    continue
                window = scope.get("window")
                if isinstance(window, str):
                    windows.append(window)
                value = scope.get("rateMbps")
                if rate is None and isinstance(value, (int, float)) and not isinstance(value, bool):
                    rate = float(value)
        return ExtractedContent(
            operation=str(raw.get("operation") or "Create"),
            region=str(raw.get("region") or ""),
            windows=tuple(windows),
            rate_mbps=rate,
            percent=None,
            source=f"agreement:{raw.get('agreementId')}@{raw.get('revision')}",
        )

    def _baseline_rejection(
        task: SimTask,
        metadata: Mapping[str, object],
        *,
        protocol: str,
        stream: bool,
    ) -> Response | None:
        """Refuse a change order that names no baseline, or names a superseded one.

        ``baseline`` is the observable behaviour (``required`` turns the check on).
        ``baseline_stale`` is a measurement switch: the Task's current plan is reported as a
        different one, standing in for a second operator who changed it first.
        """
        if config.baseline != BASELINE_REQUIRED or task.plan is None:
            return None
        declared = read_baseline(metadata)
        expected_plan: Mapping[str, object] = task.plan
        if config.baseline_stale:
            expected_plan = {**expected_plan, "rateMbps": _other_rate(expected_plan.get("rateMbps"))}
        if declared is None:
            return _refuse_change_order(
                task,
                REASON_MISSING,
                "A change order must name the plan it replaces.",
                protocol=protocol,
                stream=stream,
            )
        current = (
            read_agreement({AGREEMENT_METADATA_KEY: task.agreement})
            if task.agreement is not None
            else None
        )
        if declared.agreement_id is not None and (
            current is None or current.agreement_id != declared.agreement_id
        ):
            return _refuse_change_order(
                task,
                AGREEMENT_UNKNOWN,
                "The change order names an agreement this Task does not execute.",
                protocol=protocol,
                stream=stream,
            )
        if declared.revision is not None and (
            current is None or current.revision != declared.revision
        ):
            return _refuse_change_order(
                task,
                AGREEMENT_STALE,
                "The change order was written against a superseded revision.",
                protocol=protocol,
                stream=stream,
            )
        if declared.plan_digest != plan_digest(expected_plan):
            return _refuse_change_order(
                task,
                REASON_STALE,
                "The plan this change order was written against is no longer current.",
                protocol=protocol,
                stream=stream,
            )
        return None

    def _refuse_change_order(
        task: SimTask,
        code: str,
        message: str,
        *,
        protocol: str,
        stream: bool,
    ) -> Response:
        """End a Task whose change order could not be bound to the current plan."""
        metrics.outcome(protocol, OUTCOME_REJECTED)
        resolved_store.update(
            task.task_id,
            state=TASK_REJECTED,
            message=message,
            metadata={BASELINE_ERROR_METADATA_KEY: {"reasonCode": code}},
        )
        if protocol == PROTOCOL_A2A:
            return _pure_task_response(task, stream=stream)
        return _approval_response(task, stream=stream)

    # ---------------------------------------------------------------- A2A-T

    def _a2a_t_request(body: Mapping[str, object], request: Request, *, stream: bool) -> Response:
        """Handle one Task-T request (initial or negotiation reply)."""
        # A Task already owns its callback: a second registration is rejected before the request
        # content is even judged, matching the vendor's "config already exists" answer.
        early_task_id = _requested_task_id(body)
        early_config = PushConfig.from_request(body)
        if early_config is not None and early_task_id and registry.for_task(early_task_id) is not None:
            return _register_config_error(PushConfigExistsError(early_task_id))
        metadata = _message_metadata(body)
        raw_context = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
        if raw_context is not None:
            parsed_context = NegotiationContext.from_metadata(metadata)
            if parsed_context is None:
                return negotiation_error(
                    "negotiationContext must carry an id, a round, maxRounds and a performative"
                )
            return _negotiated_request(body, request, parsed_context, stream=stream)
        # An agreement reference asks to execute a revision the server already holds. It is checked
        # before the document is read: a reference may replace the document entirely.
        agreement_ref = read_agreement_ref(metadata)
        agreement_raw: dict[str, object] | None = None
        if agreement_ref is not None:
            agreement_raw, reason = _agreement_for(agreement_ref)
            if reason is not None:
                return _agreement_error(reason)
        # A change order against a Task that already has a plan must name the plan it replaces.
        # Negotiation replies are dispatched above, so this only sees a fresh request.
        baseline_task = resolved_store.get(early_task_id) if early_task_id else None
        if baseline_task is not None:
            refused = _baseline_rejection(
                baseline_task, metadata, protocol=PROTOCOL_A2A_T, stream=stream
            )
            if refused is not None:
                return refused
        from_agreement = agreement_raw is not None and not _has_task_payload(metadata)
        if from_agreement:
            # Executing a stored revision needs no re-render: the agreement *is* the statement.
            content = _content_from_agreement(agreement_raw)
            metrics.path(PROTOCOL_A2A_T, "agreement")
        else:
            try:
                content = read_message_request(body, request.headers)
            except SimRequestError as exc:
                return JSONResponse(exc.payload, status_code=exc.status_code)
            _count_read_path(content)
        if agreement_raw is not None and _has_task_payload(metadata):
            declared = plan_digest(
                {
                    "operation": content.operation or "",
                    "region": content.region,
                    "window": content.windows[0] if content.windows else "",
                    "rateMbps": content.rate_mbps if content.rate_mbps is not None else 0.0,
                }
            )
            if declared != agreement_raw.get("planDigest"):
                return _agreement_error(AGREEMENT_MISMATCH)
        issue = select_issue(content, config)
        context_id = _context_id(body) or str(uuid.uuid4())
        requested_task_id = _requested_task_id(body)
        request_config = PushConfig.from_request(body)
        if issue is None:
            # A retry names the same request: return the Task that already answers it instead of
            # executing the plan a second time. A continuation names a Task and is not a retry.
            identity = _request_identity(metadata)
            if identity is not None and not requested_task_id:
                known = resolved.executions.get(identity)
                existing = resolved_store.get(known) if known else None
                if existing is not None:
                    metrics.duplicate(PROTOCOL_A2A_T)
                    return _approval_response(existing, stream=stream)
            task, is_new = _resolve_task(body, context_id=context_id)
            try:
                push = _push_config_for(
                    task,
                    request_config,
                    explicit_task=bool(requested_task_id),
                    context_id=context_id,
                )
            except PushConfigExistsError as exc:
                return _register_config_error(exc)
            # A reference request carries no document to parse: the agreement supplied the values.
            intent = (
                accepted_intent(content, None, config)
                if from_agreement
                else parse_message_request(body, request.headers, config)
            )
            if agreement_raw is not None and from_agreement:
                # Executing a revision is a continuation of that revision, not a new promise: seed
                # the agreement so the Task is bound to the id/revision the client named. Without
                # this, the response mints a fresh agreement and the reference means nothing.
                task.agreement = dict(agreement_raw)
            _remember_plan(task, intent, metadata)
            metrics.outcome(PROTOCOL_A2A_T, OUTCOME_ACCEPTED)
            hold = config.hold_working and is_new
            if config.approval:
                return _request_approval(task, intent, stream=stream, hold=hold)
            if stream:
                return _stream_completion(task, intent, hold=hold)
            return _json_completion(task, intent, hold=hold, push=push)
        task = resolved_store.get(requested_task_id) if requested_task_id else None
        if task is None and request_config is not None and config.negotiation_carrier != CARRIER_TASK:
            registry.remember_pending(context_id, request_config)
        session = _new_negotiation(
            content,
            issue,
            context_id=context_id,
            task_id=task.task_id if task is not None else None,
            structured=declares_negotiation(request.headers),
        )
        if task is not None and declares_negotiation(request.headers) and config.negotiation_carrier == CARRIER_TASK:
            session.task_id = task.task_id
        return _propose(
            session,
            issue,
            structured=declares_negotiation(request.headers),
            task=task,
            stream=stream,
            context_id=context_id,
            request_config=request_config,
            explicit_task=bool(requested_task_id),
        )

    # ---------------------------------------------------------------- plain A2A

    def _pure_task_response(task: SimTask, *, stream: bool) -> Response:
        """Return one terminal plain-A2A Task; a stream always closes at a terminal state."""
        if stream:
            return StreamingResponse(
                status_stream(task, config=config, keep_open=False),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse({"task": task.to_dict(embedded=True)}, status_code=200)

    def _pure_request(
        body: Mapping[str, object],
        request: Request,
        *,
        stream: bool,
    ) -> Response:
        """Handle one plain A2A request: ask for missing values and resume on the follow-up."""
        message = body.get("message")
        message_map = message if isinstance(message, Mapping) else {}
        text = message_text(message_map)
        requested_task_id = _requested_task_id(body)
        context_id = _context_id(body) or str(uuid.uuid4())
        request_config = PushConfig.from_request(body)
        # A plain question is a standalone Message, so the follow-up carries only the context id.
        task = (
            resolved_store.get(requested_task_id)
            if requested_task_id
            else resolved_store.find_by_context(context_id)
        )
        # A Task in INPUT_REQUIRED is waiting for the answer to the question it just asked. Any
        # other state (including a held WORKING Task) means this message starts a new exchange,
        # e.g. a standard-A2A Modify. That distinction is what keeps the Modify's own outage
        # window from being merged into the Create the baseline was built from.
        is_answer = task is not None and task.state == TASK_INPUT_REQUIRED
        if task is None:
            task = resolved_store.create(context_id=context_id)
            task.pure_text = text
            task.pure_baseline_text = text
            task.pure_hold = config.hold_working
        elif is_answer:
            task.pure_text = f"{task.pure_text}\n{text}".strip()
            task.pure_round += 1
            resolved_store.update(task.task_id, state=TASK_SUBMITTED, message="Task creation succeeded")
        else:
            task.pure_text = text
            task.pure_baseline_text = text
            task.pure_round = 0
            task.pure_hold = False
            resolved_store.update(task.task_id, state=TASK_SUBMITTED, message="Task creation succeeded")
        task.pure_max_rounds = config.max_rounds
        # A plain-A2A change order is checked the same way. The plain client has no accepted
        # document to derive a baseline from, so with ``baseline=required`` it cannot pass.
        if requested_task_id and not is_answer:
            refused = _baseline_rejection(
                task, _message_metadata(body), protocol=PROTOCOL_A2A, stream=stream
            )
            if refused is not None:
                return refused
        try:
            push = _push_config_for(
                task,
                request_config,
                explicit_task=bool(requested_task_id),
                context_id=context_id,
            )
        except PushConfigExistsError as exc:
            return _register_config_error(exc)
        # Only the latest reply is judged; the request of this exchange supplies the fields it did
        # not repeat. Concatenating and parsing the whole conversation would let the first mention
        # of a field win, so a corrected region/rate in the answer would be ignored.
        baseline = extract_content(task.pure_baseline_text)
        content = extract_content(text).merged_with(baseline) if is_answer else baseline
        if is_answer and PURE_REJECTION.search(text):
            # The vendor skill terminates on a clear refusal. Without this the simulator would
            # treat the client's "Reject: ..." as another answer and keep asking until the round
            # budget ran out, leaving the flow without a terminal state.
            metrics.outcome(PROTOCOL_A2A, OUTCOME_REJECTED)
            resolved_store.update(task.task_id, state=TASK_REJECTED, message=text)
            return _pure_task_response(task, stream=stream)
        if config.pure_question == PURE_QUESTION_CONFIRM and not is_answer:
            # The vendor's plain-A2A front end asks the same terse sentence whatever the business
            # issue is; the caller's free-text answer is merged over this request and re-validated.
            # The prose stays that sentence even under ``missing_items``: the envelope is the only
            # variable the E1 comparison changes.
            return _pure_question_response(
                task,
                PURE_CONFIRM_TEXT,
                stream=stream,
                issue=select_issue(content, config),
            )
        issue = select_issue(content, config, effective_rate_only=is_answer)
        if issue is not None:
            return _pure_issue(task, issue, content, stream=stream, is_answer=is_answer)
        metrics.outcome(PROTOCOL_A2A, OUTCOME_ACCEPTED)
        intent = accepted_intent(content, None, config)
        _remember_plan(task, intent, _message_metadata(body))
        if config.approval:
            return _request_approval(task, intent, stream=stream, hold=task.pure_hold)
        if stream:
            # The pure stream owns the same rehearsal switch as the webhook path: a held Task must
            # publish its report and stay WORKING so the Modify picker can select it.
            return _stream_completion(task, intent, hold=task.pure_hold)
        return _json_completion(task, intent, hold=task.pure_hold, push=push)

    def _pure_issue(
        task: SimTask,
        issue: NegotiationIssue,
        content: ExtractedContent,
        *,
        stream: bool,
        is_answer: bool,
    ) -> Response:
        """Answer a plain-A2A request that cannot be executed yet.

        The initial request gets a vendor text question. A follow-up that still cannot be served
        follows the configured ``pure_failure`` policy: the vendor answers ``TASK_STATE_COMPLETED``
        with an error sentence and no artifact -- a refusal that must not be reported as success;
        ``retry`` asks again until the round budget; ``rejected`` ends the Task.
        """
        if is_answer and config.pure_failure == PURE_FAILURE_COMPLETED:
            metrics.outcome(PROTOCOL_A2A, OUTCOME_REFUSED)
            resolved_store.update(
                task.task_id,
                state=TASK_COMPLETED,
                message=_pure_failure_text(issue, content),
            )
            return _pure_task_response(task, stream=stream)
        if is_answer and config.pure_failure == PURE_FAILURE_REJECTED:
            metrics.outcome(PROTOCOL_A2A, OUTCOME_REJECTED)
            resolved_store.update(task.task_id, state=TASK_REJECTED, message=issue.body())
            return _pure_task_response(task, stream=stream)
        if task.pure_round >= task.pure_max_rounds:
            metrics.outcome(PROTOCOL_A2A, OUTCOME_ABORTED)
            resolved_store.update(
                task.task_id,
                state=TASK_REJECTED,
                message=f"Negotiation did not converge: {issue.body()}",
            )
            return _pure_task_response(task, stream=stream)
        if config.pure_question == PURE_QUESTION_CONFIRM:
            # The vendor's front end never renders the business issue: every round is the same
            # terse sentence, and the answer is merged over the request before re-validating.
            return _pure_question_response(task, PURE_CONFIRM_TEXT, stream=stream, issue=issue)
        return _pure_question_response(task, issue.body(), stream=stream, issue=issue)

    def _pure_question_response(
        task: SimTask,
        text: str,
        *,
        stream: bool,
        issue: NegotiationIssue | None = None,
    ) -> Response:
        """Ask one plain-A2A question as a standalone agent Message.

        The real NMS asks a plain-A2A question as a standalone message with no negotiation
        metadata, even when it also marks its internal Task ``INPUT_REQUIRED``. Returning a Task
        status here would make the OSS answer it itself instead of relaying the question.
        """
        metadata: dict[str, object] = (
            missing_items_metadata(issue, config) if config.missing_items else {}
        )
        # The negotiation kind stays the business kind; the shape is a separate axis so an E1 run
        # does not look like a different kind of negotiation.
        metrics.question(
            PROTOCOL_A2A,
            issue.kind if issue is not None else "unknown",
            shape="items" if metadata else "prose",
        )
        metrics.round(PROTOCOL_A2A)
        resolved_store.update(task.task_id, state=TASK_INPUT_REQUIRED, message=text)
        if stream:
            return StreamingResponse(
                message_stream(
                    text,
                    context_id=task.context_id,
                    config=config,
                    structured=False,
                    metadata=metadata or None,
                ),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache"},
            )
        return JSONResponse(
            {
                "message": negotiation_message(
                    context_id=task.context_id,
                    body=text,
                    metadata=metadata,
                )
            },
            status_code=200,
        )

    def _is_pure_request(body: Mapping[str, object], request: Request) -> bool:
        """Return whether the message is plain A2A (no Task-T / Negotiation-T extension)."""
        message = body.get("message")
        if not isinstance(message, Mapping):
            return False
        metadata = message.get("metadata")
        metadata_map = metadata if isinstance(metadata, Mapping) else {}
        if extension_header_value(request.headers):
            return False
        return (
            TASK_T_EXTENSION_URI not in metadata_map
            and NEGOTIATION_T_EXTENSION_URI not in metadata_map
        )

    # ---------------------------------------------------------------- routes

    async def _handle(request: Request, *, stream: bool) -> Response:
        """Authorize, parse and dispatch one request on either transport."""
        if not authorized(request):
            return unauthorized()
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        pure = _is_pure_request(body, request)
        protocol = PROTOCOL_A2A if pure else PROTOCOL_A2A_T
        metrics.request(protocol)
        # An approval decision is a normal message to an existing Task on both protocols; dispatch
        # it before the negotiation/question handling so it cannot be read as another answer.
        decision = read_approval_decision(_message_metadata(body))
        if decision is not None:
            return _handle_approval_decision(
                body, decision, stream=stream, protocol=protocol
            )
        try:
            if pure:
                return _pure_request(body, request, stream=stream)
            return _a2a_t_request(body, request, stream=stream)
        except CanonicalFormError as exc:
            # A value the canonical profile cannot represent is a malformed contract, not a server
            # fault: reject it as a client error so the peer learns why instead of seeing a 500.
            return JSONResponse(
                {"error": "canonical.invalid", "detail": str(exc)}, status_code=400
            )

    async def send_message(request: Request) -> Response:
        """Accept one A2A message and stream the Task or negotiation message."""
        return await _handle(request, stream=True)

    async def send_message_json(request: Request) -> Response:
        """Accept one A2A message, answer its first event and push the rest."""
        return await _handle(request, stream=False)

    async def list_tasks(request: Request) -> Response:
        """List the latest Tasks."""
        if not authorized(request):
            return unauthorized()
        return JSONResponse({"tasks": resolved_store.list()}, status_code=200)

    async def get_task(request: Request) -> Response:
        """Return one Task."""
        if not authorized(request):
            return unauthorized()
        task = resolved_store.get(str(request.path_params["task_id"]))
        if task is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return JSONResponse({"task": task.to_dict()}, status_code=200)

    async def cancel_task(request: Request) -> Response:
        """Cancel one non-terminal Task, executing its reversal when it is aborted.

        ``cancel_supported=False`` reproduces the vendor's older HTTP 503 answer; otherwise a
        terminal Task is returned unchanged so a repeated cancel stays idempotent.

        An abort (the default reason) also restores the state the agreement was accepted over.
        That restoration is published as an artifact instead of being left to the client: only
        then can a third party compare it with the reversal the agreement carried. A Task whose
        agreement has no reversal, or a cancel with another reason, keeps the old behaviour.
        """
        if not authorized(request):
            return unauthorized()
        if not config.cancel_supported:
            return JSONResponse(
                {"error": {"code": 503, "message": "Task cancellation is unavailable"}},
                status_code=503,
            )
        reason = await _cancel_reason(request)
        task_id = str(request.path_params["task_id"])
        task = resolved_store.get(task_id)
        if task is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        if task.state not in TERMINAL_STATES:
            resolved_store.update(task_id, state=TASK_CANCELED, message="Task canceled.")
        if reason == CANCEL_REASON_ABORT:
            _restore_task(task)
        return JSONResponse({"task": task.to_dict()}, status_code=200)

    def _restore_task(task: SimTask) -> None:
        """Execute the reversal the Task's agreement carries, at most once."""
        if task.reversal is None:
            return
        artifact_id = _restoration_artifact_id(task.task_id)
        if any(artifact.get("artifactId") == artifact_id for artifact in task.artifacts):
            # A second cancel is the same abort; it must not restore twice.
            return
        reversal = read_reversal({REVERSAL_METADATA_KEY: task.reversal})
        if reversal is None:
            return
        resolved_store.add_artifact(task.task_id, _restoration_artifact(task, reversal))

    async def list_agreements(request: Request) -> Response:
        """Return the current revision of every agreement this simulator holds.

        The agreement is the durable object; a listing makes "what did we agree to" answerable
        without walking Tasks. It reads the runtime's own table rather than the Task list: Tasks
        are evicted (``task_limit``), and an agreement that is still executable must not vanish
        from the listing because the Task that produced it aged out. One entry per agreement id.
        """
        if not authorized(request):
            return unauthorized()
        entries = [
            _agreement_listing(document)
            for _, document in sorted(resolved.agreements.items())
        ]
        return JSONResponse({"agreements": entries}, status_code=200)

    async def get_agreement(request: Request) -> Response:
        """Return every revision of one agreement, newest first.

        A superseded revision is still evidence: the client that signed it needs to see what it
        was, and why a reference to it can no longer run.
        """
        if not authorized(request):
            return unauthorized()
        agreement_id = str(request.path_params["agreement_id"])
        current = resolved.agreements.get(agreement_id)
        if current is None:
            return JSONResponse({"reasonCode": AGREEMENT_UNKNOWN}, status_code=404)
        revisions = resolved.revisions.get(agreement_id, {})
        documents = [
            dict(revisions[revision]) for revision in sorted(revisions, reverse=True)
        ] or [dict(current)]
        revision = current.get("revision")
        return JSONResponse(
            {
                "agreementId": agreement_id,
                "currentRevision": (
                    revision
                    if isinstance(revision, int) and not isinstance(revision, bool)
                    else 0
                ),
                "revisions": documents,
            },
            status_code=200,
        )

    async def list_negotiations(request: Request) -> Response:
        """Return the open negotiations, optionally filtered by ``contextId``.

        A negotiation that has not created a Task yet is the one state a client cannot recover from
        the Task list. Exposing it makes a lost ``negotiationContext.id`` findable instead of
        orphaned (S7).
        """
        if not authorized(request):
            return unauthorized()
        context_id = request.query_params.get("contextId")
        return JSONResponse(
            {"negotiations": negotiations.snapshot(context_id=context_id)}, status_code=200
        )

    async def server_metrics(request: Request) -> Response:
        """Return the server-side work counters of this simulator process."""
        del request
        return JSONResponse(metrics.snapshot(), status_code=200)

    routes: list[BaseRoute] = [
        Route("/rest/plat/smapp/v1/sessions", sessions, methods=["PUT"]),
        Route("/a2a/json/metrics", server_metrics, methods=["GET"]),
        Route("/a2a/json/message:stream", send_message, methods=["POST"]),
        Route("/a2a/json/message:send", send_message_json, methods=["POST"]),
        Route("/a2a/json/tasks", list_tasks, methods=["GET"]),
        Route("/a2a/json/negotiations", list_negotiations, methods=["GET"]),
        Route("/a2a/json/agreements", list_agreements, methods=["GET"]),
        Route("/a2a/json/agreements/{agreement_id}", get_agreement, methods=["GET"]),
        Route("/a2a/json/tasks/{task_id}", get_task, methods=["GET"]),
        Route("/a2a/json/tasks/{task_id}:cancel", cancel_task, methods=["POST"]),
    ]
    return routes, resolved


async def _single_message_stream(message: Mapping[str, object]) -> AsyncIterator[str]:
    """Yield one ``message`` SSE frame and close."""
    yield f"data: {json.dumps({'message': dict(message)}, ensure_ascii=False)}\n\n"


def _agreement_listing(document: Mapping[str, object]) -> dict[str, object]:
    """Project one stored revision onto the listing shape."""
    revision = document.get("revision")
    entry: dict[str, object] = {
        "agreementId": str(document.get("agreementId") or ""),
        "revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else 0,
        "planDigest": str(document.get("planDigest") or ""),
        "operation": str(document.get("operation") or ""),
        "region": str(document.get("region") or ""),
    }
    expires = document.get("expiresAt")
    if isinstance(expires, str):
        entry["expiresAt"] = expires
    return entry


def _other_rate(value: object) -> float:
    """Return a rate that differs from the plan's, for measurement switches."""
    rate = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    return rate + 1.0


def _readback_for(
    intent: ParsedIntent, config: SimConfig, task: SimTask | None = None
) -> dict[str, object]:
    """Build the S8 readback envelope of one executed plan.

    The executed plan is the accepted one; ``readback_deviation`` is a measurement switch that
    reports a different rate, standing in for a network that applied something else.
    """
    planned = plan_for(intent)
    executed = dict(planned)
    if config.readback_deviation:
        rate = planned.get("rateMbps")
        executed["rateMbps"] = (
            round(float(rate) * 0.8, 2)
            if isinstance(rate, (int, float)) and not isinstance(rate, bool)
            else 0.0
        )
    agreement = (
        read_agreement({AGREEMENT_METADATA_KEY: task.agreement})
        if task is not None and task.agreement is not None
        else None
    )
    return Readback(
        plan_digest=plan_digest(planned),
        executed=executed,
        deviations=compare_plans(planned, executed),
        execution_id=task.execution_id if task is not None else "",
        request_id=task.request_id if task is not None else "",
        agreement_id=agreement.agreement_id if agreement is not None else "",
        revision=agreement.revision if agreement is not None else 0,
        delegation_chain=task.delegation_chain if task is not None else (),
    ).to_metadata()


def _restoration_artifact_id(task_id: str) -> str:
    """Return the artifact id of one Task's reversal execution."""
    return f"{task_id}-restoration"


def _restoration_artifact(task: SimTask, reversal: Reversal) -> dict[str, object]:
    """Build the artifact proving one reversal was executed.

    It reuses the S8 readback envelope (and its metadata key), so a client that already reads the
    report artifact needs no second reader: ``restoredFrom`` names the plan that was undone, and
    ``planDigest``/``executed`` name the one that replaced it.
    """
    aborted = dict(task.plan or {})
    restored = dict(reversal.plan)
    readback = Readback(
        plan_digest=plan_digest(restored),
        executed=restored,
        deviations=compare_plans(aborted, restored),
        restored_from=plan_digest(aborted),
    )
    return {
        "artifactId": _restoration_artifact_id(task.task_id),
        "name": "energy saving reversal report",
        "parts": [
            {"data": restored},
            {
                "text": (
                    f"Executed {OPERATION_RESTORE}: the plan in force before the abort "
                    f"({restored.get('window') or 'the window'} at "
                    f"{restored.get('rateMbps') or 0} Mbps) is back."
                )
            },
        ],
        "metadata": {"taskId": task.task_id, **readback.to_metadata()},
    }


async def _cancel_reason(request: Request) -> str:
    """Return the cancel reason, ``abort`` when the request states none.

    A cancel request historically carried no body, so an empty or unreadable body must keep
    meaning "abort" instead of silently turning the cancel into one that restores nothing.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return CANCEL_REASON_ABORT
    if not isinstance(body, Mapping):
        return CANCEL_REASON_ABORT
    reason = body.get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else CANCEL_REASON_ABORT


def _pure_failure_text(issue: NegotiationIssue, content: ExtractedContent) -> str:
    """Render the vendor-style failure sentence of one unservable plain-A2A request."""
    if "Energy saving region" in issue.items:
        if content.region:
            return (
                f'The region name "{content.region}" is not recognized. '
                "Please provide the correct region name."
            )
        return (
            "The energy saving region is not recognized. "
            "Please provide the correct region name."
        )
    return issue.detail or issue.body()


def _context_id(body: Mapping[str, object]) -> str | None:
    """Return the message context id, when present."""
    message = body.get("message")
    if not isinstance(message, Mapping):
        return None
    for key in ("contextId", "context_id"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _message_metadata(body: Mapping[str, object]) -> Mapping[str, object]:
    """Return the metadata map of one request's message."""
    message = body.get("message")
    if not isinstance(message, Mapping):
        return {}
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _json_compact(payload: Mapping[str, object]) -> str:
    """Render the vendor's compact structured suffix."""
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


__all__ = ["SimRuntime", "build_routes", "build_runtime"]
