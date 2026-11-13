"""Tests of the A2A-T half of E1: answering a proposal from its structured annex."""

from __future__ import annotations

import pytest
from a2a_t.core.metadata import NegotiationContext, NegotiationPerformative
from demo.envelope_decision import EnvelopeDecisionProvider, envelope_decision
from protocol.client_policy import ClientPolicy
from shared.missing_items import (
    MISSING_ITEMS_METADATA_KEY,
    MissingItem,
    MissingItems,
    read_missing_items,
)
from shared.models import NegotiationDecision, NegotiationProposeView


def _session() -> NegotiationContext:
    """Return one negotiation context."""
    return NegotiationContext(
        id="neg-1", round=1, max_rounds=5, performative=NegotiationPerformative.PROPOSE
    )


def _view(envelope: MissingItems | None) -> NegotiationProposeView:
    """Return one proposal view carrying the given annex."""
    return NegotiationProposeView(
        negotiation_id="neg-1",
        kind="feasibility",
        round=1,
        max_rounds=5,
        content="Feasibility negotiation: the requested plan cannot be executed as written.",
        missing_items=envelope,
    )


def _outage_envelope(max_mbps: float = 2.0) -> MissingItems:
    """Return an annex asking the caller to lower an outage rate."""
    return MissingItems(
        kind="feasibility",
        items=(
            MissingItem(
                item="Guaranteed throughput target",
                field="guaranteedThroughputTarget",
                source="operator",
                acceptable={"maxMbps": max_mbps, "during": "outage"},
            ),
        ),
    )


class _Delegate:
    """Decision provider double recording the proposals it was asked about."""

    def __init__(self) -> None:
        self.calls = 0
        self.sinks: list[object] = []

    async def respond(
        self,
        propose: NegotiationProposeView,
        session: NegotiationContext,
    ) -> NegotiationDecision:
        del propose, session
        self.calls += 1
        return NegotiationDecision(action="accept", rationale="delegate")

    def set_proposal_sink(self, sink: object) -> None:
        self.sinks.append(sink)


def test_the_annex_names_the_corrected_target_without_a_model_turn() -> None:
    """The A2A-T answer is the same grounded counter the plain client would send."""
    decision = envelope_decision(_view(_outage_envelope()), request_text="10Mbps outage")

    assert decision is not None
    # An ACCEPT that names the rate: the flow applies it to every window the event covers, which a
    # prose counter could not do for a segmented plan.
    assert decision.action == "accept"
    assert decision.agreed_rate_mbps == 2.0
    assert decision.feasibility_summary == "Guarantee 2 Mbps during the power outage period."


def test_the_annex_never_answers_below_the_client_floor() -> None:
    """Annex or not, the client's own limits decide what it may accept."""
    decision = envelope_decision(
        _view(_outage_envelope(max_mbps=1.0)),
        policy=ClientPolicy(min_throughput_mbps=2.0),
    )

    assert decision is None


def test_a_proposal_without_the_annex_is_not_answered() -> None:
    """A prose-only vendor keeps the ordinary model decision."""
    assert envelope_decision(_view(None)) is None


def test_a_server_owned_item_is_not_answered() -> None:
    """A region the network owns must still become a question, not an invention."""
    envelope = MissingItems(
        kind="information",
        items=(
            MissingItem(
                item="Energy saving region",
                field="energySavingRegion",
                source="server",
                acceptable={},
            ),
        ),
    )

    assert envelope_decision(_view(envelope)) is None


@pytest.mark.asyncio
async def test_the_wrapper_delegates_when_the_annex_cannot_answer() -> None:
    """The agent stays in the loop for every proposal the annex does not resolve."""
    delegate = _Delegate()
    provider = EnvelopeDecisionProvider(delegate)

    decision = await provider.respond(_view(None), _session())

    assert delegate.calls == 1
    assert decision.rationale == "delegate"


@pytest.mark.asyncio
async def test_the_wrapper_answers_without_consulting_the_delegate() -> None:
    """One grounded decision instead of a model turn."""
    delegate = _Delegate()
    provider = EnvelopeDecisionProvider(delegate)

    decision = await provider.respond(_view(_outage_envelope()), _session())

    assert delegate.calls == 0
    assert decision.action == "accept"


def test_the_wrapper_forwards_the_sinks_the_runtime_installs() -> None:
    """The runtime installs observers on the provider; hiding them would drop the UI events."""
    delegate = _Delegate()
    provider = EnvelopeDecisionProvider(delegate)
    sink = object()

    provider.set_proposal_sink(sink)

    assert delegate.sinks == [sink]
    assert provider.delegate is delegate


def test_a_proposal_view_reads_the_annex_off_the_wire_metadata() -> None:
    """The proposal view is where an A2A-T client sees the structured annex."""
    raw = {
        MISSING_ITEMS_METADATA_KEY: {
            "version": 1,
            "kind": "feasibility",
            "items": [
                {
                    "item": "Guaranteed throughput target",
                    "field": "guaranteedThroughputTarget",
                    "source": "operator",
                    "acceptable": {"maxMbps": 2.0, "during": "outage"},
                }
            ],
        }
    }
    envelope = read_missing_items(raw)
    view = _view(envelope)

    assert view.missing_items is not None
    assert view.missing_items.items[0].acceptable["maxMbps"] == 2.0
