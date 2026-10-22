"""Server-side A2A-T negotiation gate of the RAN energy-saving sample.

The gate is the single protocol entry point of the server executor. It handles every request that
arrives **before a Task exists** (``context.current_task is None``; ``context.task_id`` is not a
reliable signal because ``RequestContext`` generates one in its constructor):

* initial Task-T validation, mapped to an information/target/feasibility negotiation on failure;
* client ``Accept``: validate the negotiation answer and the agreed Task-T prompt, then hand the
  request to the inner ADK executor so a Task is created and executed;
* client ``Reject``/``Abort``: terminal; answer with a plain ``Message("协商已结束")`` and create no
  Task;
* client counter-``Propose``: validate it, let the deterministic policy decide the next proposal or
  the abort, then emit the standalone Negotiation-T message and create no Task.

Every SDK call is synchronous (it may call the LLM), so the gate runs it in a worker thread with
``asyncio.to_thread`` to keep the server event loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar

from a2a.server.agent_execution.context import RequestContext
from a2a.types import Message
from a2a_t.core.errors.exceptions import A2ATError, ContentValidationError, SlotValidationError
from a2a_t.core.metadata import (
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
    MetadataContent,
    NegotiationContext,
    NegotiationPerformative,
)
from a2a_t.core.validation_pipeline import FilledParamData
from shared.scenario import MAX_ROUNDS
from shared.task_schema import TASK_PARAM_SCHEMA

from protocol.messages import (
    ABORT_MESSAGE_TEXT,
    PROPOSE_MESSAGE_TEXT,
    SAMPLE_PROPOSAL_METADATA_KEY,
    TASK_TEMPLATE_URI,
    build_end_message,
    build_error_message,
    build_negotiation_message,
    is_negotiation_message,
    metadata_to_dict,
    missing_extensions,
    negotiation_prompt_from_metadata,
    parse_negotiation_context,
    task_prompt_from_metadata,
)
from protocol.negotiation_policy import (
    ABORT_TEMPLATE_URI,
    AbortContent,
    InboundDecision,
    NegotiationState,
    PolicyDecision,
    ProposalContent,
    SampleProposal,
    decide,
    negotiation_kind_for_errors,
    negotiation_kind_for_template_uri,
)

#: Caller parameter schema of the Negotiation-T validation steps.
#:
#: The SDK merges this schema into its semantic-validation output contract; the sample validates the
#: message and does not need extra extracted parameters, so the schema stays empty.
NEGOTIATION_PARAM_SCHEMA: Final[dict[str, object]] = {"type": "object", "properties": {}}

_LOGGER = logging.getLogger("ran_energy_saving.negotiation_gate")

_ResultT = TypeVar("_ResultT")


class NegotiationContentServer(Protocol):
    """The subset of ``A2ATServer`` the gate depends on (also implemented by test fakes)."""

    def validate_task_prompt_and_data_filling(
        self, prompt: str, schema: Mapping[str, object], template_uri: str
    ) -> FilledParamData: ...

    def validate_propose_prompt_and_data_filling(
        self,
        prompt: str | None,
        context: NegotiationContext | None,
        schema: Mapping[str, object],
        template_uri: str,
    ) -> FilledParamData: ...

    def validate_accept_prompt_and_data_filling(
        self,
        prompt: str | None,
        context: NegotiationContext | None,
        schema: Mapping[str, object],
        template_uri: str,
    ) -> FilledParamData: ...

    def validate_reject_prompt_and_data_filling(
        self,
        prompt: str | None,
        context: NegotiationContext | None,
        schema: Mapping[str, object],
        template_uri: str,
    ) -> FilledParamData: ...

    def validate_abort_prompt_and_data_filling(
        self,
        prompt: str | None,
        context: NegotiationContext | None,
        schema: Mapping[str, object],
        template_uri: str,
    ) -> FilledParamData: ...

    def generate_negotiation_propose_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent: ...

    def generate_negotiation_abort_prompt_from_text(
        self, text: str | None, context: NegotiationContext, template_uri: str
    ) -> MetadataContent: ...


@dataclass(frozen=True)
class GateOutcome:
    """Result of one gate invocation.

    Attributes:
        short_circuit: ``True`` when the gate produced a standalone message that must be enqueued
            without creating a Task.
        message: short-circuit message; ``None`` when execution continues.
        task_request: request context to hand to the inner ADK executor; ``None`` on short circuit.
    """

    short_circuit: bool
    message: Message | None = None
    task_request: RequestContext | None = None

    @classmethod
    def execution(cls, context: RequestContext) -> GateOutcome:
        """Return an outcome that lets the inner ADK executor create and run the Task."""
        return cls(short_circuit=False, task_request=context)

    @classmethod
    def short(cls, message: Message) -> GateOutcome:
        """Return an outcome that only enqueues the given message."""
        return cls(short_circuit=True, message=message)


class NegotiationGate:
    """Deterministic pre-task negotiation gate over the A2A-T SDK server facade."""

    def __init__(
        self,
        prompt_server: NegotiationContentServer,
        *,
        max_rounds: int = MAX_ROUNDS,
        task_template_uri: str = TASK_TEMPLATE_URI,
        task_schema: Mapping[str, object] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create one gate.

        Args:
            prompt_server: A2A-T SDK server facade (or a test double).
            max_rounds: round budget of every negotiation.
            task_template_uri: Task-T template the accepted prompt must address.
            task_schema: caller parameter schema of the Task-T validation.
            logger: optional logger; defaults to the module logger.
        """
        self._prompt_server = prompt_server
        self._max_rounds = max_rounds
        self._task_template_uri = task_template_uri
        self._task_schema: Mapping[str, object] = task_schema if task_schema is not None else TASK_PARAM_SCHEMA
        self._logger = logger or _LOGGER

    async def handle(self, context: RequestContext) -> GateOutcome:
        """Classify one inbound request and return the gate outcome.

        Args:
            context: inbound A2A request context.

        Returns:
            Either a short-circuit message (no Task) or the request to hand to the ADK executor.
        """
        if context.current_task is not None:
            return GateOutcome.execution(context)

        message = context.message
        if message is None:
            return GateOutcome.short(
                build_error_message(context.context_id, "The request carries no A2A message.")
            )
        metadata = metadata_to_dict(message.metadata)
        if is_negotiation_message(metadata):
            return await self._handle_negotiation(context, metadata)
        return await self._handle_initial_task(context, metadata)

    # ------------------------------------------------------------------
    # Initial Task-T
    # ------------------------------------------------------------------

    async def _handle_initial_task(
        self, context: RequestContext, metadata: Mapping[str, object]
    ) -> GateOutcome:
        """Validate the initial Task-T prompt and open a negotiation when it maps to one."""
        context_id = context.context_id
        missing = missing_extensions(context.requested_extensions, (TASK_T_EXTENSION_URI,))
        if missing:
            return GateOutcome.short(
                build_error_message(
                    context_id,
                    f"The request does not declare the required A2A extension(s): {', '.join(missing)}.",
                )
            )
        prompt = task_prompt_from_metadata(metadata)
        if not prompt.strip():
            return GateOutcome.short(
                build_error_message(context_id, "The request carries no Task-T prompt.")
            )
        try:
            await self._call(
                self._prompt_server.validate_task_prompt_and_data_filling,
                prompt,
                self._task_schema,
                self._task_template_uri,
            )
        except ContentValidationError as exc:
            kind = negotiation_kind_for_errors(exc.errors)
            if kind is None:
                return GateOutcome.short(build_error_message(context_id, _validation_error_text(exc)))
            state = NegotiationState(
                kind=kind,
                round=1,
                max_rounds=self._max_rounds,
                missing_items=_missing_slot_names(exc.errors),
            )
            return await self._emit_policy_decision(context_id, decide(state))
        except (A2ATError, ValueError) as exc:
            return GateOutcome.short(build_error_message(context_id, f"Task-T validation failed: {exc}"))

        self._rewrite_task_prompt(context, prompt)
        self._logger.info("initial Task-T validated; handing the request to the ADK executor")
        return GateOutcome.execution(context)

    # ------------------------------------------------------------------
    # Negotiation messages
    # ------------------------------------------------------------------

    async def _handle_negotiation(
        self, context: RequestContext, metadata: Mapping[str, object]
    ) -> GateOutcome:
        """Dispatch one inbound negotiation message by performative."""
        context_id = context.context_id
        session = parse_negotiation_context(metadata)
        if session is None:
            return GateOutcome.short(
                build_error_message(context_id, "The negotiation message carries no valid negotiationContext.")
            )
        performative = session.performative
        if performative is NegotiationPerformative.ACCEPT:
            return await self._handle_accept(context, metadata, session)
        if performative is NegotiationPerformative.REJECT:
            return await self._handle_terminal(context, metadata, session)
        if performative is NegotiationPerformative.ABORT:
            return await self._handle_terminal(context, metadata, session)
        return await self._handle_counter_propose(context, metadata, session)

    async def _handle_accept(
        self,
        context: RequestContext,
        metadata: Mapping[str, object],
        session: NegotiationContext,
    ) -> GateOutcome:
        """Validate the accept and hand the agreed Task-T prompt to the ADK executor."""
        context_id = context.context_id
        prompt = negotiation_prompt_from_metadata(metadata)
        template_uri = metadata.get(TEMPLATE_URI_METADATA_KEY)
        if not prompt.strip() or not isinstance(template_uri, str):
            return GateOutcome.short(
                build_error_message(context_id, "The accept message carries no Negotiation-T content.")
            )
        try:
            await self._call(
                self._prompt_server.validate_accept_prompt_and_data_filling,
                prompt,
                session,
                NEGOTIATION_PARAM_SCHEMA,
                template_uri,
            )
        except (A2ATError, ValueError) as exc:
            return await self._negotiation_failure(context_id, session, f"Accept validation failed: {exc}")

        task_prompt = task_prompt_from_metadata(metadata)
        if not task_prompt.strip():
            return GateOutcome.short(
                build_error_message(context_id, "The accept message carries no agreed Task-T prompt.")
            )
        try:
            await self._call(
                self._prompt_server.validate_task_prompt_and_data_filling,
                task_prompt,
                self._task_schema,
                self._task_template_uri,
            )
        except (A2ATError, ValueError) as exc:
            return await self._negotiation_failure(
                context_id, session, f"Accepted Task-T validation failed: {exc}"
            )

        self._rewrite_task_prompt(context, task_prompt)
        self._logger.info("negotiation accepted at round %s; creating the task", session.round)
        return GateOutcome.execution(context)

    async def _handle_terminal(
        self,
        context: RequestContext,
        metadata: Mapping[str, object],
        session: NegotiationContext,
    ) -> GateOutcome:
        """End a rejected/aborted negotiation with a plain message and no Task."""
        validator = (
            self._prompt_server.validate_reject_prompt_and_data_filling
            if session.performative is NegotiationPerformative.REJECT
            else self._prompt_server.validate_abort_prompt_and_data_filling
        )
        template_uri = metadata.get(TEMPLATE_URI_METADATA_KEY)
        prompt = negotiation_prompt_from_metadata(metadata)
        if isinstance(template_uri, str) and prompt.strip():
            try:
                await self._call(validator, prompt, session, NEGOTIATION_PARAM_SCHEMA, template_uri)
            except (A2ATError, ValueError) as exc:
                self._logger.warning("terminal negotiation message failed validation: %s", exc)
        self._logger.info(
            "negotiation ended by client %s at round %s; no task is created",
            session.performative.value,
            session.round,
        )
        return GateOutcome.short(build_end_message(context.context_id))

    async def _handle_counter_propose(
        self,
        context: RequestContext,
        metadata: Mapping[str, object],
        session: NegotiationContext,
    ) -> GateOutcome:
        """Validate a client counter proposal and let the policy answer it."""
        context_id = context.context_id
        prompt = negotiation_prompt_from_metadata(metadata)
        template_uri = metadata.get(TEMPLATE_URI_METADATA_KEY)
        if not prompt.strip() or not isinstance(template_uri, str):
            return GateOutcome.short(
                build_error_message(context_id, "The counter proposal carries no Negotiation-T content.")
            )
        try:
            await self._call(
                self._prompt_server.validate_propose_prompt_and_data_filling,
                prompt,
                session,
                NEGOTIATION_PARAM_SCHEMA,
                template_uri,
            )
        except (A2ATError, ValueError) as exc:
            return await self._negotiation_failure(
                context_id, session, f"Counter proposal validation failed: {exc}"
            )

        kind = negotiation_kind_for_template_uri(template_uri)
        if kind is None:
            return GateOutcome.short(
                build_error_message(context_id, f"Unsupported negotiation template URI: {template_uri}")
            )
        sample = SampleProposal.try_parse(metadata.get(SAMPLE_PROPOSAL_METADATA_KEY))
        state = NegotiationState(
            kind=kind,
            round=session.round,
            max_rounds=session.max_rounds,
            inbound=InboundDecision(performative=NegotiationPerformative.PROPOSE, sample_proposal=sample),
        )
        return await self._emit_policy_decision(context_id, decide(state))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _emit_policy_decision(self, context_id: str | None, decision: PolicyDecision) -> GateOutcome:
        """Turn one policy decision into a standalone negotiation message or an execution request."""
        content = decision.content
        if decision.action == "end":
            return GateOutcome.short(build_end_message(context_id))
        if decision.action == "propose" and isinstance(content, ProposalContent):
            session = NegotiationContext(
                id=context_id or "",
                round=content.round,
                max_rounds=self._max_rounds,
                performative=NegotiationPerformative.PROPOSE,
            )
            generated = await self._call(
                self._prompt_server.generate_negotiation_propose_prompt_from_text,
                content.text,
                session,
                content.template_uri,
            )
            self._logger.info(
                "server propose generated: kind=%s round=%s", content.kind.value, content.round
            )
            return GateOutcome.short(
                build_negotiation_message(generated, context_id=session.id, text=PROPOSE_MESSAGE_TEXT)
            )
        if decision.action == "abort" and isinstance(content, AbortContent):
            session = NegotiationContext(
                id=context_id or "",
                round=content.round,
                max_rounds=self._max_rounds,
                performative=NegotiationPerformative.ABORT,
            )
            try:
                generated = await self._call(
                    self._prompt_server.generate_negotiation_abort_prompt_from_text,
                    content.text,
                    session,
                    content.template_uri,
                )
            except (A2ATError, ValueError) as exc:
                return GateOutcome.short(
                    build_error_message(context_id, f"Negotiation abort generation failed: {exc}")
                )
            self._logger.info("server abort generated at round %s", content.round)
            return GateOutcome.short(
                build_negotiation_message(generated, context_id=session.id, text=ABORT_MESSAGE_TEXT)
            )
        raise AssertionError(f"Unsupported policy decision: {decision.action}")

    async def _negotiation_failure(
        self, context_id: str | None, session: NegotiationContext, text: str
    ) -> GateOutcome:
        """Answer an invalid negotiation message with an abort beyond the round budget, else an error."""
        self._logger.warning("negotiation message rejected: %s", text)
        if session.round <= session.max_rounds:
            return GateOutcome.short(build_error_message(context_id, text))
        self._logger.warning(
            "abort requested after an invalid answer beyond the round budget (round=%s)", session.round
        )
        decision = PolicyDecision(
            "abort",
            AbortContent(
                round=session.round,
                template_uri=ABORT_TEMPLATE_URI,
                text="The negotiation exceeded its round budget without a valid answer.",
                reason="invalid-answer-beyond-budget",
            ),
            "The negotiation exceeded its round budget with an invalid answer.",
        )
        return await self._emit_policy_decision(context_id, decision)

    @staticmethod
    async def _call(function: Callable[..., _ResultT], *args: object) -> _ResultT:
        """Run one synchronous SDK call in a worker thread."""
        return await asyncio.to_thread(function, *args)

    def _rewrite_task_prompt(self, context: RequestContext, prompt: str) -> None:
        """Replace the request body with the agreed Task-T prompt.

        The A2A-T prompt itself stays in ``metadata[Task-T/v1]`` for audit; the message part is the
        agreed prompt so transports and logs that only read the text part see the negotiated task.
        """
        message = context.message
        if message is None:
            return
        message.ClearField("parts")
        message.parts.add(text=prompt)
        message.metadata[TASK_T_EXTENSION_URI] = prompt


def _missing_slot_names(errors: Sequence[SlotValidationError]) -> tuple[str, ...]:
    """Return the distinct non-empty slot names of a structured validation failure."""
    names: list[str] = []
    for error in errors:
        slot_name = error.slot_name
        if slot_name.strip() and slot_name not in names:
            names.append(slot_name)
    return tuple(names)


def _validation_error_text(error: ContentValidationError) -> str:
    """Render one unmapped content-validation failure with its structured codes."""
    codes = ", ".join(f"{item.slot_name}:{item.code}" for item in error.errors) or "no structured errors"
    return f"Task-T validation failed and cannot open a negotiation ({codes}): {error}"
