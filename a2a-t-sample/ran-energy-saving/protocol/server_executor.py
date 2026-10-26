"""LangGraph A2A task executor and the pre-task negotiation wrapper.

The server deliberately uses **no ADK agent**. After the negotiation gate accepts a Task-T, the
request is handed to :class:`LangGraphTaskExecutor`, which runs a small ReAct graph over the sample's
four RAN NMS tools and publishes a standard A2A Task lifecycle. The protocol layer stays in
:class:`NegotiationAgentExecutor`: it runs the gate first, enqueues standalone negotiation messages
without creating a Task, and delegates only accepted work to the graph executor.

The graph is sample business logic, not a protocol rule: it reads cell-001's profile, evaluates and
applies a 30% saving plan in 00:00~06:00, and writes the report. A real deployment replaces the
planner/tool set with its own RAN NMS planning logic.

Offline mode uses the deterministic :class:`~agents.server_agent.planner.ScriptedChatModel`; live
mode uses ``ChatOpenAI``. Both run through the same graph and the same guarded tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
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
from agents.server_agent.graph import build_execution_graph
from agents.server_agent.langgraph_tools import (
    REPORT_TOOL_NAME,
    PlanGuard,
    build_execution_tools,
    tool_result_from_text,
)
from agents.server_agent.planner import (
    build_planner_input,
    build_planner_model,
    build_system_prompt,
)
from google.protobuf.json_format import ParseDict
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from shared.config import SampleConfig, load_config
from shared.logging_setup import log_context
from shared.trace import PHASE_EXECUTION, Span, current_trace, emit_span, trace_scope

from protocol.messages import (
    SAMPLE_RUN_ID_METADATA_KEY,
    TASK_TEMPLATE_URI,
    metadata_to_dict,
    negotiation_context_metadata,
    parse_negotiation_context,
    task_prompt_from_metadata,
)
from protocol.negotiation_gate import NegotiationGate

#: Artifact names.
REPORT_ARTIFACT_NAME: Final[str] = "energy saving intent report"
TOOL_ARTIFACT_NAME: Final[str] = "tool result"

#: Final text of a completed Task.
FINAL_TASK_TEXT: Final[str] = "RAN energy-saving task completed."

#: Trace stages.
EXECUTOR_STAGE: Final[str] = "server_execution_agent"
PLANNER_STAGE: Final[str] = "server_planner"

#: Default recursion limit when no config is supplied (tests).
DEFAULT_PLANNER_MAX_STEPS: Final[int] = 12

_LOGGER = logging.getLogger("ran_energy_saving.server_executor")


@dataclass(frozen=True)
class ToolStep:
    """One executed RAN NMS tool step.

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


@dataclass
class _PendingToolCall:
    """One model-requested tool call awaiting its result."""

    name: str
    args: dict[str, object]
    started: float


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
            title="RAN NMS task execution",
            purpose="Run the LangGraph ReAct planner over the RAN NMS tools.",
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
    """Emit one tool span parented to the execution span."""
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
            purpose="Guarded RAN NMS tool call.",
            status="ok" if step.result.get("ok") is True else "error",
            duration_ms=step.duration_ms,
            detail={
                "toolName": step.name,
                "args": step.args,
                "result": step.result,
            },
        )
    )


def _message_digest(message: BaseMessage, *, limit: int = 2000) -> dict[str, object]:
    """Build a bounded trace digest of one planner message."""
    content = str(message.content)
    if len(content) > limit:
        content = content[: limit - 1] + "…"
    digest: dict[str, object] = {"role": message.type, "content": content}
    calls = getattr(message, "tool_calls", None)
    if calls:
        digest["toolCalls"] = [
            {"name": call.get("name"), "args": call.get("args"), "id": call.get("id")}
            for call in calls
        ]
    return digest


def _model_id(model: BaseChatModel) -> str:
    """Return the display model id of one chat model."""
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def _token_usage(message: BaseMessage) -> dict[str, object] | None:
    """Return the usage map of one model response, when the provider reports one."""
    usage = getattr(message, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) else None


