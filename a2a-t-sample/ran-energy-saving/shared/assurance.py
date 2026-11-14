"""Grade how well each hop was bound (v2: honest grading).

A hop that cannot recompute the contract is not a failure -- it is a lower grade. A mixed network
is the normal case (an A2A-T client, a plain-A2A middle hop, a vendor NMS), and the question a
report has to answer is not "did every link pass" but "which grade the weakest hop earned".
Collapsing that to a boolean either flatters a prose echo or fails an honest hop, so the report
keeps the vocabulary and the ordering here, in one place.

Grades, best first:

``recomputed`` the hop computed the digest itself and it matches
``echoed``     the hop returned a digest it did not compute
``declared``   the hop names a contract but no digest can be checked
``unverified`` nothing to check

A grade is never upgraded by assumption: an unknown string, a malformed entry, a missing field or a
``recomputed`` claim without a digest all fall to a lower grade rather than to trust.

Roles answer a different question: *what did the hop do to the contract?*

``carrier``  forwarded it unchanged -- its output digest must equal its input digest
``reducer``  may change it -- but then it must name the revision it produced
``unknown``  made no claim; a change here is attributed to nobody

A hop that parses intent and rewrites the prose (the OSS proxy is one) is a reducer. Without this
vocabulary a chain can only say *that* the contract changed, not whether anyone was allowed to
change it, so the audit ends up inferring a reason from two digests that differ.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: The hop recomputed the digest itself and it matches.
ASSURANCE_RECOMPUTED: Final[str] = "recomputed"

#: The hop returned a digest it did not compute.
ASSURANCE_ECHOED: Final[str] = "echoed"

#: The hop names a contract but no digest can be checked.
ASSURANCE_DECLARED: Final[str] = "declared"

#: Nothing to check.
ASSURANCE_UNVERIFIED: Final[str] = "unverified"

#: Every grade, best first. A grade outside this tuple is read as ``unverified``.
ASSURANCE_ORDER: Final[tuple[str, ...]] = (
    ASSURANCE_RECOMPUTED,
    ASSURANCE_ECHOED,
    ASSURANCE_DECLARED,
    ASSURANCE_UNVERIFIED,
)

#: The hop forwarded the contract unchanged; its output digest must equal its input digest.
ROLE_CARRIER: Final[str] = "carrier"

#: The hop may change the contract, and must then name the revision it produced.
ROLE_REDUCER: Final[str] = "reducer"

#: The hop made no claim about what it did; a change it makes cannot be attributed.
ROLE_UNKNOWN: Final[str] = "unknown"

#: Every role. A role outside this tuple is read as ``unknown``.
ROLE_ORDER: Final[tuple[str, ...]] = (ROLE_CARRIER, ROLE_REDUCER, ROLE_UNKNOWN)

#: A carrier's output digest differs from its input digest.
VIOLATION_CARRIER_CHANGED: Final[str] = "carrier-changed-contract"

#: A reducer changed the contract without naming a revision.
VIOLATION_REDUCER_UNREVISED: Final[str] = "reducer-without-revision"

#: A hop with no declared role changed the contract.
VIOLATION_UNATTRIBUTED: Final[str] = "unattributed-change"

#: A hop received a contract that is not what the previous hop says it sent.
VIOLATION_CHAIN_GAP: Final[str] = "chain-gap"


@dataclass(frozen=True)
class Hop:
    """One hop of a delegation chain and the grade it earned.

    Attributes:
        hop: Name of the hop, e.g. ``ui`` or ``nms``.
        digest: Digest of the contract the hop received, empty when it published none.
        assurance: One of the module-level grades.
        role: What the hop did with the contract; one of the module-level roles.
        emitted: Digest of the contract the hop sent on, empty when it did not say.
        revision: Revision the hop produced, for a reducer.
    """

    hop: str
    digest: str = ""
    assurance: str = ASSURANCE_UNVERIFIED
    role: str = ROLE_UNKNOWN
    emitted: str = ""
    revision: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the v2 object form; absent claims stay absent rather than defaulting to trust."""
        payload: dict[str, object] = {"hop": self.hop}
        if self.digest:
            payload["digest"] = self.digest
        if self.role != ROLE_UNKNOWN:
            payload["role"] = self.role
        if self.emitted:
            payload["emitted"] = self.emitted
        if self.revision is not None:
            payload["revision"] = self.revision
        payload["assurance"] = self.assurance
        return payload

    @staticmethod
    def from_mapping(raw: object) -> Hop | None:
        """Read one hop in the v2 object form or the legacy bare-string form.

        The legacy form carries only a name, so it can never be better than ``declared``: the hop
        named a contract but published nothing to check it against.
        """
        if isinstance(raw, str):
            name = raw.strip()
            return Hop(name, "", ASSURANCE_DECLARED) if name else None
        if not isinstance(raw, Mapping):
            return None
        raw_name = raw.get("hop")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None
        raw_digest = raw.get("digest")
        raw_assurance = raw.get("assurance")
        raw_role = raw.get("role")
        raw_emitted = raw.get("emitted")
        raw_revision = raw.get("revision")
        return Hop(
            hop=raw_name.strip(),
            digest=raw_digest if isinstance(raw_digest, str) else "",
            assurance=raw_assurance
            if isinstance(raw_assurance, str) and raw_assurance in ASSURANCE_ORDER
            else ASSURANCE_UNVERIFIED,
            role=raw_role if isinstance(raw_role, str) and raw_role in ROLE_ORDER else ROLE_UNKNOWN,
            emitted=raw_emitted if isinstance(raw_emitted, str) else "",
            revision=raw_revision
            if isinstance(raw_revision, int) and not isinstance(raw_revision, bool)
            else None,
        )


