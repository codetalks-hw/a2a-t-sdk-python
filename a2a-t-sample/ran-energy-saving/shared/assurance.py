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


@dataclass(frozen=True)
class Hop:
    """One hop of a delegation chain and the grade it earned.

    Attributes:
        hop: Name of the hop, e.g. ``ui`` or ``nms``.
        digest: Digest the hop published for the contract, empty when it published none.
        assurance: One of the module-level grades.
    """

    hop: str
    digest: str = ""
    assurance: str = ASSURANCE_UNVERIFIED

    def to_dict(self) -> dict[str, object]:
        """Return the v2 object form; the digest key appears only when there is a digest."""
        payload: dict[str, object] = {"hop": self.hop}
        if self.digest:
            payload["digest"] = self.digest
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
        return Hop(
            hop=raw_name.strip(),
            digest=raw_digest if isinstance(raw_digest, str) else "",
            assurance=raw_assurance
            if isinstance(raw_assurance, str) and raw_assurance in ASSURANCE_ORDER
            else ASSURANCE_UNVERIFIED,
        )


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

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this report."""
        return {
            "hops": [hop.to_dict() for hop in self.hops],
            "weakest": self.weakest,
            "changedAt": list(self.changed_at),
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
    return AssuranceReport(hops=tuple(hops), weakest=grade, changed_at=tuple(changed_at))


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
    "AssuranceReport",
    "Hop",
    "read_hops",
    "report",
    "weakest",
]
