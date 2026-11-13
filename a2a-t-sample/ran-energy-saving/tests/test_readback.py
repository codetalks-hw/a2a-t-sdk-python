"""Tests of the S8 readback envelope: comparison and verification."""

from __future__ import annotations

from shared.approval import plan_digest
from shared.readback import (
    STATUS_DEVIATED,
    STATUS_MATCHED,
    STATUS_UNVERIFIED,
    Readback,
    compare_plans,
    read_readback,
    verify,
)

PLAN = {
    "operation": "Create",
    "region": "A2ATestRegion",
    "window": "00:00-24:00",
    "rateMbps": 10.0,
}


def _readback(**executed: object) -> Readback:
    actual = {**PLAN, **executed}
    return Readback(
        plan_digest=plan_digest(PLAN),
        executed=actual,
        deviations=compare_plans(PLAN, actual),
    )


def test_an_identical_plan_has_no_deviations() -> None:
    assert compare_plans(PLAN, dict(PLAN)) == ()


def test_a_different_rate_is_one_deviation() -> None:
    deviations = compare_plans(PLAN, {**PLAN, "rateMbps": 8.0})

    assert deviations == ({"field": "rateMbps", "planned": 10.0, "actual": 8.0},)


def test_an_integer_rate_is_not_a_deviation() -> None:
    """``10`` and ``10.0`` are the same guaranteed rate, not a mismatch."""
    assert compare_plans(PLAN, {**PLAN, "rateMbps": 10}) == ()


def test_a_readback_round_trips_through_artifact_metadata() -> None:
    readback = _readback(rateMbps=8.0)

    assert read_readback(readback.to_metadata()) == readback


def test_a_malformed_readback_degrades_to_absent() -> None:
    assert read_readback(None) is None
    assert read_readback({"a2at-sample/readback": "nope"}) is None
    assert read_readback({"a2at-sample/readback": {"version": 9}}) is None


def test_a_client_with_the_accepted_plan_can_verify() -> None:
    readback = _readback()

    assert verify(readback, plan_digest(PLAN)) == STATUS_MATCHED


def test_a_client_without_the_accepted_plan_can_only_report_unverified() -> None:
    """The plain-A2A hop holds no plan object, so a readback stays a claim."""
    assert verify(_readback(), None) == STATUS_UNVERIFIED


def test_a_readback_of_another_plan_is_a_deviation() -> None:
    assert verify(_readback(), plan_digest({**PLAN, "rateMbps": 5.0})) == STATUS_DEVIATED


def test_an_executed_deviation_with_the_same_digest_is_reported() -> None:
    """The peer can execute differently even when it names the right plan."""
    assert verify(_readback(rateMbps=8.0), plan_digest(PLAN)) == STATUS_DEVIATED
