"""Canonical form and digests for agreements that both sides must be able to recompute.

An approval or an execution readback is only *binding* when both peers reach the same digest from
the same plan. That needs exactly two things: one canonical serialization, and one agreed field
set. Nothing else. A digest that includes a value only one side can see is a claim, not a binding --
the other side can only echo it.

The SDK deliberately knows no business field. Callers pass the field tuple; whatever the plan
carries outside it is ignored by the digest and by the comparison, so server-side inventory
(sites, cell ids, cost estimates) cannot silently become part of the contract.

This module is mechanism only. It does not define an approval envelope, a readback envelope, or
the ``matched``/``deviated`` adjectives a particular application reports: those are application
policy and stay with the application.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: Prefix of every digest this module returns, so a value is self-describing on the wire.
DIGEST_PREFIX: Final[str] = "sha256:"


def canonical_json(value: object) -> str:
    """Return the canonical JSON text of ``value``.

    Mapping keys are sorted, no insignificant whitespace is emitted, and non-JSON leaves are
    rendered by ``str`` so a datetime or an enum-like object does not change the digest between
    two writers that meant the same instant.
    """
    return json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_digest(value: object) -> str:
    """Return ``DIGEST_PREFIX`` plus the SHA-256 of :func:`canonical_json`."""
    return DIGEST_PREFIX + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plan_digest(plan: Mapping[str, object], fields: Sequence[str]) -> str:
    """Return the digest of one plan, projected onto ``fields``.

    Projection is what makes the digest recomputable: a peer that holds the same plan and the same
    field list gets the same value, and a peer that only hears about extra server-side details is
    not forced to reproduce them.
    """
    return canonical_digest({field: plan.get(field) for field in fields})


def _canonical(value: object) -> object:
    """Return ``value`` with mappings sorted and non-JSON leaves stringified."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        # ``10`` and ``10.0`` are the same plan value; JSON spells them differently, and a digest
        # that depends on the spelling would fail exactly when two implementations use different
        # numeric types.
        return int(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class PlanDeviation:
    """One field whose executed value differs from the planned one.

    Attributes:
        field: Field name from the caller's field tuple.
        planned: Value in the plan that was agreed.
        actual: Value in the plan that was reported or executed.
    """

    field: str
    planned: object
    actual: object

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this deviation."""
        return {"field": self.field, "planned": self.planned, "actual": self.actual}


def plan_deviations(
    planned: Mapping[str, object],
    executed: Mapping[str, object],
    fields: Sequence[str],
) -> tuple[PlanDeviation, ...]:
    """Return one entry per field whose executed value differs from the planned one.

    Comparison is exact on purpose: a readback that "approximately" matches is not evidence that
    the plan was executed. Numeric fields tolerate the ``10`` versus ``10.0`` spelling, and ``True``
    is never equal to ``1``.
    """
    deviations: list[PlanDeviation] = []
    for field in fields:
        planned_value = planned.get(field)
        executed_value = executed.get(field)
        if same_plan_value(planned_value, executed_value):
            continue
        deviations.append(
            PlanDeviation(field=field, planned=planned_value, actual=executed_value)
        )
    return tuple(deviations)


def same_plan_value(left: object, right: object) -> bool:
    """Return whether two plan values are the same value, numbers compared numerically."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


__all__ = [
    "DIGEST_PREFIX",
    "PlanDeviation",
    "canonical_digest",
    "canonical_json",
    "plan_deviations",
    "plan_digest",
    "same_plan_value",
]
