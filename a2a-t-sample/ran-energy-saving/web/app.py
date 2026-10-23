"""Lightweight Web UI of the RAN energy-saving client (design §10).

The Starlette app observes and steers the client flow, it never re-implements protocol logic:

* ``GET /`` serves the single page (native JS + ``EventSource``) and ``/static`` its assets;
* ``POST /api/runs`` starts one background client run (one concurrent run, a second start is 409);
* ``GET /api/runs/{id}/events`` streams the normalized events, ``decision_requested`` prompts and
  the ``finished`` outcome as Server-Sent Events;
* ``POST /api/runs/{id}/decisions`` submits a :class:`~shared.models.NegotiationDecision` to the
  manual provider (404 for an unknown run, 409 when no decision is pending);
* ``GET /api/runs/{id}`` returns the status, result and transcript;
* ``GET /api/runs/{id}/transcript.json`` / ``.md`` download the run transcript.

The language is process-level and read-only: it comes from the sample configuration and is exposed
through ``GET /api/meta`` (design D18).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from a2a_t.core.metadata import NegotiationContext
from protocol.client_flow import ClientFlowResult, DecisionMadeCallback
from protocol.client_runtime import MODE_AUTO, MODE_MANUAL, build_client_runtime
from protocol.transcript import DEFAULT_RUNS_DIR, NegotiationTranscript, TranscriptPaths
from shared.config import (
    DISCOVERY_REGISTRY,
    SUPPORTED_DISCOVERY_MODES,
    SUPPORTED_TRANSPORTS,
    SampleConfig,
)
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import SUPPORTED_SCENARIO_NAMES, get_scenario, resolve_user_request
from shared.trace import (
    PHASE_DISPATCH,
    PHASE_DONE,
    SCHEMA_VERSION,
    Span,
    TraceRecorder,
    infer_decision_phase,
    infer_event_phase,
)
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

#: Static directory of the single page.
STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"

#: Run statuses.
RunStatus = Literal["running", "finished", "failed"]
STATUS_RUNNING: Final[RunStatus] = "running"
STATUS_FINISHED: Final[RunStatus] = "finished"
STATUS_FAILED: Final[RunStatus] = "failed"

#: Modes the Web UI may start; the scripted mode is a CLI/test seam only.
UI_MODES: Final[tuple[str, ...]] = (MODE_AUTO, MODE_MANUAL)


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
    ) -> ClientFlowResult: ...

    async def aclose(self) -> None: ...


RuntimeFactory = Callable[[SampleConfig, str, str, str, str], Awaitable[ClientRuntimeLike]]


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
    """

    run_id: str
    scenario: str
    mode: str
    transport: str
    discovery: str = DISCOVERY_REGISTRY
    status: RunStatus = STATUS_RUNNING
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
    _seq: int = field(default=0, repr=False)
    _phase_steps: dict[str, int] = field(default_factory=dict, repr=False)
    _recorder: TraceRecorder = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the per-run span recorder."""
        self._recorder = TraceRecorder(self.run_id)

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
        """Ingest one producer span (client flow or server HTTP transport) and broadcast it."""
        candidate = Span.from_dict(span)
        phase = candidate.phase
        seq, step = self.allocate(phase)
        built = self._recorder.emit(
            seq=seq,
            phase_step=step,
            span_id=candidate.span_id or None,
            parent_span_id=candidate.parent_span_id,
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
            self.transcript.record_trace(payload, seq=seq)
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
        return entry

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
        return entry

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
        message.update({"phase": PHASE_DONE, "phaseStep": step, "spanId": span.span_id})
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
    """Owns the runs of one app instance (one concurrent run)."""

    def __init__(
        self,
        *,
        config: SampleConfig,
        runtime_factory: RuntimeFactory,
        runs_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._runtime_factory = runtime_factory
        self._runs_dir = runs_dir
        self._runs: dict[str, RunState] = {}
        self._active: RunState | None = None

    def get(self, run_id: str) -> RunState | None:
        """Return one run by id, ``None`` when unknown."""
        return self._runs.get(run_id)

    def start(
        self,
        scenario: str,
        mode: str,
        transport: str,
        discovery: str = DISCOVERY_REGISTRY,
    ) -> RunState:
        """Start one background run and broadcast ``run_started`` before the task is scheduled.

        Raises:
            RunConflictError: when another run is still active.
        """
        if self._active is not None and not self._active.finished():
            raise RunConflictError("Another run is still active; wait for it to finish.")
        run = RunState(
            run_id=uuid.uuid4().hex,
            scenario=scenario,
            mode=mode,
            transport=transport,
            discovery=discovery,
        )
        run.transcript = NegotiationTranscript(
            scenario=scenario,
            language=self._config.language,
            model_mode=self._config.model_mode,
            mode=mode,
            transport=transport,
            runs_dir=self._runs_dir or DEFAULT_RUNS_DIR,
        )
        spec = get_scenario(scenario)
        run.emit_run_started(
            description=spec.description,
            user_request=resolve_user_request(spec, self._config.language),
            language=self._config.language,
            model_mode=self._config.model_mode,
            model_label=self._config.model_label,
            expected_sequence=[
                {"actor": step.actor, "performative": step.performative, "round": step.round}
                for step in spec.expected_sequence
            ],
        )
        self._runs[run.run_id] = run
        self._active = run
        run.task = asyncio.create_task(self._execute(run), name=f"ran-web-run-{run.run_id}")
        return run

    async def _execute(self, run: RunState) -> None:
        """Build the runtime, run the client flow, write the transcript and publish the outcome."""
        transcript = run.transcript
        assert transcript is not None

        def event_sink(event: dict[str, object]) -> None:
            run.record_event(event)

        def on_decision_requested(propose: NegotiationProposeView, session: NegotiationContext) -> None:
            # The proposals are recorded in both modes because the finished run pairs them with the
            # provider decisions; a manual decision panel only exists in manual mode.
            run.proposals.append(propose)
            if run.mode != MODE_MANUAL:
                return
            run.pending_decision = True
            run.broadcast(
                {
                    "type": "decision_requested",
                    "mode": run.mode,
                    "proposal": propose.model_dump(),
                    "round": session.round,
                    "maxRounds": session.max_rounds,
                    "performative": session.performative.value,
                    "phase": "negotiation",
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
        except Exception as exc:  # noqa: BLE001 - a failed run must reach the UI, not a bare task
            run.status = STATUS_FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            transcript.record_error(run.error)
        finally:
            run.pending_decision = False
            if run.runtime is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await run.runtime.aclose()
            try:
                run.paths = transcript.write(self._runs_dir)
            except OSError as exc:
                run.transcript_error = f"{type(exc).__name__}: {exc}"
            self._active = None
            run.emit_finished()

    async def shutdown(self) -> None:
        """Cancel a still-running background run (app shutdown)."""
        active = self._active
        if active is None or active.task is None or active.task.done():
            return
        active.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await active.task


def _resolve_default(
    value: str | None,
    allowed: tuple[str, ...],
    fallback: str,
    label: str,
) -> str:
    """Resolve one Web-UI selection default, rejecting an invalid explicit value.

    Raises:
        ValueError: when an explicit default is not one of ``allowed``.
    """
    if value is None:
        return fallback
    if value not in allowed:
        raise ValueError(f"Unsupported default {label}: {value}; expected one of {allowed}")
    return value


def create_app(
    config: SampleConfig,
    *,
    runs_dir: Path | None = None,
    runtime_factory: RuntimeFactory | None = None,
    env_path: Path | None = None,
    default_scenario: str | None = None,
    default_mode: str | None = None,
    default_transport: str | None = None,
    default_discovery: str | None = None,
) -> Starlette:
    """Build the Starlette application of the Web UI.

    Args:
        config: process-level sample configuration (language and model mode are read-only).
        runs_dir: transcript directory; defaults to the sample ``runs/`` directory.
        runtime_factory: optional runtime builder (tests inject a deterministic double).
        env_path: source ``.env`` handed to the real client runtime.
        default_scenario: scenario preselected by the page and used when a start body omits it;
            defaults to the first supported scenario.
        default_mode: decision mode preselected/used by default; defaults to ``auto``.
        default_transport: transport preselected/used by default; defaults to the process-level
            ``A2AT_SAMPLE_TRANSPORT`` value.
        default_discovery: discovery preselected/used by default; defaults to the process-level
            ``A2AT_SAMPLE_DISCOVERY`` value.

    Returns:
        The assembled Starlette application.

    Raises:
        ValueError: when an explicit default is not a supported value.
    """
    resolved_default_scenario = _resolve_default(
        default_scenario, SUPPORTED_SCENARIO_NAMES, SUPPORTED_SCENARIO_NAMES[0], "scenario"
    )
    resolved_default_mode = _resolve_default(default_mode, UI_MODES, MODE_AUTO, "mode")
    config_transport = config.transport if config.transport in SUPPORTED_TRANSPORTS else SUPPORTED_TRANSPORTS[0]
    resolved_default_transport = _resolve_default(
        default_transport, SUPPORTED_TRANSPORTS, config_transport, "transport"
    )
    config_discovery = (
        config.discovery if config.discovery in SUPPORTED_DISCOVERY_MODES else SUPPORTED_DISCOVERY_MODES[0]
    )
    resolved_default_discovery = _resolve_default(
        default_discovery, SUPPORTED_DISCOVERY_MODES, config_discovery, "discovery"
    )

    async def default_factory(
        resolved_config: SampleConfig,
        scenario: str,
        mode: str,
        transport: str,
        discovery: str,
    ) -> ClientRuntimeLike:
        """Build the real client runtime with the app's source ``.env``."""
        return await build_client_runtime(
            replace(resolved_config, discovery=discovery),
            scenario,
            mode,
            transport,
            env_path=env_path,
        )

    manager = RunManager(
        config=config,
        runtime_factory=runtime_factory or default_factory,
        runs_dir=runs_dir,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        del app
        yield
        await manager.shutdown()

    async def index(request: Request) -> Response:
        del request
        return FileResponse(STATIC_DIR / "index.html")

    async def meta(request: Request) -> Response:
        del request
        return JSONResponse(
            {
                "language": config.language,
                "modelMode": config.model_mode,
                "modelLabel": config.model_label,
                "scenarios": list(SUPPORTED_SCENARIO_NAMES),
                "modes": list(UI_MODES),
                "transports": list(SUPPORTED_TRANSPORTS),
                "discoveries": list(SUPPORTED_DISCOVERY_MODES),
                "defaultScenario": resolved_default_scenario,
                "defaultMode": resolved_default_mode,
                "defaultTransport": resolved_default_transport,
                "defaultDiscovery": resolved_default_discovery,
            }
        )

    async def start_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)
        scenario = body.get("scenario", resolved_default_scenario)
        mode = body.get("mode", resolved_default_mode)
        transport = body.get("transport", resolved_default_transport)
        discovery = body.get("discovery", resolved_default_discovery)
        if scenario not in SUPPORTED_SCENARIO_NAMES:
            return JSONResponse(
                {"error": f"Unsupported scenario: {scenario}; expected one of {SUPPORTED_SCENARIO_NAMES}"},
                status_code=400,
            )
        if mode not in UI_MODES:
            return JSONResponse(
                {"error": f"Unsupported mode: {mode}; expected one of {UI_MODES}"},
                status_code=400,
            )
        if transport not in SUPPORTED_TRANSPORTS:
            return JSONResponse(
                {"error": f"Unsupported transport: {transport}; expected one of {SUPPORTED_TRANSPORTS}"},
                status_code=400,
            )
        if discovery not in SUPPORTED_DISCOVERY_MODES:
            return JSONResponse(
                {"error": f"Unsupported discovery: {discovery}; expected one of {SUPPORTED_DISCOVERY_MODES}"},
                status_code=400,
            )
        try:
            run = manager.start(str(scenario), str(mode), str(transport), str(discovery))
        except RunConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(
            {
                "runId": run.run_id,
                "language": config.language,
                "modelMode": config.model_mode,
                "modelLabel": config.model_label,
                "discovery": discovery,
            },
            status_code=202,
        )

    async def run_status(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        return JSONResponse(run.snapshot())

    async def run_events(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        snapshot, queue = run.subscribe()

        async def stream() -> AsyncIterator[dict[str, str]]:
            """Replay recorded messages, then forward live ones until the run finishes."""
            try:
                seen_finished = any(message.get("type") == "finished" for message in snapshot)
                for message in snapshot:
                    yield _sse_data(message)
                if run.finished() and seen_finished:
                    return
                while True:
                    message = await queue.get()
                    yield _sse_data(message)
                    if message.get("type") == "finished":
                        return
            finally:
                run.unsubscribe(queue)

        return EventSourceResponse(stream(), ping=15)

    async def submit_decision(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        try:
            body = await request.json()
            decision = NegotiationDecision.model_validate(body)
        except (ValueError, TypeError):
            return JSONResponse({"error": "The body must be a NegotiationDecision JSON object."}, status_code=400)
        provider = run.manual_provider
        if run.status != STATUS_RUNNING or not run.pending_decision or provider is None:
            return JSONResponse(
                {"error": "No manual decision is pending for this run."},
                status_code=409,
            )
        run.pending_decision = False
        provider.submit(decision)
        return JSONResponse({"accepted": True, "runId": run.run_id})

    async def transcript_json(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None or run.transcript is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        return Response(
            run.transcript.to_json(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{run.run_id}.json"'},
        )

    async def transcript_markdown(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None or run.transcript is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        return Response(
            run.transcript.to_markdown(),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{run.run_id}.md"'},
        )

    async def run_span(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        span = run.spans.get(request.path_params["span_id"])
        if span is None:
            return JSONResponse({"error": "Unknown span."}, status_code=404)
        return JSONResponse(span)

    async def run_trace_json(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        spans = sorted(run.spans.values(), key=_span_seq)
        return JSONResponse(
            {
                "schemaVersion": SCHEMA_VERSION,
                "runId": run.run_id,
                "scenario": run.scenario,
                "status": run.status,
                "spans": spans,
                "transcript": run.transcript.to_dict() if run.transcript is not None else None,
            }
        )

    async def ingest_trace(request: Request) -> Response:
        run = manager.get(request.path_params["run_id"])
        if run is None:
            return JSONResponse({"error": "Unknown run."}, status_code=404)
        if not _trace_token_ok(request):
            return JSONResponse({"error": "Invalid trace token."}, status_code=403)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The body must be a JSON span or span list."}, status_code=400)
        spans = _coerce_trace_payload(body)
        if spans is None:
            return JSONResponse({"error": "The body must be a JSON span or span list."}, status_code=400)
        for span in spans:
            run.record_trace(span)
        return JSONResponse({"accepted": len(spans), "runId": run.run_id}, status_code=202)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/meta", meta),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/runs/{run_id}", run_status),
            Route("/api/runs/{run_id}/events", run_events),
            Route("/api/runs/{run_id}/decisions", submit_decision, methods=["POST"]),
            Route("/api/runs/{run_id}/transcript.json", transcript_json),
            Route("/api/runs/{run_id}/transcript.md", transcript_markdown),
            Route("/api/runs/{run_id}/trace", ingest_trace, methods=["POST"]),
            Route("/api/runs/{run_id}/trace.json", run_trace_json),
            Route("/api/runs/{run_id}/spans/{span_id}", run_span),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        lifespan=lifespan,
    )
    app.state.run_manager = manager
    app.state.config = config
    return app


def _sse_data(message: dict[str, object]) -> dict[str, str]:
    """Render one SSE message payload."""
    return {"data": json.dumps(message, ensure_ascii=False)}


def _span_seq(span: Mapping[str, object]) -> int:
    """Return the ``seq`` of one span, ``0`` when absent."""
    value = span.get("seq")
    return value if isinstance(value, int) else 0


def _trace_token_ok(request: Request) -> bool:
    """Check the optional local trace token (``A2AT_SAMPLE_TRACE_TOKEN``)."""
    expected = os.environ.get("A2AT_SAMPLE_TRACE_TOKEN", "").strip()
    if not expected:
        return True
    supplied = request.headers.get("x-a2at-trace-token", "").strip()
    if supplied == expected:
        return True
    authorization = request.headers.get("authorization", "")
    return authorization == f"Bearer {expected}"


def _coerce_trace_payload(body: object) -> list[dict[str, object]] | None:
    """Return the spans of one JSON list/dict body, ``None`` when malformed."""
    if isinstance(body, list):
        if not all(isinstance(item, Mapping) for item in body):
            return None
        return [dict(item) for item in body if isinstance(item, Mapping)]
    if isinstance(body, Mapping):
        nested = body.get("spans")
        if isinstance(nested, list):
            return _coerce_trace_payload(nested)
        return [dict(body)]
    return None


__all__ = [
    "RunConflictError",
    "RunManager",
    "RunState",
    "create_app",
]
