"""The sample-local approval envelope (S6: approval as state).

Approval already exists inside the Task lifecycle -- the server publishes ``INPUT_REQUIRED`` and
continues on ``WORKING`` -- but the *message* carries none of it: the request is a sentence, the
decision travels over the sample's private ``POST /approvals`` route, and the decision is a bare
boolean whose reason is never asked for. That makes approval a screen, not a state object.

This module defines the object both sides agree on. It is deliberately sample-local (the same
stance as ``a2at-sample/missing-items``): the point of S6 is not that A2A cannot carry it -- a
``Struct`` metadata map and a follow-up ``Message`` are standard -- but that an approval has to be
**bound to the plan it approves**. Without ``planDigest``, an approval can be replayed onto a plan
that has since changed, and no protocol in this sample can tell.

The digest can be computed from the accepted Negotiation-T proposal on an A2A-T hop, where both
sides hold the same object. On a plain-A2A hop it can only be computed from prose, which is the
difference S6 measures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from a2a_t.core.plan_binding import plan_digest as _sdk_plan_digest

from shared.plan import PLAN_FIELDS

#: Sample-local metadata key of the envelope. Not a registered A2A-T extension URI.
APPROVAL_METADATA_KEY: Final[str] = "a2at-sample/approval"

#: Envelope schema version, so a reader can refuse a shape it does not know.
APPROVAL_VERSION: Final[int] = 1

#: Envelope kinds.
KIND_REQUEST: Final[str] = "approval-request"
KIND_DECISION: Final[str] = "approval-decision"

#: Decision outcomes.
OUTCOME_APPROVE: Final[str] = "approve"
OUTCOME_REJECT: Final[str] = "reject"

#: Reason codes a decision may carry. A free-form ``reason`` travels next to the code; the code is
#: what a client is allowed to branch on.
REASON_APPROVED: Final[str] = "operator_accepted"
REASON_RATE_ABOVE_POLICY: Final[str] = "rate_above_policy"
REASON_WINDOW_CONFLICT: Final[str] = "window_conflict"
REASON_OPERATOR_DECLINED: Final[str] = "operator_declined"
REASON_PLAN_STALE: Final[str] = "plan_stale"
REASON_EXPIRED: Final[str] = "expired"

#: Order a client should try when it has to re-plan after a rejection.
REPLANNABLE_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_RATE_ABOVE_POLICY, REASON_WINDOW_CONFLICT}
)

#: Verification failures of :func:`verify_decision`.
VERIFY_UNKNOWN: Final[str] = "approval-unknown"
VERIFY_STALE: Final[str] = "approval-stale"
VERIFY_EXPIRED: Final[str] = "approval-expired"


def plan_digest(plan: Mapping[str, object]) -> str:
    """Return the canonical digest of one plan.

    Both sides must reach the same value from the same plan. The SDK owns the canonical form; this
    function only fixes the sample's field list, so a field a client cannot compute -- the site
    inventory -- can never enter the digest by being present in the mapping.
    """
    return _sdk_plan_digest(plan, PLAN_FIELDS)


@dataclass(frozen=True)
class ApprovalRequest:
    """One pending approval the server is waiting for.

    Attributes:
        approval_id: Identity of this request. A decision must echo it.
        plan_digest: Digest of the plan being approved. A decision must echo it.
        scope: Human-readable permission names the approval grants.
        summary: One line describing what will happen if approved.
        expires_at: ISO-8601 instant after which the request is no longer decidable.
        replan_on_reject: Whether a rejection leaves the task open for a new proposal.
        task_id: Task the approval belongs to, when the server knows it.
    """

    approval_id: str
    plan_digest: str
    scope: tuple[str, ...] = ()
    summary: str = ""
    expires_at: str | None = None
    replan_on_reject: bool = False
    task_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this request."""
        payload: dict[str, object] = {
            "version": APPROVAL_VERSION,
            "kind": KIND_REQUEST,
            "approvalId": self.approval_id,
            "planDigest": self.plan_digest,
            "scope": list(self.scope),
            "summary": self.summary,
            "onReject": {"replan": self.replan_on_reject},
        }
        if self.expires_at is not None:
            payload["expiresAt"] = self.expires_at
        if self.task_id is not None:
            payload["taskId"] = self.task_id
        return payload

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this request."""
        return {APPROVAL_METADATA_KEY: self.to_dict()}


@dataclass(frozen=True)
class ApprovalDecision:
    """One answer to an :class:`ApprovalRequest`.

    Attributes:
        approval_id: The request this answers.
        plan_digest: The plan the approver believed they were approving.
        outcome: ``approve`` or ``reject``.
        reason_code: Machine-readable reason; required for a rejection.
        reason: Free-form explanation for the approval record.
        decided_by: Who decided (role or identity), empty when unknown.
        decided_at: ISO-8601 instant of the decision.
    """

    approval_id: str
    plan_digest: str
    outcome: str
    reason_code: str = ""
    reason: str = ""
    decided_by: str = ""
    decided_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this decision."""
        payload: dict[str, object] = {
            "version": APPROVAL_VERSION,
            "kind": KIND_DECISION,
            "approvalId": self.approval_id,
            "planDigest": self.plan_digest,
            "outcome": self.outcome,
            "reasonCode": self.reason_code,
            "reason": self.reason,
            "decidedBy": self.decided_by,
        }
        if self.decided_at is not None:
            payload["decidedAt"] = self.decided_at
        return payload

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this decision."""
        return {APPROVAL_METADATA_KEY: self.to_dict()}


def read_approval_request(metadata: Mapping[str, object] | None) -> ApprovalRequest | None:
    """Read an approval request from one message's metadata, ``None`` when absent or malformed.

    A malformed envelope degrades to the prose gate instead of raising: an unknown shape must never
    make an approval impossible.
    """
    raw = _read_envelope(metadata, KIND_REQUEST)
    if raw is None:
        return None
    approval_id = raw.get("approvalId")
    digest = raw.get("planDigest")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    scope = raw.get("scope")
    on_reject = raw.get("onReject")
    return ApprovalRequest(
        approval_id=approval_id,
        plan_digest=digest,
        scope=tuple(str(entry) for entry in scope) if isinstance(scope, list) else (),
        summary=str(raw.get("summary") or ""),
        expires_at=_optional_str(raw.get("expiresAt")),
        replan_on_reject=bool(on_reject.get("replan")) if isinstance(on_reject, Mapping) else False,
        task_id=_optional_str(raw.get("taskId")),
    )


def read_approval_decision(metadata: Mapping[str, object] | None) -> ApprovalDecision | None:
    """Read an approval decision from one message's metadata, ``None`` when absent or malformed."""
    raw = _read_envelope(metadata, KIND_DECISION)
    if raw is None:
        return None
    approval_id = raw.get("approvalId")
    digest = raw.get("planDigest")
    outcome = _optional_str(raw.get("outcome"))
    if not isinstance(approval_id, str) or not approval_id:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    if outcome is None or outcome not in {OUTCOME_APPROVE, OUTCOME_REJECT}:
        return None
    return ApprovalDecision(
        approval_id=approval_id,
        plan_digest=digest,
        outcome=outcome,
        reason_code=str(raw.get("reasonCode") or ""),
        reason=str(raw.get("reason") or ""),
        decided_by=str(raw.get("decidedBy") or ""),
        decided_at=_optional_str(raw.get("decidedAt")),
    )


