"""Server half of the S6 approval envelope.

The simulator already validates a request and then executes it in one step. Under ``approval`` it
splits that into two: the accepted plan is parked in ``INPUT_REQUIRED`` with a request object, and
execution only starts when a decision that matches the request's ``planDigest`` arrives.

The plan is the *approved object*, not the request text: sites, window, rate and operation. A plan
that changed after the request was issued produces a different digest, which is how a stale
approval is refused instead of silently applied.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

from shared.approval import ApprovalRequest, plan_digest

from demo.nms_sim.intent import ParsedIntent

#: Permission the approval grants. Kept as a name, not an enforced ACL.
APPROVAL_SCOPE: Final[str] = "write:ran-energy-plan"

#: Text the status message carries while the plan waits; the object is in the metadata.
APPROVAL_PENDING_TEXT: Final[str] = "Approval required before this plan is executed."


def plan_for(intent: ParsedIntent) -> dict[str, object]:
    """Return the canonical plan of one accepted intent.

    Only the fields that change what would be executed belong here. The report text and the
    original prompt do not: including them would make two approvals of the same plan differ.
    """
    return {
        "operation": intent.operation,
        "region": intent.region,
        "window": intent.window,
        "rateMbps": intent.rate_mbps,
        "sites": list(intent.sites),
    }


def intent_from_plan(plan: Mapping[str, object]) -> ParsedIntent:
    """Rebuild the accepted intent a parked plan came from."""
    sites = plan.get("sites")
    rate = plan.get("rateMbps")
    return ParsedIntent(
        operation=str(plan.get("operation") or "Create"),
        region=str(plan.get("region") or ""),
        window=str(plan.get("window") or ""),
        rate_mbps=(
            float(rate)
            if isinstance(rate, (int, float)) and not isinstance(rate, bool)
            else 0.0
        ),
        sites=tuple(str(site) for site in sites) if isinstance(sites, list) else (),
        source="",
    )


def build_request(
    intent: ParsedIntent,
    *,
    task_id: str,
    ttl_seconds: float,
    now: datetime | None = None,
) -> ApprovalRequest:
    """Return the approval request for one accepted plan."""
    plan = plan_for(intent)
    expires = (now or datetime.now(UTC)) + timedelta(seconds=max(0.0, ttl_seconds))
    return ApprovalRequest(
        approval_id=f"apv-{uuid.uuid4().hex[:12]}",
        plan_digest=plan_digest(plan),
        scope=(APPROVAL_SCOPE,),
        summary=(
            f"{intent.operation} {intent.region} at {intent.rate_mbps:g} Mbps "
            f"during {intent.window}."
        ),
        expires_at=expires.isoformat().replace("+00:00", "Z"),
        replan_on_reject=True,
        task_id=task_id,
    )


__all__ = [
    "APPROVAL_PENDING_TEXT",
    "APPROVAL_SCOPE",
    "build_request",
    "intent_from_plan",
    "plan_for",
]
