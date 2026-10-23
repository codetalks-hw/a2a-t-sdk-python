"""Deterministic A2A task executor and the pre-task negotiation wrapper.

The server deliberately uses **no ADK agent**. After the negotiation gate accepts a Task-T, the
request is handed to :class:`DeterministicTaskExecutor`, which runs the sample's four OSS tools in
order and publishes a standard A2A Task lifecycle. The protocol layer stays in
:class:`NegotiationAgentExecutor`: it runs the gate first, enqueues standalone negotiation messages
without creating a Task, and delegates only accepted work to the deterministic executor.

The plan executed here is **sample business logic**, not a protocol rule: it reads cell-001's
profile, evaluates and applies a 30% saving plan in 00:00~06:00, and writes the report. A real
deployment replaces :func:`execute_sample_plan` with its own OSS planning logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Final

from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import Part, TaskState
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from agents.server_agent.tools import (
    apply_energy_saving_plan,
    evaluate_energy_saving_plan,
    generate_energy_saving_report,
    get_cell_energy_profile,
)
from google.protobuf.json_format import ParseDict
from shared import ran_data
from shared.trace import PHASE_EXECUTION, Span, current_trace, emit_span, trace_scope

from protocol.messages import (
    SAMPLE_RUN_ID_METADATA_KEY,
    TASK_TEMPLATE_URI,
    metadata_to_dict,
    negotiation_context_metadata,
    parse_negotiation_context,
)
from protocol.negotiation_gate import NegotiationGate

#: Sample plan executed by the deterministic server.
DEFAULT_CELL_ID: Final[str] = "cell-001"
DEFAULT_WINDOW: Final[str] = "00:00~06:00"
DEFAULT_TARGET_RATE: Final[str] = "30"

#: Artifact names.
REPORT_ARTIFACT_NAME: Final[str] = "energy saving intent report"
TOOL_ARTIFACT_NAME: Final[str] = "tool result"

#: Final text of a completed Task.
FINAL_TASK_TEXT: Final[str] = "RAN energy-saving task completed."

#: Trace stage of the deterministic server execution.
EXECUTOR_STAGE: Final[str] = "server_execution_agent"

_LOGGER = logging.getLogger("ran_energy_saving.server_executor")


@dataclass(frozen=True)
class SamplePlan:
    """The deterministic sample plan executed after a Task-T is accepted.

    Attributes:
        cell_id: Cell to optimise.
        window: Energy-saving window.
        target_rate: Energy-saving rate in percent.
        date: Profile date; ``None`` uses the dataset date.
    """

    cell_id: str = DEFAULT_CELL_ID
    window: str = DEFAULT_WINDOW
    target_rate: str = DEFAULT_TARGET_RATE
    date: str | None = None


@dataclass(frozen=True)
class ToolStep:
    """One executed OSS tool step.

    Attributes:
        name: Tool name.
        args: Arguments passed to the tool.
        result: Structured tool result.
        duration_ms: Wall-clock duration of the tool call.
    """

    name: str
    args: dict[str, object]
    result: dict[str, object]
    duration_ms: float = 0.0


class PlanExecutionError(RuntimeError):
    """Raised when one deterministic tool returns a structured failure."""

    def __init__(self, tool: str, args: dict[str, object], result: dict[str, object]) -> None:
        """Create one failed plan step.

        Args:
            tool: Tool name.
            args: Arguments passed to the tool.
            result: Structured failure returned by the tool.
        """
        self.tool = tool
        self.arguments = args
        self.result = result
        message = result.get("error_message", "the tool returned ok=false")
        super().__init__(f"{tool} failed: {message}")


def execute_sample_plan(plan: SamplePlan | None = None) -> list[ToolStep]:
    """Run the sample's four OSS tools in order.

    Args:
        plan: Plan to execute; defaults to :class:`SamplePlan`.

    Returns:
        The four executed steps in order.

    Raises:
        PlanExecutionError: when a tool returns ``{"ok": False, ...}``.
    """
    resolved = plan or SamplePlan()
    date = resolved.date or ran_data.dataset_date()
    steps: list[ToolStep] = []

    profile_args: dict[str, object] = {"cell_id": resolved.cell_id, "date": date}
    started = time.perf_counter()
    profile = get_cell_energy_profile(resolved.cell_id, date)
    steps.append(
        ToolStep("get_cell_energy_profile", profile_args, profile, _elapsed_ms(started))
    )
    _require_ok("get_cell_energy_profile", profile_args, profile)

    evaluate_args: dict[str, object] = {
        "cell_id": resolved.cell_id,
        "window": resolved.window,
        "target_rate": resolved.target_rate,
    }
    started = time.perf_counter()
    evaluation = evaluate_energy_saving_plan(
        resolved.cell_id, resolved.window, resolved.target_rate
    )
    steps.append(
        ToolStep("evaluate_energy_saving_plan", evaluate_args, evaluation, _elapsed_ms(started))
    )
    _require_ok("evaluate_energy_saving_plan", evaluate_args, evaluation)

    apply_args: dict[str, object] = {
        "cell_id": resolved.cell_id,
        "window": resolved.window,
        "target_rate": resolved.target_rate,
    }
    started = time.perf_counter()
    applied = apply_energy_saving_plan(resolved.cell_id, resolved.window, resolved.target_rate)
    steps.append(
        ToolStep("apply_energy_saving_plan", apply_args, applied, _elapsed_ms(started))
    )
    _require_ok("apply_energy_saving_plan", apply_args, applied)

    plan_id = applied.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise PlanExecutionError("apply_energy_saving_plan", apply_args, applied)
    report_args: dict[str, object] = {"plan_id": plan_id}
    started = time.perf_counter()
    report = generate_energy_saving_report(plan_id)
    steps.append(
        ToolStep("generate_energy_saving_report", report_args, report, _elapsed_ms(started))
    )
    _require_ok("generate_energy_saving_report", report_args, report)
    return steps


def _elapsed_ms(started: float) -> float:
    """Return the milliseconds elapsed since ``started``."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def _require_ok(tool: str, args: dict[str, object], result: dict[str, object]) -> None:
    """Raise :class:`PlanExecutionError` when a tool result is not successful."""
    if result.get("ok") is not True:
        raise PlanExecutionError(tool, args, result)