def _emit_planner_span(
    context: RequestContext,
    task_id: str,
    parent_span_id: str,
    *,
    model: BaseChatModel,
    system_prompt: str,
    history: list[BaseMessage],
    response: AIMessage,
    duration_ms: float,
) -> None:
    """Emit one LLM span for a planner turn."""
    emit_span(
        Span(
            run_id=_run_id(context),
            span_id=f"llm_server_{uuid.uuid4().hex}",
            parent_span_id=parent_span_id,
            context_id=context.context_id,
            task_id=task_id,
            phase=PHASE_EXECUTION,
            actor="llm",
            side="server",
            kind="llm",
            stage=PLANNER_STAGE,
            title="Planner turn",
            purpose="Choose the next guarded RAN NMS tool call.",
            status="ok",
            duration_ms=round(duration_ms, 3),
            model=_model_id(model),
            tokens=_token_usage(response),
            detail={
                "provider": "langgraph",
                "request": {
                    "systemPrompt": system_prompt,
                    "messages": [_message_digest(message) for message in history[-8:]],
                },
                "response": _message_digest(response),
            },
        )
    )


def _task_prompt(context: RequestContext) -> str:
    """Return the accepted Task-T prompt, falling back to the inbound text."""
    prompt = task_prompt_from_metadata(_metadata(context))
    if prompt.strip():
        return prompt
    message = context.message
    if message is not None:
        for part in message.parts or ():
            if part.text and part.text.strip():
                return part.text.strip()
    return "Create a RAN energy saving task."


