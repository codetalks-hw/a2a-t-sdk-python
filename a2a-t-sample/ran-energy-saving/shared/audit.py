"""Replay one exchange from artifacts alone (P5: audit without a model).

Every other capability in this sample rests on the same fact: both sides hold the accepted plan,
so each later claim can be checked against it. This module turns that into a verdict a third party
can reproduce: give it the artifacts of a run and it returns one status per link in the chain.

``matched``        the two artifacts agree
``mismatched``     both are present and disagree -- the concrete failure the chain exists to catch
``unverifiable``   one side is missing; on plain A2A this is the normal answer, because the accepted
                   response is prose and need not carry the executed values
``not_applicable`` the link does not exist for this run (a negotiated run legitimately accepts a
                   plan that differs from the request)

No language model, no log reading: every link is a projection and a digest comparison.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from a2a_t.core.plan_binding import plan_digest

from shared.assurance import AssuranceReport
from shared.plan import PLAN_FIELDS
from shared.readback import STATUS_DEVIATED, STATUS_MATCHED, STATUS_UNVERIFIED

#: Status of one link.
LINK_MATCHED: Final[str] = STATUS_MATCHED
LINK_MISMATCHED: Final[str] = STATUS_DEVIATED
LINK_UNVERIFIABLE: Final[str] = STATUS_UNVERIFIED
LINK_NOT_APPLICABLE: Final[str] = "not-applicable"

#: Link names, stable so a report can be diffed between runs.
LINK_REQUEST: Final[str] = "request->accepted"
LINK_APPROVAL: Final[str] = "accepted->approval"
LINK_EXECUTION: Final[str] = "approval->execution"
LINK_BASELINE: Final[str] = "change->baseline"
LINK_RESTORED: Final[str] = "execution->restored"


@dataclass(frozen=True)
class Link:
    """One verdict of the audit chain.

    Attributes:
        name: Which transition was checked.
        status: One of the four module-level statuses.
        detail: Short human-readable reason, empty when the link matched.
    """

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the report form of this verdict."""
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Artifacts:
    """What a run left behind, in the shape the chain checks.

    Attributes:
        requested_plan: Plan projected from the request the client sent.
        accepted_plan: Plan the client believes was agreed; ``None`` on a plain-A2A hop.
        accepted_digest: Digest the client computed for the accepted plan. Preferred over the
            projection when a negotiation changed values, because the flow already holds it.
        approved_digest: Digest carried by the approval request/decision; ``None`` without an
            approval.
        executed_plan: Plan reported as executed by the readback; ``None`` when no readback came.
        receipt_digest: Digest carried by the execution readback.
        baseline_plan: For a change order, the plan it declared as its baseline.
        baseline_digest: For a change order, the digest it declared.
        reversal_plan: Plan the accepted agreement promised as the state to return to; ``None``
            when the run has no reversal contract.
        restored_plan: Plan the reversal execution reported as applied; ``None`` when the contract
            was not executed.
        negotiated: Whether a negotiation changed values, making request != accepted legitimate.
        changed_by: Hops that declared a revision, i.e. that were allowed to change the contract.
            Preferred over ``negotiated`` because it is a statement rather than an inference.
    """

    requested_plan: Mapping[str, object] | None = None
    accepted_plan: Mapping[str, object] | None = None
    accepted_digest: str | None = None
    approved_digest: str | None = None
    executed_plan: Mapping[str, object] | None = None
    receipt_digest: str | None = None
    baseline_plan: Mapping[str, object] | None = None
    baseline_digest: str | None = None
    reversal_plan: Mapping[str, object] | None = None
    restored_plan: Mapping[str, object] | None = None
    negotiated: bool = False
    changed_by: tuple[str, ...] = ()


def verify_chain(
    artifacts: Artifacts, fields: Sequence[str] = PLAN_FIELDS
) -> tuple[Link, ...]:
    """Return one verdict per link of the chain, in a stable order.

    The restored link is appended only when the run carried a reversal contract. A run without one
    has no such question to ask, so its chain keeps the original four links exactly.
    """
    links = [
        _request_link(artifacts, fields),
        _approval_link(artifacts, fields),
        _execution_link(artifacts, fields),
        _baseline_link(artifacts, fields),
    ]
    if artifacts.reversal_plan is not None:
        links.append(_restored_link(artifacts, fields))
    return tuple(links)


def summarize(links: Sequence[Link], *, assurance: AssuranceReport | None = None) -> dict[str, object]:
    """Return the counts a report shows: how many links are decidable and how many agree."""
    applicable = [link for link in links if link.status != LINK_NOT_APPLICABLE]
    summary: dict[str, object] = {
        "links": len(links),
        "applicable": len(applicable),
        "matched": sum(1 for link in applicable if link.status == LINK_MATCHED),
        "mismatched": sum(1 for link in applicable if link.status == LINK_MISMATCHED),
        "unverifiable": sum(1 for link in applicable if link.status == LINK_UNVERIFIABLE),
        "notApplicable": len(links) - len(applicable),
        "decidedBy": "digest",
    }
    if assurance is not None:
        summary["assurance"] = assurance.to_dict()
    return summary


def _request_link(artifacts: Artifacts, fields: Sequence[str]) -> Link:
    if artifacts.changed_by:
        # A named hop declared a revision, so the difference is accounted for rather than assumed.
        return Link(
            LINK_REQUEST,
            LINK_NOT_APPLICABLE,
            "changed by " + ", ".join(artifacts.changed_by),
        )
    if artifacts.negotiated:
        return Link(
            LINK_REQUEST,
            LINK_NOT_APPLICABLE,
            "the accepted plan differs from the request by agreement",
        )
    if artifacts.requested_plan is None or artifacts.accepted_plan is None:
        return Link(LINK_REQUEST, LINK_UNVERIFIABLE, "no accepted plan object")
    return _compare(LINK_REQUEST, artifacts.requested_plan, artifacts.accepted_plan, fields)


