"""ADK function tools of the RAN energy-saving server agent.

Every tool is a deterministic function over the in-memory dataset of :mod:`shared.ran_data` and its
process-local plan store. Tools never access the network and never raise for expected failures:
unknown cells, unknown plans, malformed requests, and infeasible plans are reported as structured
error dictionaries so the LLM can explain them and adapt.

All return values are JSON-serializable dictionaries. Successful results carry ``"ok": True``;
failures carry ``"ok": False``, ``"error_code"``, and ``"error_message"``.
"""

from __future__ import annotations

from shared import ran_data
from shared.ran_data import (
    CellCapacity,
    EnergySavingPlan,
    HourlySample,
    PlanEvaluation,
    RanCell,
)

#: Error code for an unknown cell id.
ERROR_UNKNOWN_CELL = "unknown_cell"
#: Error code for an unknown plan id.
ERROR_UNKNOWN_PLAN = "unknown_plan"
#: Error code for a malformed date.
ERROR_INVALID_DATE = "invalid_date"
#: Error code for a malformed window or target rate.
ERROR_INVALID_REQUEST = "invalid_request"
#: Error code for a well-formed plan that violates the cell constraints.
ERROR_PLAN_INFEASIBLE = "plan_infeasible"


def _error(code: str, message: str, **details: object) -> dict[str, object]:
    """Build a structured error result."""
    return {
        "ok": False,
        "error_code": code,
        "error_message": message,
        **details,
    }


def _capacity_dict(capacity: CellCapacity) -> dict[str, object]:
    """Return the capacity block of a profile."""
    return capacity.to_dict()


def _hourly_dict(sample: HourlySample) -> dict[str, object]:
    """Return one hourly curve point of a profile."""
    return {
        "hour": sample.hour,
        "load_rate": sample.load_rate,
        "energy_kw": sample.energy_kw,
    }


def _energy_summary(cell: RanCell) -> dict[str, object]:
    """Return the 24-hour energy summary of a cell."""
    peak = max(cell.hourly, key=lambda sample: sample.load_rate)
    return {
        "total_kwh": round(sum(sample.energy_kw for sample in cell.hourly), 2),
        "average_load_rate": round(
            sum(sample.load_rate for sample in cell.hourly) / ran_data.HOURS_PER_DAY, 3
        ),
        "peak_load_rate": peak.load_rate,
        "peak_load_hour": peak.hour,
        "peak_energy_kw": max(sample.energy_kw for sample in cell.hourly),
    }


def _infeasible_result(evaluation: PlanEvaluation) -> dict[str, object]:
    """Build the structured result of an infeasible plan."""
    return {
        "ok": False,
        "error_code": ERROR_PLAN_INFEASIBLE,
        "error_message": "The plan is infeasible: " + "; ".join(evaluation.reasons),
        **evaluation.to_dict(),
    }


def _plan_confirmation(plan: EnergySavingPlan) -> dict[str, object]:
    """Build the result of an applied plan."""
    return {
        "ok": True,
        "message": (
            f"Plan {plan.plan_id} applied to cell {plan.cell_id}: "
            f"{plan.target_rate:g}% energy saving during {plan.window}."
        ),
        **plan.to_dict(),
    }


def _known_cells() -> list[str]:
    """Return the ids of all cells of the dataset."""
    return [cell.cell_id for cell in ran_data.load_cells()]


def get_cell_energy_profile(cell_id: str, date: str) -> dict[str, object]:
    """Read the 24-hour load and energy profile of one radio cell.

    Use this first, before evaluating any plan: it returns the cell's hourly load rate and energy
    consumption, its capacity / SLA limits, and the energy-saving windows the cell accepts.

    Args:
        cell_id: Cell identifier, for example ``cell-001``.
        date: Date of the profile in ``YYYY-MM-DD`` form, for example ``2026-10-22``.

    Returns:
        On success ``{"ok": True, "cell_id", "cell_name", "region", "date", "curve_date",
        "capacity", "max_saving_rate", "available_windows", "hourly_profile", "energy_summary"}``.
        On failure ``{"ok": False, "error_code", "error_message", ...}``.
    """
    try:
        ran_data.validate_date(date)
    except ran_data.InvalidPlanError as exc:
        return _error(ERROR_INVALID_DATE, str(exc), cell_id=cell_id, date=date)
    try:
        cell = ran_data.get_cell_or_raise(cell_id)
    except ran_data.UnknownCellError as exc:
        return _error(ERROR_UNKNOWN_CELL, str(exc), cell_id=cell_id, known_cells=_known_cells())

    return {
        "ok": True,
        "cell_id": cell.cell_id,
        "cell_name": cell.name,
        "region": cell.region,
        "date": date,
        "curve_date": ran_data.dataset_date(),
        "capacity": _capacity_dict(cell.capacity),
        "max_saving_rate": cell.max_saving_rate,
        "available_windows": list(cell.available_windows),
        "hourly_profile": [_hourly_dict(sample) for sample in cell.hourly],
        "energy_summary": _energy_summary(cell),
    }