@dataclass(frozen=True)
class RoleViolation:
    """One contradiction between what a hop was allowed to do and what the chain says it did.

    Attributes:
        hop: Name of the hop the contradiction is attributed to.
        reason: One of the module-level ``VIOLATION_*`` reasons.
        detail: Short human-readable explanation, for a refusal message.
    """

    hop: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the wire form a refusal or a report carries."""
        payload: dict[str, object] = {"hop": self.hop, "reasonCode": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class AssuranceReport:
    """The graded chain as a report shows it.

    Attributes:
        hops: Hops in delegated order, as read.
        weakest: Grade of the weakest hop, after the downgrades in :func:`report`.
        changed_at: Names of the hops that published a digest other than the expected one.
    """

    hops: tuple[Hop, ...]
    weakest: str
    changed_at: tuple[str, ...] = ()
    violations: tuple[RoleViolation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this report."""
        return {
            "hops": [hop.to_dict() for hop in self.hops],
            "weakest": self.weakest,
            "changedAt": list(self.changed_at),
            "violations": [violation.to_dict() for violation in self.violations],
        }


def weakest(hops: Sequence[Hop]) -> str:
    """Return the lowest-ranked grade among ``hops``; ``unverified`` when there are none.

    A hop is graded at what it can be believed at rather than at what it claims: an unknown grade
    reads as ``unverified`` and a ``recomputed`` claim without a digest reads as ``declared``.
    """
    if not hops:
        return ASSURANCE_UNVERIFIED
    return max((_believable(hop) for hop in hops), key=ASSURANCE_ORDER.index)


def report(hops: Sequence[Hop], *, expected_digest: str | None = None) -> AssuranceReport:
    """Grade a chain, recording which hops published a different contract.

    A hop that changed the contract cannot be grading the contract that was agreed, so it cannot
    raise the chain above ``declared`` -- but it never raises a lower grade either.
    """
    changed_at: list[str] = []
    if expected_digest is not None:
        for hop in hops:
            if hop.digest and hop.digest != expected_digest and hop.hop not in changed_at:
                changed_at.append(hop.hop)
    grade = weakest(hops)
    if changed_at and ASSURANCE_ORDER.index(grade) < ASSURANCE_ORDER.index(ASSURANCE_DECLARED):
        grade = ASSURANCE_DECLARED
    return AssuranceReport(
        hops=tuple(hops),
        weakest=grade,
        changed_at=tuple(changed_at),
        violations=check_roles(hops),
    )


