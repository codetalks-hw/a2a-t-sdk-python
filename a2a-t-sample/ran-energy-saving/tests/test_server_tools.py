"""Tests for the RAN energy-saving server agent tools and their offline agent wiring."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import pytest
from agents.server_agent.agent import (
    AGENT_NAME,
    SERVICE_INSTRUCTION,
    build_server_agent,
    root_agent,
)
from agents.server_agent.tools import (
    ERROR_INVALID_DATE,
    ERROR_INVALID_REQUEST,
    ERROR_PLAN_INFEASIBLE,
    ERROR_UNKNOWN_CELL,
    ERROR_UNKNOWN_PLAN,
    apply_energy_saving_plan,
    evaluate_energy_saving_plan,
    generate_energy_saving_report,
    get_cell_energy_profile,
)
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from shared import ran_data
from shared.adk_fake_model import (
    SERVER_AGENT_NAME,
    FakeLlm,
    build_fake_model,
)

#: Tool lookup used to replay the fake model's scripted calls.
TOOLS: dict[str, Callable[..., dict[str, object]]] = {
    "get_cell_energy_profile": get_cell_energy_profile,
    "evaluate_energy_saving_plan": evaluate_energy_saving_plan,
    "apply_energy_saving_plan": apply_energy_saving_plan,
    "generate_energy_saving_report": generate_energy_saving_report,
}

#: Expected tool sequence of the scripted server agent.
EXPECTED_TOOL_SEQUENCE = [
    "get_cell_energy_profile",
    "evaluate_energy_saving_plan",
    "apply_energy_saving_plan",
    "generate_energy_saving_report",
]


@pytest.fixture(autouse=True)
def _reset_plan_store() -> Iterator[None]:
    """Keep every test independent of the plan store state."""
    ran_data.reset()
    yield
    ran_data.reset()


def test_profile_returns_the_full_curve() -> None:
    result = get_cell_energy_profile("cell-001", "2026-10-22")

    assert result["ok"] is True
    assert result["cell_id"] == "cell-001"
    assert result["cell_name"] == "Songshanhu-North"
    assert len(result["hourly_profile"]) == 24
    assert result["available_windows"] == ["00:00~06:00", "23:00~24:00"]
    assert result["capacity"]["max_throughput_mbps"] == 400
    assert result["energy_summary"]["total_kwh"] > 0
    json.dumps(result)


def test_profile_reports_an_unknown_cell_without_raising() -> None:
    result = get_cell_energy_profile("cell-999", "2026-10-22")

    assert result["ok"] is False
    assert result["error_code"] == ERROR_UNKNOWN_CELL
    assert "cell-001" in result["known_cells"]


def test_profile_reports_an_invalid_date() -> None:
    result = get_cell_energy_profile("cell-001", "22-10-2026")

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_DATE


def test_profile_reports_a_non_string_cell_id() -> None:
    result = get_cell_energy_profile(123, "2026-10-22")  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST
    assert result["argument"] == "cell_id"


def test_profile_reports_a_non_string_date() -> None:
    result = get_cell_energy_profile("cell-001", 20261022)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST
    assert result["argument"] == "date"


def test_evaluate_returns_a_feasible_evaluation() -> None:
    result = evaluate_energy_saving_plan("cell-001", "00:00~06:00", "30")

    assert result["ok"] is True
    assert result["feasible"] is True
    assert result["estimated_saving_kwh"] == pytest.approx(32.49)
    assert result["kpi_impact"]["meets_sla"] is True
    json.dumps(result)


def test_evaluate_reports_an_unknown_cell() -> None:
    result = evaluate_energy_saving_plan("cell-999", "00:00~06:00", "30")

    assert result["ok"] is False
    assert result["error_code"] == ERROR_UNKNOWN_CELL


def test_evaluate_reports_a_malformed_window() -> None:
    result = evaluate_energy_saving_plan("cell-001", "midnight", "30")

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST


def test_evaluate_accepts_a_numeric_target_rate() -> None:
    result = evaluate_energy_saving_plan("cell-001", "00:00~06:00", 30)  # type: ignore[arg-type]

    assert result["ok"] is True
    assert result["feasible"] is True
    assert result["target_rate"] == 30.0


def test_evaluate_reports_a_non_string_window() -> None:
    result = evaluate_energy_saving_plan("cell-001", 900, "30")  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST
    assert result["argument"] == "window"


def test_evaluate_reports_an_unsupported_target_rate_type() -> None:
    result = evaluate_energy_saving_plan("cell-001", "00:00~06:00", True)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST
    assert result["argument"] == "target_rate"


def test_evaluate_reports_an_infeasible_plan() -> None:
    result = evaluate_energy_saving_plan("cell-001", "12:00~14:00", "30")

    assert result["ok"] is False
    assert result["error_code"] == ERROR_PLAN_INFEASIBLE
    assert result["feasible"] is False
    assert result["reasons"]
    assert result["recommendation"] is not None


def test_apply_writes_a_plan_and_rejects_infeasible_plans() -> None:
    applied = apply_energy_saving_plan("cell-001", "00:00~06:00", "30")
    rejected = apply_energy_saving_plan("cell-001", "12:00~14:00", "30")

    assert applied["ok"] is True
    assert applied["plan_id"] == "plan-cell-001-001"
    assert applied["status"] == "applied"
    assert rejected["ok"] is False
    assert rejected["error_code"] == ERROR_PLAN_INFEASIBLE
    assert ran_data.list_plan_ids() == ("plan-cell-001-001",)


def test_apply_reports_non_string_arguments_and_writes_nothing() -> None:
    result = apply_energy_saving_plan("cell-001", ["00:00~06:00"], "30")  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_code"] == ERROR_INVALID_REQUEST
    assert result["argument"] == "window"
    assert ran_data.list_plans() == ()


def test_report_has_the_expected_structure() -> None:
    applied = apply_energy_saving_plan("cell-001", "00:00~06:00", "30")
    plan_id = applied["plan_id"]

    report = generate_energy_saving_report(str(plan_id))

    assert report["ok"] is True
    assert report["plan_id"] == plan_id
    assert report["cell_id"] == "cell-001"
    assert report["status"] == "applied"
    assert report["energy"] == {
        "baseline_kwh": pytest.approx(108.3),
        "saved_kwh": pytest.approx(32.49),
        "remaining_kwh": pytest.approx(75.81),
        "saving_percent": pytest.approx(30.0),
    }
    assert report["kpi_impact"]["affected_hours"] == 6
    assert str(plan_id) in report["summary"]
    assert report["highlights"]
    json.dumps(report)


def test_report_reports_an_unknown_plan() -> None:
    report = generate_energy_saving_report("plan-cell-001-001")

    assert report["ok"] is False
    assert report["error_code"] == ERROR_UNKNOWN_PLAN
    assert report["known_plans"] == []


def test_report_reports_a_non_string_plan_id() -> None:
    report = generate_energy_saving_report(123)  # type: ignore[arg-type]

    assert report["ok"] is False
    assert report["error_code"] == ERROR_INVALID_REQUEST
    assert report["argument"] == "plan_id"


def test_built_server_agent_uses_the_injected_fake_model() -> None:
    agent = build_server_agent(build_fake_model())

    assert agent.name == AGENT_NAME
    assert isinstance(agent.model, FakeLlm)
    assert [getattr(tool, "__name__", None) for tool in agent.tools] == EXPECTED_TOOL_SEQUENCE


def test_root_agent_is_constructible_with_the_default_offline_model() -> None:
    assert root_agent.name == AGENT_NAME
    assert isinstance(root_agent.model, FakeLlm)
    assert [getattr(tool, "__name__", None) for tool in root_agent.tools] == EXPECTED_TOOL_SEQUENCE
    assert "get_cell_energy_profile" in SERVICE_INSTRUCTION
    assert "generate_energy_saving_report" in SERVICE_INSTRUCTION


def _server_request() -> LlmRequest:
    """Build a request the fake model routes to the server agent script."""
    return LlmRequest(
        model="fake-ran-energy-saving",
        contents=[types.Content(role="user", parts=[types.Part(text="Apply the energy-saving plan.")])],
        config=types.GenerateContentConfig(
            system_instruction=f'You are an agent. Your internal name is "{SERVER_AGENT_NAME}".',
        ),
    )


async def _next_response(model: FakeLlm, request: LlmRequest) -> LlmResponse:
    """Return the single response the fake model serves for a request."""
    responses = [response async for response in model.generate_content_async(request)]
    assert len(responses) == 1
    return responses[0]


@pytest.mark.asyncio
async def test_fake_model_sequence_is_accepted_by_the_real_tools() -> None:
    model = build_fake_model()
    request = _server_request()
    tool_names: list[str] = []
    results: list[dict[str, object]] = []

    for _ in range(len(EXPECTED_TOOL_SEQUENCE)):
        response = await _next_response(model, request)
        assert response.content is not None
        call = (response.content.parts or [])[0].function_call
        assert call is not None
        tool = TOOLS[call.name]
        result = tool(**dict(call.args or {}))
        tool_names.append(call.name)
        results.append(result)
        request.contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(name=call.name, response=result)
                    )
                ],
            )
        )

    assert tool_names == EXPECTED_TOOL_SEQUENCE
    assert all(result["ok"] is True for result in results)
    assert results[2]["window"] == "00:00~06:00"
    assert results[3]["plan_id"] == results[2]["plan_id"]