class LangGraphTaskExecutor(AgentExecutor):
    """Execute one accepted Task-T with the LangGraph ReAct planner."""

    def __init__(
        self,
        *,
        config: SampleConfig | None = None,
        planner_model_factory: Callable[[], BaseChatModel] | None = None,
    ) -> None:
        """Create one executor.

        Args:
            config: resolved sample configuration; defaults to :func:`load_config`.
            planner_model_factory: optional model factory (tests); defaults to
                :func:`~agents.server_agent.planner.build_planner_model`.
        """
        self._config = config
        self._planner_model_factory = planner_model_factory

    @property
    def recursion_limit(self) -> int:
        """Return the planner recursion limit of this executor."""
        configured = self._config.planner_max_steps if self._config is not None else None
        return configured if configured is not None else DEFAULT_PLANNER_MAX_STEPS

    def _model(self) -> BaseChatModel:
        """Build the planner model of this execution."""
        if self._planner_model_factory is not None:
            return self._planner_model_factory()
        return build_planner_model(self._config or load_config())

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Create the Task, run the graph, and publish artifacts/status updates.

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
        _LOGGER.info("task accepted; running the planner")
        execution_span_id = _emit_execution_span(context, task.id)

        negotiation_context = _negotiation_context(context)
        with log_context(
            task_id=task.id,
            context_id=task.context_id,
            run_id=run_id or None,
            role="server",
        ):
            try:
                await self._run_graph(
                    context,
                    updater,
                    task.id,
                    execution_span_id,
                    negotiation_context,
                )
            except asyncio.CancelledError:
                _LOGGER.info("task cancelled")
                raise
            except GraphRecursionError:
                _LOGGER.warning(
                    "planner exceeded %s steps for task %s", self.recursion_limit, task.id
                )
                await updater.failed(
                    message=updater.new_agent_message(
                        [
                            Part(
                                text=(
                                    "The energy-saving planner exceeded "
                                    f"{self.recursion_limit} steps without completing the task."
                                )
                            )
                        ]
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the Task must always reach a terminal state
                _LOGGER.exception("unexpected LangGraph executor failure")
                await updater.failed(
                    message=updater.new_agent_message(
                        [Part(text=f"The energy-saving task failed unexpectedly: {exc}")]
                    )
                )

    async def _run_graph(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        task_id: str,
        execution_span_id: str,
        negotiation_context: dict[str, object] | None,
    ) -> None:
        """Run the graph, publishing one artifact per tool result and the final report."""
        guard = PlanGuard()
        tools = build_execution_tools(guard)
        model = self._model()
        system_prompt = build_system_prompt()
        graph = build_execution_graph(model=model, tools=tools, system_prompt=system_prompt)
        user_content = build_planner_input(_task_prompt(context), negotiation_context)

        history: list[BaseMessage] = [HumanMessage(content=user_content)]
        steps: list[ToolStep] = []
        pending: dict[str, _PendingToolCall] = {}
        final_text = ""
        last_update_at = time.perf_counter()

        async for update in graph.astream(
            {"messages": [HumanMessage(content=user_content)]},
            config={"recursion_limit": self.recursion_limit},
            stream_mode="updates",
        ):
            for node_update in update.values():
                for item in node_update.get("messages", []):
                    history.append(item)
                    if isinstance(item, AIMessage):
                        now = time.perf_counter()
                        turn_duration_ms = (now - last_update_at) * 1000.0
                        last_update_at = now
                        for call in item.tool_calls or []:
                            call_id = str(call.get("id") or uuid.uuid4().hex)
                            args = call.get("args")
                            pending[call_id] = _PendingToolCall(
                                name=str(call.get("name") or "unknown"),
                                args=dict(args) if isinstance(args, dict) else {},
                                started=now,
                            )
                        if not item.tool_calls and item.content:
                            final_text = str(item.content)
                        _emit_planner_span(
                            context,
                            task_id,
                            execution_span_id,
                            model=model,
                            system_prompt=system_prompt,
                            history=history[:-1],
                            response=item,
                            duration_ms=turn_duration_ms,
                        )
                    elif isinstance(item, ToolMessage):
                        call = pending.pop(str(item.tool_call_id), None)
                        name = call.name if call is not None else str(item.name or "unknown")
                        args = call.args if call is not None else {}
                        duration_ms = (
                            (time.perf_counter() - call.started) * 1000.0
                            if call is not None
                            else 0.0
                        )
                        step = ToolStep(
                            name=name,
                            args=args,
                            result=tool_result_from_text(item.content),
                            duration_ms=round(duration_ms, 3),
                        )
                        steps.append(step)
                        _emit_tool_span(context, task_id, execution_span_id, step)
                        if name != REPORT_TOOL_NAME:
                            await updater.add_artifact(
                                [Part(text=json.dumps(step.result, ensure_ascii=False))],
                                name=TOOL_ARTIFACT_NAME,
                                metadata={"toolName": step.name},
                                last_chunk=True,
                            )

        report_steps = [
            step
            for step in steps
            if step.name == REPORT_TOOL_NAME and step.result.get("ok") is True
        ]
        if not report_steps:
            reason = (
                "The planner stopped before generating a successful report."
                if not final_text
                else "The planner stopped without generating a report."
            )
            _LOGGER.warning("task failed: %s", reason)
            await updater.failed(
                message=updater.new_agent_message([Part(text=reason)])
            )
            return

        report = report_steps[-1].result
        summary = str(report.get("summary", ""))
        metadata = _report_metadata(context, summary, negotiation_context)
        report_part = Part()
        ParseDict(report, report_part.data)
        await updater.add_artifact(
            [report_part, Part(text=summary)],
            name=REPORT_ARTIFACT_NAME,
            metadata=metadata,
            last_chunk=True,
        )
        final_message = updater.new_agent_message(
            [Part(text=FINAL_TASK_TEXT)], metadata=metadata
        )
        _LOGGER.info("task completed: %s", summary[:120])
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            message=final_message,
            metadata=metadata,
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


class NegotiationAgentExecutor(AgentExecutor):
    """Outer executor: pre-task negotiation gate in front of the graph executor."""

    def __init__(self, *, gate: NegotiationGate, inner: AgentExecutor) -> None:
        """Create one executor.

        Args:
            gate: pre-task negotiation gate.
            inner: graph executor that runs an accepted Task.
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
        """Return the inner graph executor."""
        return self._inner

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the gate, then short-circuit or delegate to the graph executor.

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
        """Delegate cancellation to the graph executor."""
        await self._inner.cancel(context, event_queue)


def build_negotiation_executor(
    *,
    gate: NegotiationGate,
    config: SampleConfig | None = None,
    planner_model_factory: Callable[[], BaseChatModel] | None = None,
) -> NegotiationAgentExecutor:
    """Build the outer executor with the LangGraph executor inside.

    Args:
        gate: pre-task negotiation gate.
        config: resolved sample configuration.
        planner_model_factory: optional planner model factory (tests).
    """
    return NegotiationAgentExecutor(
        gate=gate,
        inner=LangGraphTaskExecutor(
            config=config,
            planner_model_factory=planner_model_factory,
        ),
    )
