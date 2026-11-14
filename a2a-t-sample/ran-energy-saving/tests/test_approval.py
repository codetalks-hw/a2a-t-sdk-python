"""Tests of the S6 approval envelope: identity, plan binding, expiry."""

from __future__ import annotations

from datetime import UTC, datetime

from shared.approval import (
    APPROVAL_METADATA_KEY,
    KIND_REQUEST,
    OUTCOME_APPROVE,
    OUTCOME_REJECT,
    REASON_RATE_ABOVE_POLICY,
    VERIFY_EXPIRED,
    VERIFY_STALE,
    VERIFY_UNKNOWN,
    ApprovalDecision,
    ApprovalRequest,
    plan_digest,
    read_approval_decision,
    read_approval_request,
    verify_decision,
)

PLAN = {
    "operation": "Create",
    "region": "A2ATestRegion",
    "window": "08:00-10:00",
    "rateMbps": 2.0,
}


def _request(**overrides: object) -> ApprovalRequest:
    fields: dict[str, object] = {
        "approval_id": "apv-1",
        "plan_digest": plan_digest(PLAN),
        "scope": ("write:ran-energy-plan",),
        "summary": "Lower the guarantee to 2 Mbps during the outage.",
        "replan_on_reject": True,
    }
    fields.update(overrides)
    return ApprovalRequest(**fields)  # type: ignore[arg-type]


def test_the_digest_ignores_key_order_and_changes_with_the_plan() -> None:
    """The digest is the approval's identity, so formatting must not change it."""
    reordered = {
        "rateMbps": 2,
        "window": "08:00-10:00",
        "region": "A2ATestRegion",
        "operation": "Create",
    }
    changed = {**PLAN, "rateMbps": 10.0}

    assert plan_digest(PLAN) == plan_digest(reordered)
    assert plan_digest(PLAN) != plan_digest(changed)
    assert plan_digest(PLAN).startswith("sha256:")


def test_the_digest_ignores_values_the_client_cannot_see() -> None:
    """A digest over server inventory is a claim, not a binding: those fields must not count."""
    server_side = {
        **PLAN,
        "sites": ["LNR_KPI_7745", "NES_27A_321071"],
        "savingRate": 0.3,
    }

    assert plan_digest(server_side) == plan_digest(PLAN)


def test_a_request_round_trips_through_message_metadata() -> None:
    request = _request(task_id="task-1")

    read = read_approval_request(request.to_metadata())

    assert read == request


def test_a_decision_round_trips_through_message_metadata() -> None:
    decision = ApprovalDecision(
        approval_id="apv-1",
        plan_digest=plan_digest(PLAN),
        outcome=OUTCOME_REJECT,
        reason_code=REASON_RATE_ABOVE_POLICY,
        reason="2 Mbps is below the site policy floor.",
        decided_by="engineer:zhang",
    )

    assert read_approval_decision(decision.to_metadata()) == decision


def test_a_malformed_envelope_degrades_to_absent() -> None:
    """An unknown shape must fall back to the prose gate, not break the exchange."""
    assert read_approval_request(None) is None
    assert read_approval_request({APPROVAL_METADATA_KEY: "not a mapping"}) is None
    assert read_approval_request({APPROVAL_METADATA_KEY: {"version": 99, "kind": KIND_REQUEST}}) is None
    assert (
        read_approval_request({APPROVAL_METADATA_KEY: {"version": 1, "kind": KIND_REQUEST}})
        is None
    )


def test_a_decision_cannot_answer_another_request() -> None:
    decision = ApprovalDecision(
        approval_id="apv-2",
        plan_digest=_request().plan_digest,
        outcome=OUTCOME_APPROVE,
    )

    assert verify_decision(_request(), decision) == VERIFY_UNKNOWN


def test_an_approval_for_a_changed_plan_is_stale() -> None:
    """The whole point of the digest: a late approval must not land on the new plan."""
    approved = _request()
    decision = ApprovalDecision(
        approval_id=approved.approval_id,
        plan_digest=plan_digest({**PLAN, "rateMbps": 5.0}),
        outcome=OUTCOME_APPROVE,
    )

    assert verify_decision(approved, decision) == VERIFY_STALE


def test_an_expired_request_cannot_be_approved() -> None:
    request = _request(expires_at="2026-11-13T00:00:00+00:00")
    decision = ApprovalDecision(
        approval_id=request.approval_id,
        plan_digest=request.plan_digest,
        outcome=OUTCOME_APPROVE,
    )

    assert (
        verify_decision(request, decision, now=datetime(2026, 11, 13, 1, tzinfo=UTC))
        == VERIFY_EXPIRED
    )
    assert (
        verify_decision(request, decision, now=datetime(2026, 11, 12, 23, tzinfo=UTC)) is None
    )


def test_matching_identity_and_plan_is_accepted() -> None:
    request = _request()
    decision = ApprovalDecision(
        approval_id=request.approval_id,
        plan_digest=request.plan_digest,
        outcome=OUTCOME_APPROVE,
    )

    assert verify_decision(request, decision) is None