def _optional_str(value: object) -> str | None:
    """Return ``value`` when it is a string, ``None`` otherwise."""
    return value if isinstance(value, str) else None


def verify_decision(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return why a decision cannot be applied, ``None`` when it can.

    The order matters: identity first, then the plan, then the clock. A decision for a plan that has
    since changed is ``approval-stale`` even if it is young, because executing it would apply an
    approval to something nobody approved.
    """
    if decision.approval_id != request.approval_id:
        return VERIFY_UNKNOWN
    if decision.plan_digest != request.plan_digest:
        return VERIFY_STALE
    if request.expires_at:
        instant = _parse_instant(request.expires_at)
        if instant is not None:
            current = now or datetime.now().astimezone()
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=current.tzinfo)
            if current >= instant:
                return VERIFY_EXPIRED
    return None


def _parse_instant(text: str) -> datetime | None:
    """Parse an ISO-8601 instant, ``None`` when it is not one."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_envelope(
    metadata: Mapping[str, object] | None, expected_kind: str
) -> Mapping[str, object] | None:
    """Return the envelope of the expected kind, ``None`` when absent, malformed or a different kind."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(APPROVAL_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version != APPROVAL_VERSION or raw.get("kind") != expected_kind:
        return None
    return raw


__all__ = [
    "APPROVAL_METADATA_KEY",
    "APPROVAL_VERSION",
    "KIND_DECISION",
    "KIND_REQUEST",
    "OUTCOME_APPROVE",
    "OUTCOME_REJECT",
    "REASON_APPROVED",
    "REASON_EXPIRED",
    "REASON_OPERATOR_DECLINED",
    "REASON_PLAN_STALE",
    "REASON_RATE_ABOVE_POLICY",
    "REASON_WINDOW_CONFLICT",
    "REPLANNABLE_REASONS",
    "VERIFY_EXPIRED",
    "VERIFY_STALE",
    "VERIFY_UNKNOWN",
    "ApprovalDecision",
    "ApprovalRequest",
    "plan_digest",
    "read_approval_decision",
    "read_approval_request",
    "verify_decision",
]