def _metadata(context: RequestContext) -> dict[str, object]:
    """Return the inbound message metadata as a plain mapping."""
    message = context.message
    return metadata_to_dict(message.metadata) if message is not None else {}


def _run_id(context: RequestContext) -> str:
    """Return the Run Explorer run id carried by the current trace or message."""
    trace_run_id = current_trace().run_id
    if trace_run_id:
        return trace_run_id
    value = _metadata(context).get(SAMPLE_RUN_ID_METADATA_KEY)
    return value if isinstance(value, str) else ""


def _negotiation_context(context: RequestContext) -> dict[str, object] | None:
    """Return the accepted negotiation context of the request, when it has one."""
    session = parse_negotiation_context(_metadata(context))
    return negotiation_context_metadata(session) if session is not None else None


def _report_metadata(
    context: RequestContext,
    summary: str,
    negotiation_context: dict[str, object] | None,
) -> dict[str, object]:
    """Build the metadata stamped on the final report artifact and status."""
    metadata: dict[str, object] = {
        TASK_T_EXTENSION_URI: summary,
        TEMPLATE_URI_METADATA_KEY: TASK_TEMPLATE_URI,
    }
    if negotiation_context is not None:
        metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = negotiation_context
    run_id = _run_id(context)
    if run_id:
        metadata[SAMPLE_RUN_ID_METADATA_KEY] = run_id
    return metadata


