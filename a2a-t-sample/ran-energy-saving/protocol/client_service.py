"""Client application service: conversations, runs and negotiation audit.

The Web UI, the CLI and the A2A gateway are inbound adapters over one application service. The
service owns the run lifecycle, conversation memory and persistence; it never depends on Starlette.

This module started as the Web UI's ``RunState``/``RunManager`` pair. It now also:

* persists conversations, turns, runs and negotiation events through :class:`ClientStore`;
* enforces one active run per conversation (different conversations may run concurrently);
* injects the recent conversation turns into the client decision prompt and Task-T generation;
* marks runs left ``running`` by a previous process as ``interrupted`` on startup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

from a2a_t.core.metadata import NegotiationContext
from shared.config import DISCOVERY_REGISTRY, SampleConfig
from shared.logging_setup import log_context
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.trace import (
    PHASE_DISPATCH,
    PHASE_DONE,
    Span,
    TraceRecorder,
    infer_decision_phase,
    infer_event_phase,
)

from protocol.client_flow import ClientFlowResult, DecisionMadeCallback
from protocol.client_runtime import MODE_AUTO, MODE_MANUAL
from protocol.client_store import (
    NEGOTIATION_DIRECTION_INBOUND,
    NEGOTIATION_DIRECTION_OUTBOUND,
    RUN_STATUS_RUNNING,
    TURN_ROLE_ASSISTANT,
    TURN_ROLE_USER,
    ClientRunRecord,
    ClientStore,
    NegotiationEventRecord,
    TaskSnapshotRecord,
    now_iso,
)
from protocol.decision_provider import sweep_stale_sessions
from protocol.run_spec import RunCatalog
from protocol.transcript import DEFAULT_RUNS_DIR, NegotiationTranscript, TranscriptPaths

_LOGGER = logging.getLogger("ran_energy_saving.client_service")

#: Minimum interval between run-triggered decision-session sweeps (startup sweeps ignore it).
SESSION_SWEEP_INTERVAL_SECONDS: Final[float] = 3600.0


class ClientRuntimeLike(Protocol):
    """Runtime surface the Web UI drives (the real :class:`ClientRuntime` or a test double)."""

    manual_provider: Any | None

    async def run(
        self,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        on_decision_requested: Callable[[NegotiationProposeView, NegotiationContext], None] | None = None,
        on_decision_made: DecisionMadeCallback | None = None,
        trace_sink: Callable[[dict[str, object]], None] | None = None,
        run_id: str | None = None,
        user_request: str | None = None,
        conversation_context: str | None = None,
    ) -> ClientFlowResult: ...

    async def aclose(self) -> None: ...


RuntimeFactory = Callable[[SampleConfig, str, str, str, str], Awaitable[ClientRuntimeLike]]

#: Run statuses owned by the client service.
RunStatus = Literal["running", "finished", "failed", "interrupted"]
STATUS_RUNNING: Final[RunStatus] = "running"
STATUS_FINISHED: Final[RunStatus] = "finished"
STATUS_FAILED: Final[RunStatus] = "failed"
STATUS_INTERRUPTED: Final[RunStatus] = "interrupted"

#: Modes the Web UI may start; the scripted mode is a CLI/test seam only.
UI_MODES: Final[tuple[str, ...]] = (MODE_AUTO, MODE_MANUAL)


class RunConflictError(RuntimeError):
    """Raised when a run is already active."""


@dataclass
class RunState:
    """Mutable state of one Web-UI run.

    Attributes:
        run_id: Identifier returned by ``POST /api/runs``.
        scenario: scenario name.
        mode: decision mode (``auto`` / ``manual``).
        transport: event transport.
        user_request: optional natural-language request supplied by the UI; ``None`` uses the
            scenario default.
        status: ``running`` / ``finished`` / ``failed``.
        messages: every SSE message broadcast so far (replayed to late subscribers).
        subscribers: live SSE queues.
        transcript: transcript recorded while the run executes.
        runtime: assembled client runtime, once built.
        result: client flow result, once finished.
        paths: transcript file paths, once written.
        error: failure message of a failed run.
        transcript_error: failure message when the transcript could not be written.
        pending_decision: whether the manual provider currently waits for a decision.
        proposals: inbound proposals in order.
        discovery: discovery mode of the run.
        traces: every trace span produced/ingested for the run.
        spans: span lookup by id.
        task: background task of the run.
        finished_at: monotonic time the terminal frame was broadcast, when it is known.
        conversation_id: client conversation owning this run.
        external_conversation_id: caller-side conversation id (for example Gemini Enterprise).
        user_id: caller identity.
        idempotency_key: optional caller key preventing a duplicate run.
        conversation_context: rendered recent turns injected into prompts.
        store: optional client persistence store.
    """

    run_id: str
    scenario: str
    mode: str
    transport: str
    discovery: str = DISCOVERY_REGISTRY
    user_request: str | None = None
    status: RunStatus = STATUS_RUNNING
    conversation_id: str | None = None
    external_conversation_id: str | None = None
    user_id: str | None = None
    idempotency_key: str = ""
    conversation_context: str | None = None
    messages: list[dict[str, object]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, object]]] = field(default_factory=set)
    transcript: NegotiationTranscript | None = None
    runtime: ClientRuntimeLike | None = None
    result: ClientFlowResult | None = None
    paths: TranscriptPaths | None = None
    error: str | None = None
    transcript_error: str | None = None
    pending_decision: bool = False
    proposals: list[NegotiationProposeView] = field(default_factory=list)
    traces: list[dict[str, object]] = field(default_factory=list)
    spans: dict[str, dict[str, object]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    finished_at: float | None = None
    store: ClientStore | None = field(default=None, repr=False)
    _seq: int = field(default=0, repr=False)
    _phase_steps: dict[str, int] = field(default_factory=dict, repr=False)
    _span_id_map: dict[str, str] = field(default_factory=dict, repr=False)
    _producer_spans: dict[str, str] = field(default_factory=dict, repr=False)
    _nego_seq: int = field(default=0, repr=False)
    _user_turn_id: str | None = field(default=None, repr=False)
    _persist_tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)
    _recorder: TraceRecorder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the per-run span recorder (UI ids never collide with client-flow ids)."""
        self._recorder = TraceRecorder(self.run_id, id_prefix="ui")

    def next_negotiation_seq(self) -> int:
        """Allocate the next negotiation-event sequence number."""
        self._nego_seq += 1
        return self._nego_seq

    def persist(self, awaitable: Coroutine[Any, Any, object]) -> None:
        """Schedule one persistence operation without blocking the protocol loop.

        Persistence must never break a run, so the scheduled task swallows exceptions (the store
        already logs them) and the result is ignored. ``drain_persistence`` awaits every pending
        write before the run publishes its terminal frame.
        """
        if self.store is None:
            self._close_awaitable(awaitable)
            return
        try:
            task = asyncio.create_task(awaitable)
        except RuntimeError:
            # No running loop (synchronous test helper): the write is simply skipped.
            self._close_awaitable(awaitable)
            return
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)

    @staticmethod
    def _close_awaitable(awaitable: Coroutine[Any, Any, object]) -> None:
        """Close an unused coroutine without triggering a 'never awaited' warning."""
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()

    async def drain_persistence(self) -> None:
        """Wait for every scheduled persistence write of this run."""
        while self._persist_tasks:
            await asyncio.gather(*tuple(self._persist_tasks), return_exceptions=True)

    @property
    def manual_provider(self) -> Any | None:
        """The manual provider of the runtime, when the run uses one."""
        return self.runtime.manual_provider if self.runtime is not None else None

    def next_seq(self) -> int:
        """Allocate the next global ``seq`` without consuming a phase step."""
        self._seq += 1
        return self._seq

    def allocate(self, phase: str) -> tuple[int, int]:
        """Allocate the next global ``seq`` and the next step number of one phase."""
        seq = self.next_seq()
        step = self._phase_steps.get(phase, 0) + 1
        self._phase_steps[phase] = step
        return seq, step

    def broadcast(self, message: dict[str, object], *, seq: int | None = None) -> dict[str, object]:
        """Publish one SSE message with a strictly increasing ``seq``."""
        if seq is None:
            seq = self.next_seq()
        else:
            self._seq = max(self._seq, seq)
        message["seq"] = seq
        self.messages.append(message)
        for queue in tuple(self.subscribers):
            queue.put_nowait(message)
        return message

    def emit_run_started(
        self,
        *,
        description: str,
        user_request: str,
        language: str,
        model_mode: str,
        model_label: str,
        expected_sequence: list[dict[str, object]],
    ) -> dict[str, object]:
        """Broadcast the first frame of the run (replayed to every late subscriber)."""
        seq, step = self.allocate(PHASE_DISPATCH)
        detail: dict[str, object] = {
            "scenario": self.scenario,
            "description": description,
            "userRequest": user_request,
            "language": language,
            "mode": self.mode,
            "transport": self.transport,
            "discovery": self.discovery,
            "modelMode": model_mode,
            "modelLabel": model_label,
            "expectedSequence": expected_sequence,
        }
        span = self._recorder.emit(
            seq=seq,
            phase_step=step,
            kind="run_started",
            phase=PHASE_DISPATCH,
            title="Run started",
            actor="system",
            side="client",
            direction="internal",
            detail=detail,
        )
        self._store_span(span, seq=seq, record=True, broadcast_frame=False)
        message: dict[str, object] = {
            "type": "run_started",
            "runId": self.run_id,
            "scenario": self.scenario,
            "description": description,
            "userRequest": user_request,
            "language": language,
            "mode": self.mode,
            "transport": self.transport,
            "discovery": self.discovery,
            "modelMode": model_mode,
            "modelLabel": model_label,
            "expectedSequence": expected_sequence,
            "phase": PHASE_DISPATCH,
            "phaseStep": step,
            "spanId": span.span_id,
            "startedAt": span.started_at,
        }
        return self.broadcast(message, seq=seq)

    def emit_trace(
        self,
        *,
        kind: str,
        phase: str,
        title: str,
        direction: str = "internal",
        actor: str = "system",
        side: str = "client",
        status: str = "ok",
        round_number: int | None = None,
        context_id: str | None = None,
        task_id: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Create, store and broadcast one local synthetic trace span."""
        seq, step = self.allocate(phase)
        span = self._recorder.emit(
            seq=seq,
            phase_step=step,
            kind=kind,
            phase=phase,
            title=title,
            actor=actor,
            side=side,
            direction=direction,
            status=status,
            round=round_number,
            context_id=context_id,
            task_id=task_id,
            detail=detail,
        )
        return self._store_span(span, seq=seq, record=True)

    def record_trace(self, span: Mapping[str, object]) -> dict[str, object]:
        """Ingest one producer span (client flow or server HTTP transport) and broadcast it.

        A producer id that is empty or already taken by another span is remapped to a fresh UI id;
        the parent reference follows the same mapping so nested spans stay connected.

        A **re-sent** producer span (same original ``spanId``) is ignored: the first ingested copy
        wins and the stored span is returned unchanged, so a retried server batch never duplicates
        a timeline row. The server emitter retries whole batches, and ids are role-scoped
        (``llm_server_*`` / ``tool_server_*``), so ignoring repeats is safe and idempotent.
        """
        candidate = Span.from_dict(span)
        original_span_id = candidate.span_id
        if original_span_id:
            existing_span_id = self._producer_spans.get(original_span_id)
            if existing_span_id is not None:
                return self.spans.get(existing_span_id, {})
        phase = candidate.phase
        seq, step = self.allocate(phase)
        span_id: str | None = candidate.span_id or None
        if span_id is None or span_id in self.spans:
            if span_id is not None:
                span_id = self._recorder.new_span_id()
                self._span_id_map[candidate.span_id] = span_id
            else:
                span_id = None
        elif candidate.span_id:
            self._span_id_map.setdefault(candidate.span_id, candidate.span_id)
        parent_span_id = candidate.parent_span_id
        if parent_span_id is not None:
            parent_span_id = self._span_id_map.get(parent_span_id, parent_span_id)
        built = self._recorder.emit(
            seq=seq,
            phase_step=step,
            span_id=span_id,
            parent_span_id=parent_span_id,
            kind=candidate.kind,
            phase=phase,
            title=candidate.title,
            actor=candidate.actor,
            side=candidate.side,
            direction=candidate.direction,
            status=candidate.status,
            round=candidate.round,
            context_id=candidate.context_id,
            task_id=candidate.task_id,
            stage=candidate.stage,
            purpose=candidate.purpose,
            model=candidate.model,
            tokens=candidate.tokens,
            error=candidate.error,
            detail=candidate.detail,
            started_at=candidate.started_at,
            ended_at=candidate.ended_at,
            duration_ms=candidate.duration_ms,
        )
        self._producer_spans[original_span_id or built.span_id] = built.span_id
        return self._store_span(built, seq=seq, record=True)

    def _store_span(
        self,
        span: Span,
        *,
        seq: int,
        record: bool,
        broadcast_frame: bool = True,
    ) -> dict[str, object]:
        """Store one span for lookup/trace export, optionally add a transcript row and broadcast."""
        payload = span.to_dict()
        self.traces.append(payload)
        self.spans[span.span_id] = payload
        if record and self.transcript is not None:
            self.transcript.record_trace(payload, seq=seq, phase_step=span.phase_step)
        if broadcast_frame:
            self.broadcast({"type": "trace", "event": payload}, seq=seq)
        return payload

    def record_event(self, event: Mapping[str, object]) -> dict[str, object]:
        """Record one normalized A2A event, its span and its SSE frame."""
        if self.transcript is None:
            return {}
        phase = infer_event_phase(event)
        seq, step = self.allocate(phase)
        entry = self.transcript.record_event(event, phase=phase, phase_step=step, seq=seq)
        round_number = entry.get("round")
        context_value = entry.get("contextId")
        span = self._recorder.emit(
            seq=seq,
            phase_step=step,
            kind=str(event.get("kind") or "event"),
            phase=phase,
            title=str(entry.get("summary") or event.get("kind") or "event"),
            actor="server",
            side="server",
            direction="server->client",
            round=round_number if isinstance(round_number, int) else None,
            context_id=context_value if isinstance(context_value, str) else None,
            task_id=str(event.get("task_id") or "") or None,
            detail={"event": dict(event)},
        )
        entry["spanId"] = span.span_id
        self._store_span(span, seq=seq, record=False, broadcast_frame=False)
        self.broadcast({"type": "event", "event": entry}, seq=seq)
        self._persist_task_snapshot(event)
        return entry

    def _persist_task_snapshot(self, event: Mapping[str, object]) -> None:
        """Upsert the last observed Task state of this run."""
        if self.store is None:
            return
        task_id = _string_value(event.get("task_id"))
        if task_id is None:
            return
        self.persist(
            self.store.save_task_snapshot(
                TaskSnapshotRecord(
                    run_id=self.run_id,
                    task_id=task_id,
                    context_id=_string_value(event.get("context_id")),
                    state=_string_value(event.get("state")),
                    payload=dict(event),
                )
            )
        )

    def record_proposal(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> None:
        """Persist one inbound proposal in the negotiation audit."""
        if self.store is None:
            return
        self.persist(
            self.store.record_negotiation_event(
                NegotiationEventRecord.new(
                    run_id=self.run_id,
                    seq=self.next_negotiation_seq(),
                    direction=NEGOTIATION_DIRECTION_INBOUND,
                    actor="server",
                    negotiation_id=propose.negotiation_id,
                    kind=propose.kind,
                    round=session.round,
                    performative=session.performative.value,
                    content={
                        "content": propose.content,
                        "templateUri": propose.template_uri,
                    },
                )
            )
        )

    def record_decision(
        self,
        decision: NegotiationDecision,
        propose: NegotiationProposeView | None,
        *,
        session: NegotiationContext | None = None,
    ) -> dict[str, object]:
        """Record one provider/human decision live and broadcast it with its span."""
        if self.transcript is None:
            return {}
        phase = infer_decision_phase(decision.action)
        seq, step = self.allocate(phase)
        entry = self.transcript.record_decision(
            decision,
            propose=propose,
            phase=phase,
            phase_step=step,
            seq=seq,
        )
        span = self._recorder.emit(
            seq=seq,
            phase_step=step,
            kind=decision.action,
            phase=phase,
            title=f"Decision · {decision.action}",
            actor="human" if self.mode == MODE_MANUAL else "client",
            side="client",
            direction="internal",
            status="ok",
            round=session.round if session is not None else (propose.round if propose is not None else None),
            context_id=propose.context_id if propose is not None else None,
            detail={
                "action": decision.action,
                "rationale": decision.rationale,
                "filledParams": dict(decision.filled_params) if decision.filled_params else None,
                "counterProposal": decision.counter_proposal,
                "counterParams": dict(decision.counter_params) if decision.counter_params else None,
                "confirmedIntent": decision.confirmed_intent,
                "feasibilitySummary": decision.feasibility_summary,
            },
        )
        entry["spanId"] = span.span_id
        self._store_span(span, seq=seq, record=False, broadcast_frame=False)
        self.broadcast({"type": "event", "event": entry}, seq=seq)
        self._persist_decision(decision, propose, session)
        return entry

    def _persist_decision(
        self,
        decision: NegotiationDecision,
        propose: NegotiationProposeView | None,
        session: NegotiationContext | None,
    ) -> None:
        """Persist one outbound decision in the negotiation audit."""
        if self.store is None:
            return
        self.persist(
            self.store.record_negotiation_event(
                NegotiationEventRecord.new(
                    run_id=self.run_id,
                    seq=self.next_negotiation_seq(),
                    direction=NEGOTIATION_DIRECTION_OUTBOUND,
                    actor="human" if self.mode == MODE_MANUAL else "client",
                    negotiation_id=(
                        propose.negotiation_id
                        if propose is not None
                        else (session.id if session is not None else None)
                    ),
                    kind=propose.kind if propose is not None else None,
                    round=(
                        session.round
                        if session is not None
                        else (propose.round if propose is not None else None)
                    ),
                    performative=_performative_for_decision(decision.action),
                    content={
                        "action": decision.action,
                        "filledParams": (
                            dict(decision.filled_params) if decision.filled_params else None
                        ),
                        "counterProposal": decision.counter_proposal,
                        "counterParams": (
                            dict(decision.counter_params) if decision.counter_params else None
                        ),
                        "confirmedIntent": decision.confirmed_intent,
                        "feasibilitySummary": decision.feasibility_summary,
                    },
                    decision_action=decision.action,
                    rationale=decision.rationale,
                )
            )
        )

    def emit_finished(self) -> dict[str, object]:
        """Broadcast the terminal frame and record its ``done`` trace row."""
        seq, step = self.allocate(PHASE_DONE)
        span = self._recorder.emit(
            seq=seq,
            phase_step=step,
            kind="finished",
            phase=PHASE_DONE,
            title="Run finished",
            actor="system",
            side="client",
            direction="internal",
            status="error" if self.status == STATUS_FAILED else "ok",
            context_id=self.result.context_id if self.result is not None else None,
            task_id=self.result.task_id if self.result is not None else None,
            error=self.error,
            detail={
                "finalState": self.result.final_state if self.result is not None else None,
                "report": self.result.report if self.result is not None else None,
            },
        )
        self._store_span(span, seq=seq, record=True, broadcast_frame=False)
        message = self._finished_message()
        message.update(
            {
                "phase": PHASE_DONE,
                "phaseStep": step,
                "spanId": span.span_id,
                # The finished row starts when the terminal frame is built, which is what lets the
                # Done phase render a duration instead of a null one.
                "startedAt": span.started_at,
            }
        )
        self.finished_at = time.monotonic()
        return self.broadcast(message, seq=seq)

    def subscribe(self) -> tuple[list[dict[str, object]], asyncio.Queue[dict[str, object]]]:
        """Return the replay snapshot and the live queue of one new subscriber atomically."""
        snapshot = list(self.messages)
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.subscribers.add(queue)
        return snapshot, queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        """Remove one SSE subscriber queue."""
        self.subscribers.discard(queue)

    def finished(self) -> bool:
        """Whether the run reached a terminal status."""
        return self.status != STATUS_RUNNING

    def _finished_message(self) -> dict[str, object]:
        """Build the terminal SSE message of this run."""
        return {
            "type": "finished",
            "runId": self.run_id,
            "conversationId": self.conversation_id,
            "status": self.status,
            "finalState": self.result.final_state if self.result is not None else None,
            "report": self.result.report if self.result is not None else None,
            "error": self.error,
            "transcriptJson": f"/api/runs/{self.run_id}/transcript.json",
            "transcriptMarkdown": f"/api/runs/{self.run_id}/transcript.md",
            "traceJson": f"/api/runs/{self.run_id}/trace.json",
        }

    def snapshot(self) -> dict[str, object]:
        """Return the JSON status/result/transcript document of this run."""
        transcript = self.transcript.to_dict() if self.transcript is not None else None
        return {
            "runId": self.run_id,
            "conversationId": self.conversation_id,
            "externalConversationId": self.external_conversation_id,
            "userId": self.user_id,
            "idempotencyKey": self.idempotency_key,
            "scenario": self.scenario,
            "mode": self.mode,
            "transport": self.transport,
            "discovery": self.discovery,
            "status": self.status,
            "pendingDecision": self.pending_decision,
            "finalState": self.result.final_state if self.result is not None else None,
            "contextId": self.result.context_id if self.result is not None else None,
            "taskId": self.result.task_id if self.result is not None else None,
            "report": self.result.report if self.result is not None else None,
            "error": self.error,
            "transcriptError": self.transcript_error,
            "seq": self._seq,
            "traceCount": len(self.traces),
            "transcript": transcript,
        }


class RunManager:
    """Owns client runs and conversations of one process.

    One conversation has at most one active run; different conversations may run concurrently.
    When a :class:`ClientStore` is configured, every lifecycle transition is persisted, the recent
    turns are injected into the next run, and a duplicate request carrying the same idempotency key
    returns the existing run instead of starting a second one.
    """

    def __init__(
        self,
        *,
        config: SampleConfig,
        runtime_factory: RuntimeFactory,
        catalog: RunCatalog,
        runs_dir: Path | None = None,
        store: ClientStore | None = None,
        memory_turns: int = 6,
    ) -> None:
        self._config = config
        self._runtime_factory = runtime_factory
        self._catalog = catalog
        self._runs_dir = runs_dir
        self._store = store
        self._memory_turns = max(0, memory_turns)
        self._runs: dict[str, RunState] = {}
        self._active_by_conversation: dict[str, RunState] = {}
        self._last_session_sweep = 0.0

    @property
    def store(self) -> ClientStore | None:
        """Return the configured client store."""
        return self._store

    def get(self, run_id: str) -> RunState | None:
        """Return one run by id, ``None`` when unknown."""
        return self._runs.get(run_id)

    def active_for_conversation(self, conversation_id: str) -> RunState | None:
        """Return the active run of one conversation, when there is one."""
        run = self._active_by_conversation.get(conversation_id)
        return run if run is not None and not run.finished() else None

    async def mark_interrupted(self) -> int:
        """Mark runs left ``running`` by a previous process as ``interrupted``."""
        if self._store is None:
            return 0
        return await self._store.mark_running_interrupted()

    async def start(
        self,
        scenario: str,
        mode: str,
        transport: str,
        discovery: str = DISCOVERY_REGISTRY,
        user_request: str | None = None,
        *,
        conversation_id: str | None = None,
        external_conversation_id: str | None = None,
        user_id: str = "web-user",
        idempotency_key: str | None = None,
    ) -> RunState:
        """Start one background run and broadcast ``run_started`` before the task is scheduled.

        ``user_request`` overrides the scenario's canned request when the caller supplied one.

        Args:
            scenario: scenario name.
            mode: decision mode.
            transport: event transport.
            discovery: discovery mode.
            user_request: optional natural-language request.
            conversation_id: client conversation; generated when omitted.
            external_conversation_id: caller-side conversation id (Gemini Enterprise, console).
            user_id: caller identity.
            idempotency_key: optional key that deduplicates a repeated request.

        Raises:
            RunConflictError: when the same conversation already has an active run.
        """
        resolved_conversation = conversation_id or f"conv-{uuid.uuid4().hex}"
        active = self.active_for_conversation(resolved_conversation)
        if active is not None:
            raise RunConflictError(
                f"Conversation {resolved_conversation} already has an active run "
                f"({active.run_id}); wait for it to finish."
            )

        resolved_key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if self._store is not None and resolved_key:
            existing = await self._store.find_run_by_idempotency(resolved_conversation, resolved_key)
            if existing is not None:
                reused = self._reuse_run(existing)
                self._runs[reused.run_id] = reused
                return reused

        custom_request = user_request.strip() if isinstance(user_request, str) else None
        if not custom_request:
            custom_request = None
        run = RunState(
            run_id=uuid.uuid4().hex,
            scenario=scenario,
            mode=mode,
            transport=transport,
            discovery=discovery,
            user_request=custom_request,
            conversation_id=resolved_conversation,
            external_conversation_id=external_conversation_id,
            user_id=user_id,
            idempotency_key=resolved_key,
            store=self._store,
        )
        run.transcript = NegotiationTranscript(
            scenario=scenario,
            language=self._config.language,
            model_mode=self._config.model_mode,
            mode=mode,
            transport=transport,
            runs_dir=self._runs_dir or DEFAULT_RUNS_DIR,
        )
        spec = self._catalog.spec(scenario, language=self._config.language)
        run.emit_run_started(
            description=spec.description,
            user_request=custom_request or spec.user_request,
            language=self._config.language,
            model_mode=self._config.model_mode,
            model_label=self._config.model_label,
            expected_sequence=[
                {"actor": step.actor, "performative": step.performative, "round": step.round}
                for step in spec.expected_sequence
            ],
        )
        self._runs[run.run_id] = run
        self._active_by_conversation[resolved_conversation] = run
        run.task = asyncio.create_task(
            self._execute_in_context(run), name=f"ran-client-run-{run.run_id}"
        )
        return run

    async def _execute_in_context(self, run: RunState) -> None:
        """Run one client run with its correlation fields bound for every log record."""
        with log_context(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            role="client",
        ):
            _LOGGER.info(
                "run started: scenario=%s mode=%s transport=%s",
                run.scenario,
                run.mode,
                run.transport,
            )
            started = time.monotonic()
            try:
                await self._execute(run)
            finally:
                _LOGGER.info(
                    "run finished: status=%s final_state=%s task_id=%s duration=%.2fs",
                    run.status,
                    run.result.final_state if run.result is not None else "-",
                    run.result.task_id if run.result is not None and run.result.task_id else "-",
                    time.monotonic() - started,
                )

    def _reuse_run(self, record: ClientRunRecord) -> RunState:
        """Build a read-only run view for an idempotent replay."""
        run = RunState(
            run_id=record.run_id,
            scenario=record.scenario,
            mode=record.mode,
            transport=record.transport,
            discovery=record.discovery,
            user_request=record.user_request,
            status=cast(RunStatus, record.status),
            conversation_id=record.conversation_id,
            idempotency_key=record.idempotency_key,
            store=self._store,
        )
        if record.status != STATUS_RUNNING:
            run.finished_at = time.monotonic()
        return run

    async def sweep_sessions(self, *, force: bool = True) -> None:
        """Delete decision sessions past the retention window.

        Called at startup (``force``) and before a run. A run is the only thing that grows the
        session table, so a run-triggered sweep is enough for a long-lived process; the hourly
        guard keeps it from scanning the table on every run.
        """
        now = time.monotonic()
        if not force and now - self._last_session_sweep < SESSION_SWEEP_INTERVAL_SECONDS:
            return
        self._last_session_sweep = now
        await sweep_stale_sessions(
            self._config.client_db_path,
            retention_days=self._config.adk_session_retention_days,
        )

    async def _execute(self, run: RunState) -> None:
        """Build the runtime, run the client flow, persist the outcome and publish the result."""
        transcript = run.transcript
        assert transcript is not None
        await self.sweep_sessions(force=False)
        await self._prepare_persistence(run)

        def event_sink(event: dict[str, object]) -> None:
            run.record_event(event)

        def on_decision_requested(propose: NegotiationProposeView, session: NegotiationContext) -> None:
            # The proposals are recorded in both modes because the finished run pairs them with the
            # provider decisions; a manual decision panel only exists in manual mode.
            run.record_proposal(propose, session)
            run.proposals.append(propose)
            if run.mode != MODE_MANUAL:
                return
            run.pending_decision = True
            timeout_seconds = getattr(run.manual_provider, "timeout_seconds", None)
            run.broadcast(
                {
                    "type": "decision_requested",
                    "mode": run.mode,
                    "proposal": propose.model_dump(),
                    "round": session.round,
                    "maxRounds": session.max_rounds,
                    "performative": session.performative.value,
                    "phase": "negotiation",
                    # Tiny UI-support fields: the manual countdown would otherwise have to invent
                    # the provider budget, and a replay after a reload would restart it.
                    "timeoutSeconds": (
                        float(timeout_seconds)
                        if isinstance(timeout_seconds, (int, float))
                        else None
                    ),
                    "requestedAt": now_iso(),
                }
            )

        def on_decision_made(
            decision: NegotiationDecision,
            propose: NegotiationProposeView,
            session: NegotiationContext,
        ) -> None:
            run.record_decision(decision, propose, session=session)

        def trace_sink(span: dict[str, object]) -> None:
            run.record_trace(span)

        try:
            run.runtime = await self._runtime_factory(
                self._config,
                run.scenario,
                run.mode,
                run.transport,
                run.discovery,
            )
            run.emit_trace(
                kind="runtime_ready",
                phase=PHASE_DISPATCH,
                title="Client runtime ready",
                detail={
                    "mode": run.mode,
                    "transport": run.transport,
                    "discovery": run.discovery,
                    "language": self._config.language,
                    "modelMode": self._config.model_mode,
                    "modelLabel": self._config.model_label,
                },
            )
            discovered = getattr(run.runtime, "discovered", None)
            card = getattr(discovered, "agent_card", None)
            run.emit_trace(
                kind="discovery",
                phase=PHASE_DISPATCH,
                title="Server discovered",
                detail={
                    "discovery": run.discovery,
                    "url": getattr(discovered, "url", None),
                    "agentName": getattr(card, "name", None),
                },
            )
            result = await run.runtime.run(
                event_sink=event_sink,
                on_decision_requested=on_decision_requested,
                on_decision_made=on_decision_made,
                trace_sink=trace_sink,
                run_id=run.run_id,
                user_request=run.user_request,
                conversation_context=run.conversation_context,
            )
            run.result = result
            # Decisions are normally recorded live by ``on_decision_made``; the late loop is only
            # a fallback for a runtime double that does not call the hook.
            for index, decision in enumerate(result.decisions):
                if index < len(transcript.decisions):
                    continue
                propose = run.proposals[index] if index < len(run.proposals) else None
                run.record_decision(decision, propose)
            transcript.record_final(result)
            run.status = STATUS_FINISHED
        except asyncio.CancelledError:
            run.status = STATUS_INTERRUPTED
            run.error = "The run was cancelled because the process is shutting down."
            transcript.record_error(run.error)
            raise
        except Exception as exc:  # noqa: BLE001 - a failed run must reach the UI, not a bare task
            _LOGGER.exception("client run %s failed", run.run_id)
            run.status = STATUS_FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            transcript.record_error(run.error)
        finally:
            run.pending_decision = False
            if run.runtime is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await run.runtime.aclose()
                # The runtime owns the ADK runner and its decision sessions; a finished run keeps
                # only the transcript, traces and result, so a long-lived UI/gateway process does
                # not accumulate one agent history per run.
                run.runtime = None
            try:
                run.paths = transcript.write(self._runs_dir)
            except OSError as exc:
                run.transcript_error = f"{type(exc).__name__}: {exc}"
            await run.drain_persistence()
            await self._finalize_persistence(run)
            self._active_by_conversation.pop(run.conversation_id or "", None)
            run.emit_finished()

    async def _prepare_persistence(self, run: RunState) -> None:
        """Create the conversation, load memory, append the user turn and persist the run row."""
        if self._store is None or run.conversation_id is None:
            return
        request_text = run.user_request or self._catalog.spec(
            run.scenario, language=self._config.language
        ).user_request
        await self._store.ensure_conversation(
            conversation_id=run.conversation_id,
            external_id=run.external_conversation_id or "",
            user_id=run.user_id or "",
            title=request_text,
        )
        if self._memory_turns > 0:
            turns = await self._store.list_recent_turns(run.conversation_id, self._memory_turns)
            run.conversation_context = _format_conversation_context(turns) or None
        user_turn = await self._store.append_turn(
            conversation_id=run.conversation_id,
            role=TURN_ROLE_USER,
            content=request_text,
            run_id=run.run_id,
        )
        if user_turn is not None:
            run._user_turn_id = user_turn.turn_id
        await self._store.create_run(
            ClientRunRecord(
                run_id=run.run_id,
                conversation_id=run.conversation_id,
                turn_id=run._user_turn_id,
                scenario=run.scenario,
                mode=run.mode,
                transport=run.transport,
                discovery=run.discovery,
                user_request=request_text,
                status=RUN_STATUS_RUNNING,
                owner_pid=os.getpid(),
                idempotency_key=run.idempotency_key,
            )
        )

    async def _finalize_persistence(self, run: RunState) -> None:
        """Persist the terminal run status and the assistant turn."""
        if self._store is None or run.conversation_id is None:
            return
        if run.status == STATUS_RUNNING:
            run.status = STATUS_INTERRUPTED
        result = run.result
        await self._store.update_run(
            run.run_id,
            status=run.status,
            final_state=result.final_state if result is not None else None,
            server_context_id=result.context_id if result is not None else None,
            server_task_id=result.task_id if result is not None else None,
            error=run.error,
            transcript_json=str(run.paths.json_path) if run.paths is not None else None,
            transcript_markdown=(
                str(run.paths.markdown_path) if run.paths is not None else None
            ),
            finished_at=now_iso(),
        )
        await self._store.append_turn(
            conversation_id=run.conversation_id,
            role=TURN_ROLE_ASSISTANT,
            content=_run_summary(run),
            run_id=run.run_id,
        )

    async def list_runs(
        self,
        limit: int = 50,
        conversation_id: str | None = None,
    ) -> list[ClientRunRecord]:
        """Return persisted runs, newest first."""
        if self._store is None:
            return []
        return await self._store.list_runs(limit, conversation_id)

    async def shutdown(self) -> None:
        """Cancel every still-running background run (app shutdown)."""
        tasks = [
            run.task
            for run in self._active_by_conversation.values()
            if run.task is not None and not run.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*tasks, return_exceptions=True)


def _string_value(value: object) -> str | None:
    """Return a non-empty string value, ``None`` otherwise."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _performative_for_decision(action: str) -> str:
    """Map one decision action to its Negotiation-T performative name."""
    return {
        "accept": "ACCEPT",
        "counter": "PROPOSE",
        "reject": "REJECT",
        "abort": "ABORT",
    }.get(action, action.upper())


def _format_conversation_context(turns: list[Any]) -> str:
    """Render recent turns as one delimited prompt block."""
    lines: list[str] = []
    total = 0
    for turn in turns:
        content = " ".join(str(turn.content).split())
        if len(content) > 500:
            content = content[:499] + "…"
        line = f"{turn.role}: {content}"
        if total + len(line) > 4000:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _run_summary(run: RunState) -> str:
    """Build the assistant turn recorded for one finished run."""
    if run.error:
        return f"Run failed: {run.error}"
    if run.result is None:
        return f"Run finished with status {run.status}."
    report = run.result.report
    if isinstance(report, Mapping):
        summary = report.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return f"Run completed with final state {run.result.final_state}."
