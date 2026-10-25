"""LangChain tools and the business guard of the LangGraph OSS execution agent.

The pure tool functions live in :mod:`agents.server_agent.tools`; this module adapts them to
LangChain tools and enforces the order the sample's plan requires. The guard is per execution:
each accepted Task gets a fresh ``PlanGuard`` and a fresh set of tool closures, so concurrent Tasks
cannot observe each other's profile/evaluation/apply state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, tool

from agents.server_agent.tools import (
    apply_energy_saving_plan,
    evaluate_energy_saving_plan,
    generate_energy_saving_report,
    get_cell_energy_profile,
)

#: Structured error code of a tool called out of order.
TOOL_ORDER_VIOLATION: str = "tool_order_violation"

#: Report tool name.
REPORT_TOOL_NAME: str = "generate_energy_saving_report"


@dataclass
class PlanGuard:
    """Per-execution state used to enforce the plan order."""

    profile: dict[str, object] | None = None
    evaluation: dict[str, object] | None = None
    applied: dict[str, object] | None = None
    report: dict[str, object] | None = None
    tool_calls: list[str] = field(default_factory=list)

    def record(self, name: str) -> None:
        """Record one tool call name (diagnostics and tests)."""
        self.tool_calls.append(name)


def _json(result: dict[str, object]) -> str:
    """Serialize one tool result (LangChain tools return text)."""
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _violation(name: str, message: str, **details: object) -> str:
    """Return one structured tool-order error."""
    return _json(
        {
            "ok": False,
            "error_code": TOOL_ORDER_VIOLATION,
            "error_message": message,
            **details,
        }
    )


def _rate_key(value: object) -> str:
    """Normalize one rate value for comparison (``30``, ``30%`` and ``30.0`` match)."""
    text = str(value).strip().removesuffix("%").strip()
    try:
        return f"{float(text):g}"
    except ValueError:
        return text


def _same_plan(
    result: dict[str, object] | None,
    *,
    cell_id: object,
    window: object,
    target_rate: object,
) -> bool:
    """Return whether a stored result describes exactly one plan."""
    if result is None:
        return False
    return (
        str(result.get("cell_id")) == str(cell_id)
        and str(result.get("window")) == str(window)
        and _rate_key(result.get("target_rate")) == _rate_key(target_rate)
    )


def build_execution_tools(guard: PlanGuard) -> list[BaseTool]:
    """Build the four guarded OSS tools of one execution.

    Args:
        guard: per-execution order state.

    Returns:
        The tools in the order the planner is expected to call them.
    """

    @tool("get_cell_energy_profile")
    def get_cell_energy_profile_tool(cell_id: str, date: str) -> str:
        """Read the 24-hour load and energy profile of one radio cell.

        Call this first, before evaluating any plan.
        """
        guard.record("get_cell_energy_profile")
        result = get_cell_energy_profile(cell_id, date)
        if result.get("ok") is True:
            guard.profile = result
            guard.evaluation = None
            guard.applied = None
            guard.report = None
        return _json(result)

    @tool("evaluate_energy_saving_plan")
    def evaluate_energy_saving_plan_tool(cell_id: str, window: str, target_rate: str) -> str:
        """Evaluate an energy-saving plan without applying it.

        Requires the profile of the same cell to have been read first.
        """
        guard.record("evaluate_energy_saving_plan")
        if guard.profile is None or str(guard.profile.get("cell_id")) != str(cell_id):
            return _violation(
                "evaluate_energy_saving_plan",
                "Read get_cell_energy_profile for this cell before evaluating a plan.",
                cell_id=str(cell_id),
            )
        result = evaluate_energy_saving_plan(cell_id, window, target_rate)
        if result.get("ok") is True:
            guard.evaluation = result
            guard.applied = None
            guard.report = None
        return _json(result)

    @tool("apply_energy_saving_plan")
    def apply_energy_saving_plan_tool(cell_id: str, window: str, target_rate: str) -> str:
        """Apply a feasible energy-saving plan and return its plan_id.

        Requires the same plan to have been evaluated successfully first.
        """
        guard.record("apply_energy_saving_plan")
        if not _same_plan(
            guard.evaluation,
            cell_id=cell_id,
            window=window,
            target_rate=target_rate,
        ):
            return _violation(
                "apply_energy_saving_plan",
                "Evaluate this exact cell/window/rate successfully before applying it.",
                cell_id=str(cell_id),
                window=str(window),
                target_rate=str(target_rate),
            )
        result = apply_energy_saving_plan(cell_id, window, target_rate)
        if result.get("ok") is True:
            guard.applied = result
            guard.report = None
        return _json(result)

    @tool("generate_energy_saving_report")
    def generate_energy_saving_report_tool(plan_id: str) -> str:
        """Generate the final report of an applied plan.

        Requires the exact plan_id returned by apply_energy_saving_plan.
        """
        guard.record("generate_energy_saving_report")
        if guard.applied is None or str(guard.applied.get("plan_id")) != str(plan_id):
            return _violation(
                "generate_energy_saving_report",
                "Apply the plan first and use the exact plan_id returned by apply.",
                plan_id=str(plan_id),
            )
        result = generate_energy_saving_report(plan_id)
        if result.get("ok") is True:
            guard.report = result
        return _json(result)

    return [
        get_cell_energy_profile_tool,
        evaluate_energy_saving_plan_tool,
        apply_energy_saving_plan_tool,
        generate_energy_saving_report_tool,
    ]


def tool_result_from_text(text: Any) -> dict[str, object]:
    """Parse one tool result string into a dict (``{}`` for malformed content)."""
    try:
        parsed = json.loads(str(text))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


__all__ = [
    "PlanGuard",
    "REPORT_TOOL_NAME",
    "TOOL_ORDER_VIOLATION",
    "build_execution_tools",
    "tool_result_from_text",
]