def _execution_span_id(task_id: str) -> str:
    """Return the stable parent span id of one deterministic execution."""
    return f"exec_server_{task_id}"


def _emit_execution_span(context: RequestContext, task_id: str) -> str:
    """Emit the execution parent span and return its id."""
    span_id = _execution_span_id(task_id)
    emit_span(
        Span(
            run_id=_run_id(context),
            span_id=span_id,
            context_id=context.context_id,
            task_id=task_id,
            phase=PHASE_EXECUTION,
            actor="server",
            side="server",
            kind="execution",
            stage=EXECUTOR_STAGE,
            title="Deterministic OSS task execution",
            purpose="Run the four OSS tools in order.",
            direction="internal",
        )
    )
    return span_id


def _emit_tool_span(
    context: RequestContext,
    task_id: str,
    parent_span_id: str,
    step: ToolStep,
) -> None:
    """Emit one tool span parented to the deterministic execution span."""
    emit_span(
        Span(
            run_id=_run_id(context),
            span_id=f"tool_server_{uuid.uuid4().hex}",
            parent_span_id=parent_span_id,
            context_id=context.context_id,
            task_id=task_id,
            phase=PHASE_EXECUTION,
            actor="tool",
            side="server",
            kind="tool",
            stage=EXECUTOR_STAGE,
            title=step.name,
            purpose="Deterministic OSS tool call.",
            status="ok" if step.result.get("ok") is True else "error",
            duration_ms=step.duration_ms,
            detail={
                "toolName": step.name,
                "args": step.args,
                "result": step.result,
            },
        )
    )


class DeterministicTaskExecutor(AgentExecutor):
    """Execute one accepted Task-T with the sample's deterministic OSS tools."""

    def __init__(self, *, plan: SamplePlan | None = None) -> None:
        """Create one executor.

        Args:
            plan: Plan override (tests); defaults to the sample plan.
        """
        self._plan = plan

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Create the Task, run the tools, and publish artifacts/status updates.

        Args:
            context: Gate-approved request; its message already carries the agreed Task-T prompt.
            event_queue: A2A event queue of this request.
        """
        if context.current_task is not None:
            # The five scenarios are single-shot. Re-enqueueing the existing Task keeps a follow-up
            # request from accidentally starting a second plan.
            await event_queue.enqueue_event(context.current_task)
            return
        message = context.message
        if message is None:
            raise ValueError("The approved request carries no A2A message")

        task_id = context.task_id or str(uuid.uuid4())
        context_id = context.context_id or task_id
        task = new_task_from_user_message(message)
        task.id = task_id
        task.context_id = context_id
        run_id = _run_id(context)
        if run_id:
            task.metadata[SAMPLE_RUN_ID_METADATA_KEY] = run_id
        await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        execution_span_id = _emit_execution_span(context, task.id)

        try:
            steps = await asyncio.to_thread(execute_sample_plan, self._plan)
        except PlanExecutionError as exc:
            _LOGGER.warning("deterministic plan failed at %s: %s", exc.tool, exc)
            _emit_failed_tool_span(context, task.id, execution_span_id, exc)
            await updater.failed(
                message=updater.new_agent_message(
                    [Part(text=f"The energy-saving plan failed: {exc}")]
                )
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the Task must always reach a terminal state
            _LOGGER.exception("unexpected deterministic executor failure")
            await updater.failed(
                message=updater.new_agent_message(
                    [Part(text=f"The energy-saving task failed unexpectedly: {exc}")]
                )
            )
            return

        negotiation_context = _negotiation_context(context)
        for step in steps[:-1]:
            _emit_tool_span(context, task.id, execution_span_id, step)
            await updater.add_artifact(
                [Part(text=json.dumps(step.result, ensure_ascii=False))],
                name=TOOL_ARTIFACT_NAME,
                metadata={"toolName": step.name},
                last_chunk=True,
            )

        report_step = steps[-1]
        _emit_tool_span(context, task.id, execution_span_id, report_step)
        report = report_step.result
        summary = str(report.get("summary", ""))
        report_metadata = _report_metadata(context, summary, negotiation_context)
        report_part = Part()
        ParseDict(report, report_part.data)
        await updater.add_artifact(
            [report_part, Part(text=summary)],
            name=REPORT_ARTIFACT_NAME,
            metadata=report_metadata,
            last_chunk=True,
        )
        final_message = updater.new_agent_message(
            [Part(text=FINAL_TASK_TEXT)], metadata=report_metadata
        )
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            message=final_message,
            metadata=report_metadata,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Mark the Task canceled.

        Args:
            context: Request context of the Task.
            event_queue: A2A event queue of this request.
        """
        updater = TaskUpdater(
            event_queue,
            context.task_id or "",
            context.context_id or "",
        )
        await updater.cancel()


