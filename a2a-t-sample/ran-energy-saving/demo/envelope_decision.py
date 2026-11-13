"""Decide an A2A-T proposal from the structured annex, before asking the agent.

An A2A-T client decides by reading the proposal prose with a model, and when the prose does not
state the adjustment it falls back to a local rule (the concession ladder in
:mod:`demo.pure_concession`). Both are guesses about a value only the server knows. When the server
sends the missing-item annex (evolution candidate E1), the same renderer the plain client uses can
answer it directly: one grounded decision, no model turn, and no dependence on a local assumption
that may not match this vendor.

This is the A2A-T half of E1. It is a provider wrapper rather than a change to the decision prompt,
so the agent stays in the loop for every proposal the annex cannot answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from a2a_t.core.metadata import NegotiationContext
from protocol.client_policy import ClientPolicy
from protocol.decision_provider import NegotiationDecisionProvider
from shared.models import NegotiationDecision, NegotiationProposeView

from demo.pure_items_answer import answer_from_envelope

_LOGGER = logging.getLogger("ran_energy_saving.envelope_decision")


class EnvelopeDecisionProvider:
    """Answer a proposal from its structured annex, delegating anything else.

    The flow looks through :attr:`delegate` when it decides whether an automatic ACCEPT needs
    policy verification, so a wrapped live model still has its annex counter checked against the
    client floor and the operator's own ask.
    """

    def __init__(
        self,
        delegate: NegotiationDecisionProvider,
        *,
        policy: ClientPolicy | None = None,
        request_text: Callable[[], str] | None = None,
    ) -> None:
        """Wrap one provider.

        Args:
            delegate: Provider consulted when the annex cannot answer the proposal.
            policy: Client acceptance limits applied by the renderer.
            request_text: Returns the operator's current request; used to avoid offering more than
                the operator asked for.
        """
        self._delegate = delegate
        self._policy = policy or ClientPolicy()
        self._request_text = request_text

    @property
    def delegate(self) -> NegotiationDecisionProvider:
        """The wrapped provider, so a caller can see what is still in the decision loop."""
        return self._delegate

    def __getattr__(self, name: str) -> Any:
        """Forward sinks and observers the runtime installs on the provider."""
        return getattr(self._delegate, name)

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        """Return the annex answer when there is one, else the delegate's decision."""
        decision = envelope_decision(
            propose,
            policy=self._policy,
            request_text=self._current_text(),
        )
        if decision is not None:
            _LOGGER.info(
                "proposal answered from the missing-item annex: kind=%s round=%s target=%s",
                propose.kind,
                propose.round,
                decision.counter_proposal,
            )
            return decision
        return await self._delegate.respond(propose, session)

    def _current_text(self) -> str:
        """Return the operator request the answer must not exceed."""
        if self._request_text is not None:
            return self._request_text()
        getter = getattr(self._delegate, "grounding_text", None)
        return getter() if callable(getter) else ""


def envelope_decision(
    propose: NegotiationProposeView,
    *,
    policy: ClientPolicy | None = None,
    request_text: str = "",
) -> NegotiationDecision | None:
    """Return the decision the annex states, or ``None`` when it does not answer the proposal."""
    envelope = propose.missing_items
    if envelope is None:
        return None
    text = answer_from_envelope(envelope, request_text=request_text, policy=policy)
    if text is None:
        return None
    # A2A-T has no counter performative: the flow turns this into an ACCEPT whose result names the
    # corrected target, which is exactly the shape the annex describes.
    return NegotiationDecision(
        action="counter",
        rationale=f"Answered the missing items the server listed: {text}",
        counter_proposal=text,
    )


__all__ = ["EnvelopeDecisionProvider", "envelope_decision"]