def check_roles(hops: Sequence[Hop]) -> tuple[RoleViolation, ...]:
    """Return every contradiction the chain states about what its hops were allowed to do.

    Three per-hop rules and one chain rule, all decided from the record itself:

    - a ``carrier`` whose output differs from its input changed something it said it would not;
    - a ``reducer`` that changed the contract must name the revision it produced, otherwise the
      change is unattributable;
    - a hop with no role that changed the contract is the same problem without even a claim;
    - consecutive hops must agree: what one says it sent is what the next says it received.

    The last rule is what makes a lying hop unnecessary to catch: it is enough that the *next* hop
    reports what actually arrived.
    """
    violations: list[RoleViolation] = []
    for index, hop in enumerate(hops):
        changed = bool(hop.digest and hop.emitted and hop.digest != hop.emitted)
        if hop.role == ROLE_CARRIER and changed:
            violations.append(
                RoleViolation(
                    hop.hop,
                    VIOLATION_CARRIER_CHANGED,
                    "a carrier hop changed the contract it promised to forward",
                )
            )
        elif hop.role == ROLE_REDUCER and changed and hop.revision is None:
            violations.append(
                RoleViolation(
                    hop.hop,
                    VIOLATION_REDUCER_UNREVISED,
                    "a reducer hop changed the contract without naming a new revision",
                )
            )
        elif hop.role == ROLE_UNKNOWN and changed:
            violations.append(
                RoleViolation(
                    hop.hop,
                    VIOLATION_UNATTRIBUTED,
                    "a hop with no declared role changed the contract",
                )
            )
        if index == 0:
            continue
        previous = hops[index - 1]
        if previous.emitted and hop.digest and previous.emitted != hop.digest:
            violations.append(
                RoleViolation(
                    hop.hop,
                    VIOLATION_CHAIN_GAP,
                    "this hop received a contract other than the one the previous hop sent",
                )
            )
    return tuple(violations)


def read_hops(value: object) -> tuple[Hop, ...]:
    """Parse a delegation value in any wire form, dropping entries that name no hop.

    Three shapes reach this function: the legacy list of hop names, a single hop object, and the v2
    envelope ``{"version": 2, "hops": [...]}`` that a carrying hop writes. The envelope is unwrapped
    first so a hop can read a chain it did not author without knowing which form produced it.
    """
    if isinstance(value, Mapping) and not isinstance(value.get("hop"), str):
        inner = value.get("hops")
        if isinstance(inner, Sequence) and not isinstance(inner, str):
            value = inner
    if isinstance(value, str | Mapping):
        hop = Hop.from_mapping(value)
        return (hop,) if hop is not None else ()
    if isinstance(value, Sequence):
        hops = (Hop.from_mapping(entry) for entry in value)
        return tuple(hop for hop in hops if hop is not None)
    return ()


def _believable(hop: Hop) -> str:
    """Return the grade a hop can be believed at, never better than its claim."""
    grade = hop.assurance if hop.assurance in ASSURANCE_ORDER else ASSURANCE_UNVERIFIED
    if grade == ASSURANCE_RECOMPUTED and not hop.digest:
        return ASSURANCE_DECLARED
    return grade


__all__ = [
    "ASSURANCE_DECLARED",
    "ASSURANCE_ECHOED",
    "ASSURANCE_ORDER",
    "ASSURANCE_RECOMPUTED",
    "ASSURANCE_UNVERIFIED",
    "ROLE_CARRIER",
    "ROLE_ORDER",
    "ROLE_REDUCER",
    "ROLE_UNKNOWN",
    "VIOLATION_CARRIER_CHANGED",
    "VIOLATION_CHAIN_GAP",
    "VIOLATION_REDUCER_UNREVISED",
    "VIOLATION_UNATTRIBUTED",
    "AssuranceReport",
    "Hop",
    "RoleViolation",
    "check_roles",
    "read_hops",
    "report",
    "weakest",
]
