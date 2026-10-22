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
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from a2a_t.core.metadata import NegotiationContext
from protocol.client_flow import ClientFlowResult
from protocol.client_runtime import MODE_AUTO, MODE_MANUAL, build_client_runtime
from protocol.transcript import DEFAULT_RUNS_DIR, NegotiationTranscript, TranscriptPaths
from shared.config import SUPPORTED_TRANSPORTS, SampleConfig
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import SUPPORTED_SCENARIO_NAMES
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

#: Static directory of the single page.
STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"

#: Run statuses.
STATUS_RUNNING: Final[str] = "running"
STATUS_FINISHED: Final[str] = "finished"
STATUS_FAILED: Final[str] = "failed"
RunStatus = Literal["running", "finished", "failed"]

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
    ) -> ClientFlowResult: ...

    async def aclose(self) -> None: ...


RuntimeFactory = Callable[[SampleConfig, str, str, str], Awaitable[ClientRuntimeLike]]


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
        task: background task of the run.
    """

    run_id: str
    scenario: str
    mode: str
    transport: str
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
    task: asyncio.Task[None] | None = None

    @property
    def manual_provider(self) -> Any | None:
        """The manual provider of the runtime, when the run uses one."""
        return self.runtime.manual_provider if self.runtime is not None else None

    def broadcast(self, message: dict[str, object]) -> None:
        """Publish one SSE message to the replay log and every subscriber."""
        self.messages.append(message)
        for queue in tuple(self.subscribers):
            queue.put_nowait(message)

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

    def snapshot(self) -> dict[str, object]:
        """Return the JSON status/result/transcript document of this run."""
        transcript = self.transcript.to_dict() if self.transcript is not None else None
        return {
            "runId": self.run_id,
            "scenario": self.scenario,
            "mode": self.mode,
            "transport": self.transport,
            "status": self.status,
            "pendingDecision": self.pending_decision,
            "finalState": self.result.final_state if self.result is not None else None,
            "contextId": self.result.context_id if self.result is not None else None,
            "taskId": self.result.task_id if self.result is not None else None,
            "report": self.result.report if self.result is not None else None,
            "error": self.error,
            "transcriptError": self.transcript_error,
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

    def start(self, scenario: str, mode: str, transport: str) -> RunState:
        """Start one background run.

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
        )
        self._runs[run.run_id] = run
        self._active = run
        run.task = asyncio.create_task(self._execute(run), name=f"ran-web-run-{run.run_id}")
        return run

    async def _execute(self, run: RunState) -> None:
        """Build the runtime, run the client flow, write the transcript and publish the outcome."""
        transcript = NegotiationTranscript(
            scenario=run.scenario,
            language=self._config.language,
            model_mode=self._config.model_mode,
            mode=run.mode,
            transport=run.transport,
            runs_dir=self._runs_dir or DEFAULT_RUNS_DIR,
        )
        run.transcript = transcript

        def event_sink(event: dict[str, object]) -> None:
            entry = transcript.record_event(event)
            run.broadcast({"type": "event", "event": entry})

        def on_decision_requested(propose: NegotiationProposeView, session: NegotiationContext) -> None:
            run.pending_decision = True
            run.proposals.append(propose)
            run.broadcast(
                {
                    "type": "decision_requested",
                    "proposal": propose.model_dump(),
                    "round": session.round,
                    "maxRounds": session.max_rounds,
                    "performative": session.performative.value,
                }
            )

        try:
            run.runtime = await self._runtime_factory(self._config, run.scenario, run.mode, run.transport)
            result = await run.runtime.run(
                event_sink=event_sink,
                on_decision_requested=on_decision_requested,
            )
            run.result = result
            for index, decision in enumerate(result.decisions):
                propose = run.proposals[index] if index < len(run.proposals) else None
                entry = transcript.record_decision(decision, propose=propose)
                run.broadcast({"type": "event", "event": entry})
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
            run.broadcast(self._finished_message(run))

    @staticmethod
    def _finished_message(run: RunState) -> dict[str, object]:
        """Build the ``finished`` SSE message of one run."""
        return {
            "type": "finished",
            "runId": run.run_id,
            "status": run.status,
            "finalState": run.result.final_state if run.result is not None else None,
            "report": run.result.report if run.result is not None else None,
            "error": run.error,
            "transcriptJson": f"/api/runs/{run.run_id}/transcript.json",
            "transcriptMarkdown": f"/api/runs/{run.run_id}/transcript.md",
        }

    async def shutdown(self) -> None:
        """Cancel a still-running background run (app shutdown)."""
        active = self._active
        if active is None or active.task is None or active.task.done():
            return
        active.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await active.task


def create_app(
    config: SampleConfig,
    *,
    runs_dir: Path | None = None,
    runtime_factory: RuntimeFactory | None = None,
    env_path: Path | None = None,
) -> Starlette:
    """Build the Starlette application of the Web UI.

    Args:
        config: process-level sample configuration (language and model mode are read-only).
        runs_dir: transcript directory; defaults to the sample ``runs/`` directory.
        runtime_factory: optional runtime builder (tests inject a deterministic double).
        env_path: source ``.env`` handed to the real client runtime.

    Returns:
        The assembled Starlette application.
    """

    async def default_factory(
        resolved_config: SampleConfig,
        scenario: str,
        mode: str,
        transport: str,
    ) -> ClientRuntimeLike:
        """Build the real client runtime with the app's source ``.env``."""
        return await build_client_runtime(
            resolved_config,
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
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
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
            }
        )

    async def start_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)
        scenario = body.get("scenario")
        mode = body.get("mode", MODE_AUTO)
        transport = body.get("transport")
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
        try:
            run = manager.start(str(scenario), str(mode), str(transport))
        except RunConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(
            {
                "runId": run.run_id,
                "language": config.language,
                "modelMode": config.model_mode,
                "modelLabel": config.model_label,
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
                for message in snapshot:
                    yield _sse_data(message)
                    if message.get("type") == "finished":
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


def supported_ui_modes() -> tuple[str, ...]:
    """Return the decision modes the Web UI may start."""
    return UI_MODES


__all__ = [
    "RunConflictError",
    "RunManager",
    "RunState",
    "create_app",
    "supported_ui_modes",
]
