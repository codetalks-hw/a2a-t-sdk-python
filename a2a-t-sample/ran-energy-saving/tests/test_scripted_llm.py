"""Tests for the scripted SDK LLM provider and its schema/scenario routing."""

from __future__ import annotations

import json
from typing import Any

import pytest
from a2a_t.llm.models import LLMClientConfig
from shared.scenario import SCENARIOS, Scenario
from shared.scripted_llm import (
    SCRIPTED_PROVIDER_NAME,
    STAGE_CONTENT_VALIDATION_FEASIBILITY,
    STAGE_CONTENT_VALIDATION_MISSING,
    STAGE_CONTENT_VALIDATION_TARGET,
    STAGE_NEGOTIATION_ACCEPT_EXTRACTION,
    STAGE_NEGOTIATION_FEASIBILITY_ACCEPT_EXTRACTION,
    STAGE_NEGOTIATION_FEASIBILITY_PROPOSE_EXTRACTION,
    STAGE_NEGOTIATION_PROPOSE_EXTRACTION,
    STAGE_NEGOTIATION_TARGET_ACCEPT_EXTRACTION,
    STAGE_NEGOTIATION_TARGET_PROPOSE_EXTRACTION,
    STAGE_SLOT_EXTRACTION,
    STAGE_SLOT_EXTRACTION_FEASIBILITY,
    STAGE_SLOT_EXTRACTION_MISSING,
    STAGE_SLOT_EXTRACTION_TARGET,
    ScriptedLLMClient,
    register_scripted_provider,
    route_structured_call,
)

SLOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"slots": {"type": "object"}, "slot_errors": {"type": "array"}},
}

VALIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"semantic_verdict": {"type": "object"}},
}

_LLM_CONFIG = LLMClientConfig(
    provider=SCRIPTED_PROVIDER_NAME,
    model=SCRIPTED_PROVIDER_NAME,
    api_key="not-needed",
    base_url=None,
    history_window=10,
    max_tokens=None,
    temperature=0.0,
    timeout_seconds=60.0,
    session_max_total=300,
    session_max_per_provider=100,
)

#: Expected slot-extraction and content-validation stage per scenario.
_EXPECTED_STAGES: dict[str, tuple[str, str]] = {
    "information": (STAGE_SLOT_EXTRACTION_MISSING, STAGE_CONTENT_VALIDATION_MISSING),
    "target": (STAGE_SLOT_EXTRACTION_TARGET, STAGE_CONTENT_VALIDATION_TARGET),
    "feasibility": (STAGE_SLOT_EXTRACTION_FEASIBILITY, STAGE_CONTENT_VALIDATION_FEASIBILITY),
    "reject": (STAGE_SLOT_EXTRACTION_MISSING, STAGE_CONTENT_VALIDATION_MISSING),
    "abort": (STAGE_SLOT_EXTRACTION_TARGET, STAGE_CONTENT_VALIDATION_TARGET),
}


def _messages(content: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": "route me"}, {"role": "user", "content": content}]


@pytest.fixture
def client() -> ScriptedLLMClient:
    return ScriptedLLMClient(_LLM_CONFIG, language="en-US")


def test_all_five_scenarios_are_defined() -> None:
    assert tuple(SCENARIOS) == ("information", "target", "feasibility", "reject", "abort")


@pytest.mark.parametrize("scenario_name", tuple(SCENARIOS))
def test_slot_extraction_routes_by_scenario(
    scenario_name: str,
    client: ScriptedLLMClient,
) -> None:
    scenario: Scenario = SCENARIOS[scenario_name]
    expected_slot_stage, _ = _EXPECTED_STAGES[scenario_name]

    response = client.structured(
        messages=_messages(scenario.user_request["en-US"]),
        json_schema=SLOT_SCHEMA,
    )

    assert client.calls[-1] == expected_slot_stage
    assert json.loads(response.content)


@pytest.mark.parametrize("scenario_name", tuple(SCENARIOS))
def test_content_validation_routes_by_scenario(
    scenario_name: str,
    client: ScriptedLLMClient,
) -> None:
    scenario = SCENARIOS[scenario_name]
    _, expected_validation_stage = _EXPECTED_STAGES[scenario_name]

    response = client.structured(
        messages=_messages(scenario.user_request["en-US"]),
        json_schema=VALIDATION_SCHEMA,
    )

    assert client.calls[-1] == expected_validation_stage
    assert json.loads(response.content)


def test_complete_input_routes_to_default_slot_extraction() -> None:
    from shared.scenario import NATURAL_LANGUAGE_PROMPT_INPUT_EN

    stage = route_structured_call(_messages(NATURAL_LANGUAGE_PROMPT_INPUT_EN), SLOT_SCHEMA, language="en-US")

    assert stage == STAGE_SLOT_EXTRACTION


@pytest.mark.parametrize(
    ("properties", "expected_stage"),
    [
        ({"conclusion", "feasibility_summary"}, STAGE_NEGOTIATION_FEASIBILITY_ACCEPT_EXTRACTION),
        (
            {"conclusion", "confirmed_intent", "failure_reason"},
            STAGE_NEGOTIATION_TARGET_ACCEPT_EXTRACTION,
        ),
        ({"conclusion", "items"}, STAGE_NEGOTIATION_ACCEPT_EXTRACTION),
        (
            {"feasibility_negotiation_description", "action"},
            STAGE_NEGOTIATION_FEASIBILITY_PROPOSE_EXTRACTION,
        ),
        ({"target_negotiation_description"}, STAGE_NEGOTIATION_TARGET_PROPOSE_EXTRACTION),
        ({"items", "relationship"}, STAGE_NEGOTIATION_PROPOSE_EXTRACTION),
    ],
)
def test_negotiation_extraction_signatures_route(
    properties: set[str],
    expected_stage: str,
    client: ScriptedLLMClient,
) -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in properties},
    }

    response = client.structured(messages=_messages("negotiator input"), json_schema=schema)

    assert client.calls[-1] == expected_stage
    assert json.loads(response.content)


def test_unroutable_schema_raises() -> None:
    with pytest.raises(ValueError, match="cannot route"):
        route_structured_call(_messages("unknown"), {"properties": {"unrelated": {}}}, language="en-US")


def test_register_scripted_provider_is_idempotent() -> None:
    register_scripted_provider()
    register_scripted_provider()

    from a2a_t.llm.factory import LLMClientFactory

    assert SCRIPTED_PROVIDER_NAME in LLMClientFactory.available_providers()
