"""Tests for the deterministic ADK fake model."""

from __future__ import annotations

import json

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from shared.adk_fake_model import (
    CLIENT_AGENT_NAME,
    DEFAULT_FINAL_TEXT,
    SERVER_AGENT_NAME,
    FakeLlm,
    FakeScript,
    FakeToolCall,
    FakeTurn,
    build_fake_model,
)


def _request(agent_name: str, user_text: str = "run the task") -> LlmRequest:
    return LlmRequest(
        model="fake-ran-energy-saving",
        contents=[types.Content(role="user", parts=[types.Part(text=user_text)])],
        config=types.GenerateContentConfig(
            system_instruction=f'You are an agent. Your internal name is "{agent_name}".',
        ),
    )


async def _collect(model: FakeLlm, request: LlmRequest) -> list[LlmResponse]:
    return [response async for response in model.generate_content_async(request)]


@pytest.mark.asyncio
async def test_server_script_runs_the_tool_sequence_then_reports() -> None:
    model = build_fake_model()
    request = _request(SERVER_AGENT_NAME)
    tool_names: list[str] = []

    for _ in range(5):
        responses = await _collect(model, request)
        assert len(responses) == 1
        response = responses[0]
        assert response.content is not None
        parts = response.content.parts or []
        function_call = parts[0].function_call
        if function_call is not None:
            tool_names.append(function_call.name)
            request.contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=function_call.name,
                                response={"ok": True},
                            )
                        )
                    ],
                )
            )
        else:
            assert parts[0].text == DEFAULT_FINAL_TEXT

    assert tool_names == [
        "get_cell_energy_profile",
        "evaluate_energy_saving_plan",
        "apply_energy_saving_plan",
        "generate_energy_saving_report",
    ]


@pytest.mark.asyncio
async def test_client_script_returns_a_negotiation_decision() -> None:
    model = build_fake_model()
    request = _request(CLIENT_AGENT_NAME, "Decide on the proposed energy-saving plan.")

    responses = await _collect(model, request)

    assert len(responses) == 1
    content = responses[0].content
    assert content is not None
    decision = json.loads((content.parts or [])[0].text or "")
    assert decision["action"] == "accept"
    assert decision["rationale"]


@pytest.mark.asyncio
async def test_unknown_marker_serves_default_text() -> None:
    model = build_fake_model()
    request = _request("some_other_agent")

    responses = await _collect(model, request)

    content = responses[0].content
    assert content is not None
    assert (content.parts or [])[0].text == DEFAULT_FINAL_TEXT


@pytest.mark.asyncio
async def test_custom_script_can_be_injected() -> None:
    script = FakeScript(
        marker="custom_agent",
        turns=(FakeTurn(tool_calls=(FakeToolCall(name="custom_tool", args={"value": 1}),)),),
        default_text="custom done",
    )
    model = build_fake_model([script])
    request = _request("custom_agent")

    responses = await _collect(model, request)

    function_call = (responses[0].content.parts or [])[0].function_call
    assert function_call is not None
    assert function_call.name == "custom_tool"
    assert function_call.args == {"value": 1}
