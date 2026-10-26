"""Run lifecycle management: conversations, persistence and per-conversation concurrency."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from a2a_t.core.metadata import NegotiationContext
from shared.config import DISCOVERY_REGISTRY, SampleConfig
from shared.logging_setup import log_context
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.trace import (
    PHASE_DISPATCH,
)

from protocol.client_runtime import MODE_MANUAL
from protocol.client_service.run_state import (
    SESSION_SWEEP_INTERVAL_SECONDS,
    STATUS_FAILED,
    STATUS_FINISHED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    RunConflictError,
    RunState,
    RunStatus,
    RuntimeFactory,
)
from protocol.client_store import (
    RUN_STATUS_RUNNING,
    TURN_ROLE_ASSISTANT,
    TURN_ROLE_USER,
    ClientRunRecord,
    ClientStore,
    now_iso,
)
from protocol.decision_provider import sweep_stale_sessions
from protocol.run_spec import RunCatalog
from protocol.transcript import DEFAULT_RUNS_DIR, NegotiationTranscript

_LOGGER = logging.getLogger("ran_energy_saving.client_service")


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
