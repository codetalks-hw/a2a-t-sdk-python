"""The sample-local execution readback (S8).

Both protocols return a report artifact, but the artifact is vendor-shaped: it says what the
network did without saying *what it was supposed to do*. A client can display it and nothing more.

This envelope puts the executed plan next to the digest of the plan that was approved, and lists
the deviations field by field. An A2A-T client can recompute that digest from the Task-T it
accepted and therefore tell "executed as approved" from "executed something else". A plain-A2A
client has no accepted document, so it can only show the claim as unverified.

Like the other sample envelopes this one is deliberately local: the point of S8 is not that A2A
lacks a metadata map, it is that a readback is only comparable when the agreement it refers to is
an object both sides hold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Sample-local key of the envelope, on the report artifact's metadata.
READBACK_METADATA_KEY: Final[str] = "a2at-sample/readback"

#: Envelope schema version.
READBACK_VERSION: Final[int] = 1

#: Fields compared between the approved plan and the executed one.
COMPARED_FIELDS: Final[tuple[str, ...]] = ("operation", "region", "window", "rateMbps")

#: Verification outcomes a client can report.
STATUS_MATCHED: Final[str] = "matched"
STATUS_DEVIATED: Final[str] = "deviated"
STATUS_UNVERIFIED: Final[str] = "unverified"


@dataclass(frozen=True)
class Readback:
    """One execution readback.

    Attributes:
        plan_digest: Digest of the plan that was approved and executed.
        executed: The plan the network reports having applied.
        deviations: Field-level differences between the approved and the executed plan.
    """

    plan_digest: str
    executed: Mapping[str, object]
    deviations: tuple[Mapping[str, object], ...]

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this envelope."""
        return {
            READBACK_METADATA_KEY: {
                "version": READBACK_VERSION,
                "kind": "readback",
                "planDigest": self.plan_digest,
                "executed": dict(self.executed),
                "deviations": [dict(entry) for entry in self.deviations],
            }
        }


def compare_plans(
    planned: Mapping[str, object], executed: Mapping[str, object]
) -> tuple[dict[str, object], ...]:
    """Return one entry per field whose executed value differs from the approved one.

    Comparison is exact on purpose: a readback that "approximately" matches is not evidence that
    the plan was executed. Numeric fields tolerate the ``10`` vs ``10.0`` spelling.
    """
    deviations: list[dict[str, object]] = []
    for field in COMPARED_FIELDS:
        planned_value = planned.get(field)
        executed_value = executed.get(field)
        if _same(planned_value, executed_value):
            continue
        deviations.append(
            {"field": field, "planned": planned_value, "actual": executed_value}
        )
    return tuple(deviations)


def _same(left: object, right: object) -> bool:
    """Return whether two plan values are the same value, numbers compared numerically."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def read_readback(metadata: Mapping[str, object] | None) -> Readback | None:
    """Read the envelope of one artifact, ``None`` when absent or malformed."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(READBACK_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version != READBACK_VERSION or raw.get("kind") != "readback":
        return None
    digest = raw.get("planDigest")
    executed = raw.get("executed")
    if not isinstance(digest, str) or not digest or not isinstance(executed, Mapping):
        return None
    deviations = raw.get("deviations")
    return Readback(
        plan_digest=digest,
        executed=dict(executed),
        deviations=tuple(
            dict(entry) for entry in deviations if isinstance(entry, Mapping)
        )
        if isinstance(deviations, list)
        else (),
    )


def verify(readback: Readback, accepted_digest: str | None) -> str:
    """Return whether a readback belongs to the plan the client accepted.

    ``accepted_digest=None`` means the client holds no accepted plan object; the readback is then
    reported as unverified rather than as a failure, because that is exactly what a plain-A2A hop
    can do.
    """
    if accepted_digest is None:
        return STATUS_UNVERIFIED
    if readback.plan_digest != accepted_digest:
        return STATUS_DEVIATED
    # The right plan can still be executed differently (a rate applied lower, a window shifted);
    # that is a deviation even though the identity matches.
    return STATUS_DEVIATED if readback.deviations else STATUS_MATCHED


__all__ = [
    "COMPARED_FIELDS",
    "READBACK_METADATA_KEY",
    "READBACK_VERSION",
    "STATUS_DEVIATED",
    "STATUS_MATCHED",
    "STATUS_UNVERIFIED",
    "Readback",
    "compare_plans",
    "read_readback",
    "verify",
]
