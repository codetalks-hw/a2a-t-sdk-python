"""Custom A2A ``AgentExecutor`` gluing the negotiation gate to ADK's ``A2aAgentExecutor``.

The outer executor runs the gate first. A short-circuit outcome enqueues one standalone
``Message`` (a negotiation ``Propose``/``Abort`` or the plain end message) and creates no Task.
Otherwise the already validated request is delegated to the inner ``A2aAgentExecutor``, which
creates the Task and runs the ADK server agent.

Two ADK integration seams are configured explicitly:

* ``request_converter`` — ADK's default converter reads request-level metadata only, while A2A-T
  metadata travels on ``context.message.metadata``. The converter reads it explicitly, puts the
  negotiated Task-T prompt into the agent message (so the agent never sees the short label text) and
  mirrors the A2A-T context into the ADK session state.
* ``ExecuteInterceptor.after_event`` — stamps the Task-T extension metadata on the report artifact.
  ``after_agent`` only receives the terminal ``TaskStatusUpdateEvent`` (not the final agent
  content), so artifact metadata is added on the artifact update instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events import Event as A2AEvent
from a2a.server.events.event_queue import EventQueue
from a2a.types import TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from agents.server_agent.agent import build_server_agent
from google.adk.a2a.converters.part_converter import A2APartToGenAIPartConverter
from google.adk.a2a.converters.request_converter import (
    A2ARequestToAgentRunRequestConverter,
    AgentRunRequest,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig, ExecuteInterceptor
from google.adk.a2a.executor.executor_context import ExecutorContext
from google.adk.events.event import Event as AdkEvent
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from shared.config import SampleConfig, build_adk_model, load_config

from protocol.messages import (
    SAMPLE_PROPOSAL_METADATA_KEY,
    TASK_TEMPLATE_URI,
    metadata_to_dict,
    negotiation_context_metadata,
    parse_negotiation_context,
    task_prompt_from_metadata,
)
from protocol.negotiation_gate import NegotiationGate

#: ADK application name of the server runtime.
SERVER_APP_NAME: Final[str] = "ran_energy_saving_server"

#: Artifact name of the energy-saving report.
REPORT_ARTIFACT_NAME: Final[str] = "energy saving intent report"

#: ADK session state key carrying the validated Task-T prompt.
STATE_TASK_PROMPT: Final[str] = "a2at_task_prompt"

#: ADK session state key carrying the Task-T template URI.
STATE_TASK_TEMPLATE_URI: Final[str] = "a2at_task_template_uri"

#: ADK session state key carrying the inbound negotiation context (audit only).
STATE_NEGOTIATION_CONTEXT: Final[str] = "a2at_negotiation_context"

#: ADK session state key carrying the sample-only counter proposal (audit only).
STATE_SAMPLE_PROPOSAL: Final[str] = "a2at_sample_proposal"

_LOGGER = logging.getLogger("ran_energy_saving.server_executor")


@dataclass(frozen=True)
class _AuditContext:
    """Per-session A2A-T values needed when the report artifact is emitted.

    Attributes:
        task_template_uri: Task-T template URI of the sample.
        negotiation_context: inbound negotiation context, when the task followed a negotiation.
    """

    task_template_uri: str
    negotiation_context: dict[str, object] | None


def build_request_converter(
    *, task_template_uri: str = TASK_TEMPLATE_URI
) -> A2ARequestToAgentRunRequestConverter:
    """Build the ADK request converter used by the inner ``A2aAgentExecutor``.

    ADK's default converter reads only request-level metadata. The A2A-T Task-T and Negotiation-T
    payloads travel on ``context.message.metadata``, so this converter reads them there, replaces
    the short label part with the negotiated Task-T prompt and exposes the protocol context as ADK
    session state.

    Args:
        task_template_uri: Task-T template URI the accepted prompt addresses.
    """

    def convert(
        context: RequestContext,
        part_converter: A2APartToGenAIPartConverter,
    ) -> AgentRunRequest:
        """Convert one gate-approved A2A request into an ADK run request."""
        del part_converter  # the prompt text is authoritative; parts are rebuilt below
        message = context.message
        if message is None:
            raise ValueError("The A2A request carries no message")
        metadata = metadata_to_dict(message.metadata)
        prompt = task_prompt_from_metadata(metadata) or context.get_user_input()
        if not prompt.strip():
            raise ValueError("The A2A request carries no Task-T prompt")

        state_delta: dict[str, object] = {
            STATE_TASK_PROMPT: prompt,
            STATE_TASK_TEMPLATE_URI: task_template_uri,
        }
        negotiation = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
        if isinstance(negotiation, dict):
            state_delta[STATE_NEGOTIATION_CONTEXT] = dict(negotiation)
        sample = metadata.get(SAMPLE_PROPOSAL_METADATA_KEY)
        if isinstance(sample, dict):
            state_delta[STATE_SAMPLE_PROPOSAL] = dict(sample)

        identity = context.context_id or context.task_id or "anonymous"
        return AgentRunRequest(
            user_id=f"A2A_USER_{identity}",
            session_id=context.context_id,
            new_message=genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=prompt)],
            ),
            state_delta=state_delta,
        )

    return convert


class NegotiationArtifactInterceptor:
    """Stamps A2A-T metadata on the report artifact of the ADK server agent.

    The interceptor caches the inbound A2A-T context per ADK session in ``before_agent``:
    ``after_event`` receives only the executor context, so the cache is how the artifact update is
    connected to the Task-T/Negotiation-T metadata. The cache entry is released by ``after_agent``.
    """

    def __init__(self, *, task_template_uri: str = TASK_TEMPLATE_URI) -> None:
        self._task_template_uri = task_template_uri
        self._audit: dict[str, _AuditContext] = {}

    def as_execute_interceptor(self) -> ExecuteInterceptor:
        """Return the ADK ``ExecuteInterceptor`` bound to this instance."""
        return ExecuteInterceptor(
            before_agent=self.before_agent,
            after_event=self.after_event,
            after_agent=self.after_agent,
        )

    async def before_agent(self, context: RequestContext) -> RequestContext:
        """Remember the A2A-T context of the session before the agent runs."""
        metadata = metadata_to_dict(context.message.metadata) if context.message is not None else {}
        session = parse_negotiation_context(metadata)
        key = context.context_id or context.task_id or ""
        self._audit[key] = _AuditContext(
            task_template_uri=self._task_template_uri,
            negotiation_context=negotiation_context_metadata(session) if session is not None else None,
        )
        return context

    async def after_event(
        self,
        executor_context: ExecutorContext,
        a2a_event: A2AEvent,
        adk_event: AdkEvent,
    ) -> A2AEvent:
        """Stamp the Task-T report metadata on a text artifact update."""
        if not isinstance(a2a_event, TaskArtifactUpdateEvent) or not _adk_event_has_text(adk_event):
            return a2a_event
        audit = self._audit.get(executor_context.session_id)
        artifact = a2a_event.artifact
        report_text = _artifact_text(artifact)
        if report_text:
            artifact.metadata[TASK_T_EXTENSION_URI] = report_text
        artifact.metadata[TEMPLATE_URI_METADATA_KEY] = (
            audit.task_template_uri if audit is not None else self._task_template_uri
        )
        if audit is not None and audit.negotiation_context is not None:
            artifact.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = audit.negotiation_context  # ty: ignore[invalid-assignment]
        if not artifact.name:
            artifact.name = REPORT_ARTIFACT_NAME
        return a2a_event

    async def after_agent(
        self,
        executor_context: ExecutorContext,
        final_event: TaskStatusUpdateEvent,
    ) -> TaskStatusUpdateEvent:
        """Attach the negotiation context to the terminal status and release the cache."""
        audit = self._audit.pop(executor_context.session_id, None)
        if audit is not None and audit.negotiation_context is not None:
            final_event.metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = audit.negotiation_context  # ty: ignore[invalid-assignment]
        return final_event


class NegotiationAgentExecutor(AgentExecutor):
    """Outer executor: pre-task negotiation gate in front of ADK's ``A2aAgentExecutor``."""

    def __init__(self, *, gate: NegotiationGate, inner: AgentExecutor) -> None:
        """Create one executor.

        Args:
            gate: pre-task negotiation gate.
            inner: ADK executor that creates and runs the Task after the gate approves it.
        """
        self._gate = gate
        self._inner = inner

    @property
    def gate(self) -> NegotiationGate:
        """Return the pre-task negotiation gate."""
        return self._gate

    @property
    def inner(self) -> AgentExecutor:
        """Return the inner ADK executor."""
        return self._inner

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the gate, then short-circuit or delegate to the inner executor."""
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

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Delegate cancellation to the inner executor."""
        await self._inner.cancel(context, event_queue)