def _approval_link(artifacts: Artifacts, fields: Sequence[str]) -> Link:
    if artifacts.approved_digest is None:
        return Link(LINK_APPROVAL, LINK_UNVERIFIABLE, "no approval on this run")
    expected = artifacts.accepted_digest
    if expected is None and artifacts.accepted_plan is not None:
        expected = plan_digest(artifacts.accepted_plan, fields)
    if expected is None:
        return Link(
            LINK_APPROVAL,
            LINK_UNVERIFIABLE,
            "no accepted plan to recompute the approved digest from",
        )
    return _digest_link(LINK_APPROVAL, expected, artifacts.approved_digest)


def _execution_link(artifacts: Artifacts, fields: Sequence[str]) -> Link:
    if artifacts.receipt_digest is None:
        return Link(LINK_EXECUTION, LINK_UNVERIFIABLE, "no execution readback")
    if artifacts.approved_digest is not None and artifacts.receipt_digest != artifacts.approved_digest:
        return Link(
            LINK_EXECUTION,
            LINK_MISMATCHED,
            "the executed plan is not the approved one",
        )
    if artifacts.accepted_plan is not None and artifacts.executed_plan is not None:
        return _compare(
            LINK_EXECUTION, artifacts.accepted_plan, artifacts.executed_plan, fields
        )
    if artifacts.accepted_plan is None:
        if artifacts.accepted_digest is None:
            return Link(
                LINK_EXECUTION,
                LINK_UNVERIFIABLE,
                "no accepted plan to compare the execution with",
            )
        return _digest_link(LINK_EXECUTION, artifacts.accepted_digest, artifacts.receipt_digest)
    # An accepted plan is held but the readback reported no executed plan. The receipt digest may
    # already have matched the approval, but that compares the readback with the approval, not with
    # the values any hop applied -- so this link cannot be called agreement.
    return Link(
        LINK_EXECUTION,
        LINK_UNVERIFIABLE,
        "the readback reports no executed plan to compare with the accepted one",
    )


def _baseline_link(artifacts: Artifacts, fields: Sequence[str]) -> Link:
    if artifacts.baseline_digest is None and artifacts.baseline_plan is None:
        if not _is_change_order(artifacts):
            # A Create has no plan to replace, so the question does not exist rather than going
            # unanswered. Calling it unverifiable would make every Create look like an audit gap.
            return Link(LINK_BASELINE, LINK_NOT_APPLICABLE, "this run is not a change order")
        return Link(LINK_BASELINE, LINK_UNVERIFIABLE, "no change order on this run")
    if artifacts.baseline_plan is None:
        return Link(
            LINK_BASELINE,
            LINK_UNVERIFIABLE,
            "a baseline was declared but no baseline plan was carried to recompute it from",
        )
    if artifacts.baseline_digest is None:
        return Link(
            LINK_BASELINE,
            LINK_UNVERIFIABLE,
            "a baseline plan was carried but no digest was declared to check it against",
        )
    return _digest_link(
        LINK_BASELINE, plan_digest(artifacts.baseline_plan, fields), artifacts.baseline_digest
    )


def _restored_link(artifacts: Artifacts, fields: Sequence[str]) -> Link:
    restored_plan = artifacts.restored_plan
    if restored_plan is None:
        return Link(
            LINK_RESTORED,
            LINK_NOT_APPLICABLE,
            "the reversal contract was not executed",
        )
    reversal_plan = artifacts.reversal_plan
    if reversal_plan is None:
        # Only reachable when called directly; verify_chain appends this link only with a contract.
        return Link(LINK_RESTORED, LINK_UNVERIFIABLE, "no reversal plan to compare with")
    return _compare(LINK_RESTORED, reversal_plan, restored_plan, fields)


#: The one operation a change order carries. Compared case-sensitively on purpose: it is a wire
#: value, and a client that spells it differently has a different problem.
CHANGE_ORDER_OPERATION: Final[str] = "Modify"


def _is_change_order(artifacts: Artifacts) -> bool:
    """Return whether this run asked to replace an existing plan."""
    plan = artifacts.requested_plan
    if plan is None:
        return False
    return str(plan.get("operation") or "") == CHANGE_ORDER_OPERATION


def _compare(
    name: str,
    planned: Mapping[str, object],
    actual: Mapping[str, object],
    fields: Sequence[str],
) -> Link:
    planned_digest = plan_digest(planned, fields)
    actual_digest = plan_digest(actual, fields)
    if planned_digest == actual_digest:
        return Link(name, LINK_MATCHED, "")
    return Link(name, LINK_MISMATCHED, f"{planned_digest} != {actual_digest}")


def _digest_link(name: str, expected: str, actual: str) -> Link:
    if expected == actual:
        return Link(name, LINK_MATCHED, "")
    return Link(name, LINK_MISMATCHED, f"{expected} != {actual}")


__all__ = [
    "LINK_APPROVAL",
    "LINK_BASELINE",
    "LINK_EXECUTION",
    "LINK_MATCHED",
    "LINK_MISMATCHED",
    "LINK_NOT_APPLICABLE",
    "LINK_REQUEST",
    "LINK_RESTORED",
    "LINK_UNVERIFIABLE",
    "Artifacts",
    "Link",
    "summarize",
    "verify_chain",
]
