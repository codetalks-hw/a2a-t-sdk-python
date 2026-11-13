"""The sample's policy for an ``a2at-sample/approval`` request (S6).

The protocol layer supplies the hook; the decision is business policy, so it lives here. The
deterministic answer exists so an S6 run does not need a human at the screen: it echoes the
request's identity and digest, which is exactly what a real approver's client would do after the
operator clicked approve.
"""

from __future__ import annotations

from collections.abc import Callable

from shared.approval import (
    OUTCOME_APPROVE,
    OUTCOME_REJECT,
    REASON_APPROVED,
    REASON_OPERATOR_DECLINED,
    ApprovalDecision,
    ApprovalRequest,
)

#: Rejection sentence of the deterministic policy; the code is what a client branches on.
REJECTION_REASON: str = "The operator declined this plan."


def approval_answer(outcome: str = OUTCOME_APPROVE) -> Callable[[ApprovalRequest], ApprovalDecision]:
    """Return a callback that answers every request with the configured outcome."""
    if outcome == OUTCOME_REJECT:
        return _reject
    return _approve


def _approve(request: ApprovalRequest) -> ApprovalDecision:
    """Approve exactly the plan that was requested."""
    return ApprovalDecision(
        approval_id=request.approval_id,
        plan_digest=request.plan_digest,
        outcome=OUTCOME_APPROVE,
        reason_code=REASON_APPROVED,
        reason="",
        decided_by="sample-policy",
    )


def _reject(request: ApprovalRequest) -> ApprovalDecision:
    """Reject exactly the plan that was requested, with a machine-readable reason."""
    return ApprovalDecision(
        approval_id=request.approval_id,
        plan_digest=request.plan_digest,
        outcome=OUTCOME_REJECT,
        reason_code=REASON_OPERATOR_DECLINED,
        reason=REJECTION_REASON,
        decided_by="sample-policy",
    )


__all__ = ["REJECTION_REASON", "approval_answer"]