def build_negotiation_executor(
    *,
    gate: NegotiationGate,
    config: SampleConfig | None = None,
    model: BaseLlm | None = None,
) -> NegotiationAgentExecutor:
    """Build the outer executor with an ADK runner around the server agent.

    Args:
        gate: pre-task negotiation gate.
        config: sample configuration; defaults to :func:`shared.config.load_config`.
        model: optional ADK model override (tests inject the fake model); defaults to
            :func:`shared.config.build_adk_model`.
    """
    resolved_config = config or load_config()
    resolved_model = model if model is not None else build_adk_model(resolved_config)
    runner = Runner(
        app_name=SERVER_APP_NAME,
        agent=build_server_agent(resolved_model),
        session_service=InMemorySessionService(),
    )
    interceptor = NegotiationArtifactInterceptor(task_template_uri=TASK_TEMPLATE_URI)
    executor_config = A2aAgentExecutorConfig(
        request_converter=build_request_converter(task_template_uri=TASK_TEMPLATE_URI),
        execute_interceptors=[interceptor.as_execute_interceptor()],
    )
    inner = A2aAgentExecutor(
        runner=runner,
        config=executor_config,
        force_new_version=True,
    )
    _LOGGER.info("negotiation executor built (force_new_version=True)")
    return NegotiationAgentExecutor(gate=gate, inner=inner)


def _adk_event_has_text(event: AdkEvent) -> bool:
    """Report whether one ADK event carries model text (as opposed to tool traffic)."""
    content = event.content
    if content is None:
        return False
    return any(part.text for part in (content.parts or ()))


def _artifact_text(artifact: object) -> str:
    """Return the concatenated text parts of one artifact."""
    parts = getattr(artifact, "parts", ())
    return "\n".join(part.text for part in parts if getattr(part, "text", "")).strip()