def _emit_failed_tool_span(
    context: RequestContext,
    task_id: str,
    parent_span_id: str,
    error: PlanExecutionError,
) -> None:
    """Emit one failed tool span for a plan step that returned a structured error."""
    _emit_tool_span(
        context,
        task_id,
        parent_span_id,
        ToolStep(error.tool, error.arguments, error.result),
    )


class NegotiationAgentExecutor(AgentExecutor):
    """Outer executor: pre-task negotiation gate in front of the deterministic executor."""

    def __init__(self, *, gate: NegotiationGate, inner: AgentExecutor) -> None:
        """Create one executor.

        Args:
            gate: pre-task negotiation gate.
            inner: deterministic executor that runs an accepted Task.
        """
        self._gate = gate
        self._inner = inner
        # The client sends its run id once, on the initial Task-T message; the follow-up
        # accept/reject/abort messages of the same context reuse it for trace correlation.
        self._run_ids: dict[str, str] = {}
        self._run_ids_lock = threading.Lock()

    @property
    def gate(self) -> NegotiationGate:
        """Return the pre-task negotiation gate."""
        return self._gate

    @property
    def inner(self) -> AgentExecutor:
        """Return the inner deterministic executor."""
        return self._inner

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the gate, then short-circuit or delegate to the deterministic executor.

        Args:
            context: inbound A2A request context.
            event_queue: A2A event queue of this request.
        """
        with trace_scope(
            run_id=self._resolve_run_id(context),
            role="server",
            context_id=context.context_id,
            stage="server_execution_agent",
        ):
            outcome = await self._gate.handle(context)
            if outcome.short_circuit:
                if outcome.terminal_task is not None:
                    await event_queue.enqueue_event(outcome.terminal_task)
                    return
                if outcome.message is None:
                    raise ValueError("The gate short-circuited without a message or a terminal task")
                await event_queue.enqueue_event(outcome.message)
                return
            if outcome.task_request is None:
                raise ValueError("The gate approved execution without a request context")
            await self._inner.execute(outcome.task_request, event_queue)

    def _resolve_run_id(self, context: RequestContext) -> str | None:
        """Return the client run id advertised on the initial message (sticky per context)."""
        key = context.context_id or context.task_id or ""
        metadata = _metadata(context)
        advertised = metadata.get(SAMPLE_RUN_ID_METADATA_KEY)
        if isinstance(advertised, str) and advertised.strip():
            with self._run_ids_lock:
                self._run_ids[key] = advertised.strip()
            return advertised.strip()
        with self._run_ids_lock:
            return self._run_ids.get(key)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Delegate cancellation to the deterministic executor."""
        await self._inner.cancel(context, event_queue)


def build_negotiation_executor(*, gate: NegotiationGate) -> NegotiationAgentExecutor:
    """Build the outer executor with the deterministic sample executor inside.

    Args:
        gate: pre-task negotiation gate.
    """
    return NegotiationAgentExecutor(gate=gate, inner=DeterministicTaskExecutor())
