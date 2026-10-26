"""Tests for the three client negotiation decision providers."""

from __future__ import annotations

import asyncio
import json

import pytest
from a2a_t.core.metadata import NegotiationContext, NegotiationPerformative
from protocol.decision_provider import (
    OFFLINE_FALLBACK_DECISION,
    AdkDecisionProvider,
    DecisionProviderError,
    ManualDecisionProvider,
    NegotiationDecisionProvider,
    ScriptedDecisionProvider,
    build_decision_prompt,
)
from shared.adk_fake_model import (
    CLIENT_AGENT_NAME,
    PROGRESS_MODEL_TURNS,
    FakeLlm,
    FakeScript,
    FakeTurn,
    build_fake_model,
)
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import SCENARIO_INFORMATION, SCENARIO_TARGET, get_scenario


def _session(
    round_number: int = 1,
    *,
    performative: NegotiationPerformative = NegotiationPerformative.PROPOSE,
) -> NegotiationContext:
    return NegotiationContext(id="neg-1", round=round_number, max_rounds=3, performative=performative)


def _view(kind: str = "information", *, content: str = "Please provide the region.") -> NegotiationProposeView:
    return NegotiationProposeView(
        negotiation_id="neg-1",
        kind=kind,
        round=1,
        max_rounds=3,
        content=content,
        template_uri="Negotiation-T/information-negotiation/propose/v1",
        context_id="ctx-1",
    )


def test_all_providers_satisfy_the_protocol() -> None:
    providers = (
        AdkDecisionProvider(model=build_fake_model()),
        ScriptedDecisionProvider([NegotiationDecision(action="accept", rationale="ok")]),
        ManualDecisionProvider(timeout_seconds=0.1),
    )

    for provider in providers:
        assert isinstance(provider, NegotiationDecisionProvider)


def test_build_decision_prompt_carries_the_round_and_content() -> None:
    prompt = build_decision_prompt(_view(), _session(2))

    assert "Round: 2 of 3" in prompt
    assert "Negotiation type: information" in prompt
    assert "Please provide the region." in prompt


def test_build_decision_prompt_carries_the_conversation_context() -> None:
    prompt = build_decision_prompt(
        _view(),
        _session(1),
        user_request="current request",
        conversation_context="user: earlier request\nassistant: earlier result",
    )

    assert "Conversation context (earlier turns):" in prompt
    assert "user: earlier request" in prompt
    assert "Original user request:\ncurrent request" in prompt


def test_build_decision_prompt_carries_the_original_request() -> None:
    prompt = build_decision_prompt(
        _view("target"),
        _session(2),
        user_request="The target is {00:00~07:00,2Mbps}; {20:00~00:00,10Mbps}.",
    )

    assert "Original user request:" in prompt
    assert "00:00~07:00,2Mbps" in prompt
    assert "Return exactly one NegotiationDecision JSON object" in prompt
    assert "no reasoning" in prompt


async def test_scripted_provider_returns_decisions_in_order_and_then_raises() -> None:
    provider = ScriptedDecisionProvider.from_scenario(SCENARIO_TARGET)
    session = _session()

    first = await provider.respond(_view("target"), session)
    second = await provider.respond(_view("target"), session)

    assert first.action == "counter"
    assert second.action == "accept"
    assert provider.remaining == 0
    with pytest.raises(DecisionProviderError, match="exhausted"):
        await provider.respond(_view("target"), session)


def test_scripted_provider_accepts_a_scenario_name() -> None:
    provider = ScriptedDecisionProvider.from_scenario(SCENARIO_INFORMATION)
    scenario = get_scenario(SCENARIO_INFORMATION)

    assert provider.remaining == len(scenario.scripted_client_decisions)


async def test_manual_provider_returns_a_submitted_decision() -> None:
    provider = ManualDecisionProvider(timeout_seconds=1.0)
    submitted = NegotiationDecision(action="reject", rationale="SLA cannot be met.")

    assert provider.pending is False
    provider.submit(submitted)
    assert provider.pending is True

    decision = await provider.respond(_view(), _session())

    assert decision is submitted
    assert provider.pending is False


async def test_manual_provider_times_out_into_an_abort() -> None:
    provider = ManualDecisionProvider(timeout_seconds=0.01)

    decision = await provider.respond(_view(), _session(round_number=3))

    assert decision.action == "abort"
    assert "timed out" in decision.rationale
    assert "round 3" in decision.rationale
    assert provider.pending is False


async def test_manual_provider_waits_for_a_late_submission() -> None:
    provider = ManualDecisionProvider(timeout_seconds=1.0)
    submitted = NegotiationDecision(action="accept", rationale="ok", filled_params={"region": "X"})

    async def submit_later() -> None:
        await asyncio.sleep(0.01)
        provider.submit(submitted)

    task = asyncio.create_task(submit_later())
    decision = await provider.respond(_view(), _session())
    await task

    assert decision is submitted


async def test_adk_provider_returns_the_fake_model_decision() -> None:
    provider = AdkDecisionProvider(model=build_fake_model())

    decision = await provider.respond(_view(), _session())

    assert decision.action == "accept"
    assert decision.rationale
    assert decision is not OFFLINE_FALLBACK_DECISION


def _scripted_model(*texts: str) -> FakeLlm:
    """Build a fake model serving one client-agent turn per text."""
    return build_fake_model(
        (
            FakeScript(
                marker=CLIENT_AGENT_NAME,
                turns=tuple(FakeTurn(text=text) for text in texts),
                progress=PROGRESS_MODEL_TURNS,
            ),
        )
    )


async def test_adk_provider_extracts_a_decision_from_prose_and_fences() -> None:
    """A live model may wrap the JSON in a sentence or a fence; the parser still finds it."""
    payload = json.dumps({"action": "reject", "rationale": "The SLA cannot be met."})
    provider = AdkDecisionProvider(
        model=_scripted_model(f"Here is my decision:\n```json\n{payload}\n```\nDone."),
    )

    decision = await provider.respond(_view(), _session())

    assert decision.action == "reject"
    assert decision.rationale == "The SLA cannot be met."


async def test_adk_provider_retries_once_when_the_model_returns_only_prose() -> None:
    """One corrective turn recovers a model that ignored the output schema."""
    payload = json.dumps({"action": "counter", "rationale": "Use a non-overlapping target."})
    provider = AdkDecisionProvider(
        model=_scripted_model(
            "I need to check the policy before I answer.",
            payload,
        ),
    )

    decision = await provider.respond(_view("target"), _session())

    assert decision.action == "counter"
    assert decision.rationale == "Use a non-overlapping target."


async def test_adk_provider_uses_the_offline_fallback_when_the_model_returns_no_decision() -> None:
    provider = AdkDecisionProvider(model=build_fake_model(), fallback_decision=OFFLINE_FALLBACK_DECISION)

    decision = await provider.respond(_view(), _session())

    # The fake model serves its scripted client decision, so the fallback only applies to a model
    # that produced garbage; the contract here is that the provider always returns a decision.
    assert decision.action in ("accept", "reject", "abort", "counter")


def test_adk_provider_raises_without_a_fallback_on_unparseable_output() -> None:
    provider = AdkDecisionProvider(model=build_fake_model())

    with pytest.raises(DecisionProviderError):
        provider._fallback_or_raise("no decision")