def evaluate_energy_saving_plan(cell_id: str, window: str, target_rate: str) -> dict[str, object]:
    """Evaluate an energy-saving plan without applying it.

    Always call this after :func:`get_cell_energy_profile` and before
    :func:`apply_energy_saving_plan`. The evaluation checks the window, the cell's maximum saving
    rate, and the throughput SLA, and estimates the energy saving and the KPI impact.

    Args:
        cell_id: Cell identifier, for example ``cell-001``.
        window: Energy-saving window in ``HH:MM~HH:MM`` form, for example ``00:00~06:00``.
        target_rate: Energy-saving rate in percent, for example ``30`` or ``30%``.

    Returns:
        On success ``{"ok": True, "feasible": True, "window", "target_rate",
        "baseline_energy_kwh", "estimated_saving_kwh", "estimated_saving_percent", "kpi_impact",
        "reasons", "recommendation"}``. An infeasible plan returns ``{"ok": False,
        "error_code": "plan_infeasible", "feasible": False, ...}`` with the reasons and a
        recommendation. An unknown cell or malformed request returns the corresponding structured
        error.
    """
    try:
        evaluation = ran_data.evaluate_plan(cell_id, window, target_rate)
    except ran_data.UnknownCellError as exc:
        return _error(ERROR_UNKNOWN_CELL, str(exc), cell_id=cell_id, known_cells=_known_cells())
    except ran_data.InvalidPlanError as exc:
        return _error(
            ERROR_INVALID_REQUEST,
            str(exc),
            cell_id=cell_id,
            window=window,
            target_rate=target_rate,
        )

    if not evaluation.feasible:
        return _infeasible_result(evaluation)
    return {"ok": True, **evaluation.to_dict()}


def apply_energy_saving_plan(cell_id: str, window: str, target_rate: str) -> dict[str, object]:
    """Apply a feasible energy-saving plan and write it to the process-local plan store.

    Never call this before :func:`evaluate_energy_saving_plan`; the tool re-evaluates the plan and
    refuses infeasible plans, so nothing is ever written for an infeasible request.

    Args:
        cell_id: Cell identifier, for example ``cell-001``.
        window: Energy-saving window in ``HH:MM~HH:MM`` form, for example ``00:00~06:00``.
        target_rate: Energy-saving rate in percent, for example ``30`` or ``30%``.

    Returns:
        On success ``{"ok": True, "plan_id", "cell_id", "cell_name", "region", "date", "window",
        "target_rate", "status", "baseline_energy_kwh", "estimated_saving_kwh",
        "remaining_energy_kwh", "kpi_impact", "message"}``. Unknown cells, malformed requests, and
        infeasible plans return structured errors and write nothing.
    """
    try:
        plan = ran_data.apply_plan(cell_id, window, target_rate)
    except ran_data.UnknownCellError as exc:
        return _error(ERROR_UNKNOWN_CELL, str(exc), cell_id=cell_id, known_cells=_known_cells())
    except ran_data.PlanInfeasibleError as exc:
        return _infeasible_result(exc.evaluation)
    except ran_data.InvalidPlanError as exc:
        return _error(
            ERROR_INVALID_REQUEST,
            str(exc),
            cell_id=cell_id,
            window=window,
            target_rate=target_rate,
        )
    return _plan_confirmation(plan)


def generate_energy_saving_report(plan_id: str) -> dict[str, object]:
    """Generate the structured final report of an applied plan.

    Call this last, with the exact ``plan_id`` returned by :func:`apply_energy_saving_plan`. The
    report restates the plan, its energy saving, and its KPI impact for the Task artifact.

    Args:
        plan_id: Plan identifier returned by :func:`apply_energy_saving_plan`, for example
            ``plan-cell-001-001``.

    Returns:
        On success ``{"ok": True, "plan_id", "cell_id", "cell_name", "region", "date", "window",
        "target_rate", "status", "energy", "kpi_impact", "summary", "highlights"}``. An unknown
        plan returns ``{"ok": False, "error_code": "unknown_plan", "error_message", "plan_id",
        "known_plans"}``.
    """
    plan = ran_data.get_plan(plan_id)
    if plan is None:
        return _error(
            ERROR_UNKNOWN_PLAN,
            f"Unknown plan '{plan_id}'; apply a plan first and use the returned plan_id.",
            plan_id=plan_id,
            known_plans=list(ran_data.list_plan_ids()),
        )

    summary = (
        f"Plan {plan.plan_id} applied to cell {plan.cell_id} ({plan.cell_name}): "
        f"a {plan.target_rate:g}% energy-saving plan during {plan.window} on {plan.date}, "
        f"saving an estimated {plan.estimated_saving_kwh:.2f} kWh of "
        f"{plan.baseline_energy_kwh:.2f} kWh."
    )
    return {
        "ok": True,
        "plan_id": plan.plan_id,
        "cell_id": plan.cell_id,
        "cell_name": plan.cell_name,
        "region": plan.region,
        "date": plan.date,
        "window": plan.window,
        "target_rate": plan.target_rate,
        "status": plan.status,
        "energy": {
            "baseline_kwh": plan.baseline_energy_kwh,
            "saved_kwh": plan.estimated_saving_kwh,
            "remaining_kwh": plan.remaining_energy_kwh,
            "saving_percent": plan.target_rate,
        },
        "kpi_impact": dict(plan.kpi_impact),
        "summary": summary,
        "highlights": [
            f"Region: {plan.region}.",
            f"Plan {plan.plan_id} is {plan.status} for window {plan.window}.",
            f"Estimated saving: {plan.estimated_saving_kwh:.2f} kWh "
            f"({plan.target_rate:g}% of the window baseline).",
        ],
    }
