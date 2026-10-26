"""Client-side negotiation decision providers of the RAN energy-saving sample.

The deterministic client flow never decides a negotiation answer itself: it hands the inbound
``Propose`` to a :class:`NegotiationDecisionProvider` and turns the returned
:class:`~shared.models.NegotiationDecision` into protocol content. Three providers cover the three
integration modes of the design:

* :class:`AdkDecisionProvider` runs the ``agents.client_agent`` ADK agent (the default, automatic
  mode);
* :class:`ScriptedDecisionProvider` replays the offline scenario decisions;
* :class:`ManualDecisionProvider` waits for a decision submitted by the Web UI.

Every provider is asynchronous. The flow calls ``respond`` once per inbound proposal and uses the
negotiation session for round/performative context; providers never advance rounds themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from a2a_t.core.metadata import NegotiationContext
from google.adk.apps import App
from google.adk.events import Event as AdkEvent
from google.adk.runners import InMemoryRunner
from google.genai import types
from shared.adk_tracing import TracePlugin
from shared.config import build_traced_adk_model
from shared.json_text import iter_json_objects
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import Scenario, get_scenario
from shared.trace import trace_scope

#: ADK application name of the client decision agent.
CLIENT_APP_NAME = "ran_energy_client"

#: ADK user id used for every client decision session.
CLIENT_USER_ID = "ran-energy-client"

#: Default budget of one manual decision (design §12: five minutes).
DEFAULT_MANUAL_TIMEOUT_SECONDS = 300.0

#: Decision returned by the offline ADK provider when the model output cannot be parsed.
OFFLINE_FALLBACK_DECISION = NegotiationDecision(
    action="accept",
    rationale="The offline model returned no parseable decision; the proposal is accepted.",
)

#: Corrective user turn sent once when a live model answers with prose instead of a decision.
DECISION_RETRY_PROMPT = (
    "Your previous message did not contain a valid NegotiationDecision JSON object. "
    "Return exactly one JSON object with the decision fields and nothing else: no reasoning, "
    "markdown fences, or prose outside the JSON."
)


class DecisionProviderError(RuntimeError):
    """Raised when a provider cannot produce a decision."""


@runtime_checkable
class NegotiationDecisionProvider(Protocol):
    """Decide how the client answers one inbound negotiation proposal.

    Implementations may call an LLM, read a script, or wait for a human. They must return a
    :class:`NegotiationDecision` and must not send protocol messages or change the round.
    """

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision: ...


def build_decision_prompt(
    propose: NegotiationProposeView,
    session: NegotiationContext,
    *,
    user_request: str | None = None,
    conversation_context: str | None = None,
) -> str:
    """Build the user message handed to the ADK decision agent for one proposal.

    The original user request matters for target/feasibility proposals: their rendered content
    states the conflict but not the concrete throughput target, so a model without the request
    cannot propose a corrected, non-overlapping target and tends to loop through the tools. The
    conversation context carries earlier turns so a follow-up keeps the same intent.
    """
    original = f"Original user request:\n{user_request}\n\n" if user_request else ""
    history = (
        f"Conversation context (earlier turns):\n{conversation_context.strip()}\n\n"
        if conversation_context and conversation_context.strip()
        else ""
    )
    if propose.kind == "information":
        tool_rule = (
            "This information proposal does not name a concrete plan. Before deciding, call "
            "get_sla_policy for cell-001 and cell-002 to ground the missing value; never use their "
            "SLA floors as guaranteedThroughputTarget. "
        )
    else:
        tool_rule = (
            "Use the tools only when the proposal names a concrete cell, window, or rate; "
            "otherwise decide from the original request and the proposal. "
        )
    return (
        "Answer the following A2A-T negotiation proposal.\n"
        f"Negotiation type: {propose.kind}\n"
        f"Round: {session.round} of {session.max_rounds}\n"
        f"Proposal template: {propose.template_uri or 'unknown'}\n"
        f"{original}"
        f"{history}"
        "Proposal content:\n"
        f"{propose.content}\n\n"
        f"{tool_rule}"
        "Return exactly one NegotiationDecision JSON object and nothing else: no reasoning, "
        "markdown, or prose outside the JSON."
    )


class AdkDecisionProvider:
    """Decision provider backed by the ``agents.client_agent`` ADK agent.

    The provider keeps one ADK session per negotiation session id, so the agent sees the previous
    proposals and decisions of the same negotiation. The structured ``NegotiationDecision`` is read
    from the final event's ``output`` when ADK parsed it and from the final text content otherwise;
    in offline mode an unparseable output falls back to :data:`OFFLINE_FALLBACK_DECISION`.
    """

    def __init__(
        self,
        agent: Any | None = None,
        *,
        model: Any | None = None,
        app_name: str = CLIENT_APP_NAME,
        user_id: str = CLIENT_USER_ID,
        fallback_decision: NegotiationDecision | None = None,
        offline: bool = False,
        user_request: str | None = None,
        conversation_context: str | None = None,
    ) -> None:
        """Create one ADK-backed provider.

        Args:
            agent: Pre-built ADK agent; defaults to ``agents.client_agent.agent.build_client_agent``
                with the configured model.
            model: Model override used when ``agent`` is not supplied (the fake model in tests).
            app_name: ADK application name.
            user_id: ADK user id of every decision session.
            fallback_decision: Decision returned when the model output cannot be parsed; ``None``
                raises :class:`DecisionProviderError` instead.
            offline: When ``True`` and no explicit fallback is supplied, use
                :data:`OFFLINE_FALLBACK_DECISION` (the deterministic fake model cannot fail, but a
                malformed offline response must not abort the demo).
            user_request: Original natural-language request of the run; included in every decision
                prompt so target/feasibility proposals can be resolved without guessing.
            conversation_context: Recent turns of the same client conversation; included in every
                decision prompt so a follow-up keeps the earlier intent.
        """
        if agent is None:
            from agents.client_agent.agent import build_client_agent

            agent = build_client_agent(build_traced_adk_model(model=model))
        # The tool plugin is attached through the ``App`` (the ``plugins=`` runner argument is
        # deprecated); it records one ``tool`` span per client-agent tool call.
        app = App(name=app_name, root_agent=agent, plugins=[TracePlugin()])
        self._runner = InMemoryRunner(app=app, app_name=app_name)
        self._user_id = user_id
        self._fallback = fallback_decision or (OFFLINE_FALLBACK_DECISION if offline else None)
        self._user_request = user_request
        self._conversation_context = conversation_context
        self._sessions: set[str] = set()

    def set_conversation_context(self, conversation_context: str | None) -> None:
        """Set the recent-turn block used by the next decision prompts."""
        self._conversation_context = conversation_context

    @property
    def runner(self) -> InMemoryRunner:
        """The ADK runner serving the decision agent (test hook)."""
        return self._runner

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        """Run the ADK agent over one proposal and return its structured decision.

        The run is executed under ``trace_scope`` (``client`` role, negotiation context, decision
        agent stage) so its LLM/tool spans are tagged with the negotiation they belong to.
        """
        with trace_scope(
            role="client",
            context_id=propose.context_id or propose.negotiation_id,
            stage="client_decision_agent",
        ):
            return await self._respond(propose, session)

    async def _respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        """Run the decision agent and parse its structured decision."""
        session_id = self._session_id(propose.negotiation_id)
        if session_id not in self._sessions:
            await self._runner.session_service.create_session(
                app_name=self._runner.app_name,
                user_id=self._user_id,
                session_id=session_id,
            )
            self._sessions.add(session_id)

        final_event = await self._run_turn(
            session_id,
            build_decision_prompt(
                propose,
                session,
                user_request=self._user_request,
                conversation_context=self._conversation_context,
            ),
        )
        if final_event is None:
            return self._fallback_or_raise("The ADK agent produced no final response.")

        decision = _decision_from_event(final_event)
        if decision is not None:
            return decision

        # A live model can answer with prose even though the request carries a JSON schema (some
        # OpenAI-compatible gateways drop ``response_format``). One corrective turn in the same ADK
        # session is much cheaper than failing the run; the offline fallback still applies when the
        # retry cannot produce a decision either.
        retry_event = await self._run_turn(session_id, DECISION_RETRY_PROMPT)
        if retry_event is not None:
            decision = _decision_from_event(retry_event)
            if decision is not None:
                return decision
        return self._fallback_or_raise("The ADK agent produced no parseable NegotiationDecision.")

    async def _run_turn(self, session_id: str, text: str) -> AdkEvent | None:
        """Run one user turn in an existing session and return its final event."""
        final_event: AdkEvent | None = None
        async for event in self._runner.run_async(
            user_id=self._user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
        ):
            if event.is_final_response():
                final_event = event
        return final_event

    def _session_id(self, negotiation_id: str) -> str:
        """Return the ADK session id of one negotiation session."""
        return f"ran-client-{negotiation_id}"

    def _fallback_or_raise(self, message: str) -> NegotiationDecision:
        """Return the offline fallback decision or raise a provider error."""
        if self._fallback is not None:
            return self._fallback
        raise DecisionProviderError(message)


def _decision_from_event(event: AdkEvent) -> NegotiationDecision | None:
    """Read a ``NegotiationDecision`` from one ADK final event, ``None`` when absent."""
    output = event.output
    if isinstance(output, NegotiationDecision):
        return output
    if isinstance(output, dict):
        try:
            return NegotiationDecision.model_validate(output)
        except ValueError:
            return None
    content = event.content
    if content is None:
        return None
    for part in content.parts or ():
        if not part.text:
            continue
        parsed = _parse_decision_json(part.text)
        if parsed is not None:
            return parsed
    return None


def _parse_decision_json(text: str) -> NegotiationDecision | None:
    """Parse one JSON decision text, tolerating code fences and surrounding prose."""
    for payload, _raw in iter_json_objects(text):
        try:
            return NegotiationDecision.model_validate(payload)
        except (ValueError, TypeError):
            continue
    return None


class ScriptedDecisionProvider:
    """Decision provider replaying a pre-defined sequence of decisions.

    The offline scenarios of :mod:`shared.scenario` carry their decisions in order; each call to
    :meth:`respond` consumes the next one. Exhausting the sequence is an error, so a scenario whose
    server behavior drifts from its script fails loudly instead of silently accepting.
    """

    def __init__(self, decisions: Sequence[NegotiationDecision]) -> None:
        """Create one scripted provider over an ordered decision list."""
        self._decisions = tuple(decisions)
        self._index = 0

    @classmethod
    def from_scenario(cls, scenario: Scenario | str) -> ScriptedDecisionProvider:
        """Build a provider from one scenario object or scenario name."""
        resolved = get_scenario(scenario) if isinstance(scenario, str) else scenario
        return cls(resolved.scripted_client_decisions)

    @property
    def remaining(self) -> int:
        """Number of scripted decisions not consumed yet."""
        return len(self._decisions) - self._index

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        """Return the next scripted decision.

        Raises:
            DecisionProviderError: when the script has no decision left.
        """
        del propose, session
        if self._index >= len(self._decisions):
            raise DecisionProviderError("The scripted decision sequence is exhausted.")
        decision = self._decisions[self._index]
        self._index += 1
        return decision


class ManualDecisionProvider:
    """Decision provider waiting for a decision submitted by a human (Web UI).

    ``submit`` may be called before or while ``respond`` waits. On timeout the provider answers with
    an ``abort`` decision whose rationale reports the timeout, per the design's error handling.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_MANUAL_TIMEOUT_SECONDS) -> None:
        """Create one manual provider with the given decision budget."""
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[NegotiationDecision] = asyncio.Queue()
        self._waiting = False

    @property
    def timeout_seconds(self) -> float:
        """Decision budget in seconds."""
        return self._timeout_seconds

    @property
    def pending(self) -> bool:
        """Whether a decision is submitted and not consumed yet, or a decision is expected."""
        return self._waiting or not self._queue.empty()

    def submit(self, decision: NegotiationDecision) -> None:
        """Hand one decision to the waiting flow."""
        self._queue.put_nowait(decision)

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        """Wait for the next submitted decision, or abort after the configured timeout."""
        del propose
        self._waiting = True
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=self._timeout_seconds)
        except TimeoutError:
            return NegotiationDecision(
                action="abort",
                rationale=(
                    f"The manual decision for negotiation round {session.round} timed out after "
                    f"{self._timeout_seconds:g}s; the negotiation is aborted."
                ),
            )
        finally:
            self._waiting = False
