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
from shared.plan import CanonicalPlan

from demo.nms_sim.intent import DEFAULT_ACCEPT_RATE_MBPS, FULL_DAY_WINDOW, ParsedIntent

#: Permission the approval grants. Kept as a name, not an enforced ACL.
APPROVAL_SCOPE: Final[str] = "write:ran-energy-plan"

#: Text the status message carries while the plan waits; the object is in the metadata.
APPROVAL_PENDING_TEXT: Final[str] = "Approval required before this plan is executed."


def plan_for(intent: ParsedIntent) -> CanonicalPlan:
    """Return the canonical plan of one accepted intent.

    Only the fields the request fixes belong here, so both sides can reach the same object. The
    site list is excluded on purpose: it is server-side inventory, shown in the report text only,
    and a client cannot know it, so binding an approval to it would make the digest unverifiable.

    A field the request did not state becomes ``None`` rather than the default the execution path
    goes on to use. Echoing the default would put a number in the contract that the client never
    sent and cannot reproduce, which is the one thing a digest must not do.
    """
    unstated = set(intent.unstated)
    return {
        "operation": intent.operation,
        "region": intent.region,
        "window": None if "window" in unstated else intent.window,
        "rateMbps": None if "rateMbps" in unstated else intent.rate_mbps,
    }


def intent_from_plan(plan: Mapping[str, object]) -> ParsedIntent:
    """Rebuild the accepted intent a parked plan came from.

    The site list is not part of the bound plan, so the rebuilt intent leaves it empty; it only
    shapes the report text. A field the plan recorded as absent is filled here -- execution needs a
    number -- and marked unstated, so :func:`plan_for` reproduces the same plan it came from.
    """
    raw_rate = plan.get("rateMbps")
    rate = (
        float(raw_rate)
        if isinstance(raw_rate, (int, float)) and not isinstance(raw_rate, bool)
        else None
    )
    raw_window = plan.get("window")
    window = raw_window if isinstance(raw_window, str) and raw_window else ""
    unstated: list[str] = []
    if not window:
        window = FULL_DAY_WINDOW
        unstated.append("window")
    if rate is None:
        rate = DEFAULT_ACCEPT_RATE_MBPS
        unstated.append("rateMbps")
    return ParsedIntent(
        operation=str(plan.get("operation") or "Create"),
        region=str(plan.get("region") or ""),
        window=window,
        rate_mbps=rate,
        sites=(),
        source="",
        unstated=tuple(unstated),
    )


def build_request(
    intent: ParsedIntent,
    *,
    task_id: str,
    ttl_seconds: float,
    now: datetime | None = None,
    tamper: bool = False,
) -> ApprovalRequest:
    """Return the approval request for one accepted plan.

    ``tamper`` is the S6 measurement switch: the request carries the digest of a different plan,
    so a client that verifies the binding refuses it while a client that echoes the given digest
    accepts a plan nobody agreed to. The plan that would be executed is unchanged.
    """
    plan: Mapping[str, object] = plan_for(intent)
    if tamper:
        plan = {**plan, "rateMbps": _other_rate(plan.get("rateMbps"))}
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


def _other_rate(value: object) -> float:
    """Return a rate that differs from the real plan's, for the tamper switch."""
    rate = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    return rate + 1.0


__all__ = [
    "APPROVAL_PENDING_TEXT",
    "APPROVAL_SCOPE",
    "build_request",
    "intent_from_plan",
    "plan_for",
]
