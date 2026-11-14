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

Canonical profile ``a2at-canonical/1``
--------------------------------------

A value that cannot be recomputed identically by another implementation is not part of the
contract. The profile therefore accepts a deliberately small value set, recursively:

* ``None``, ``bool``.
* ``int`` with ``abs(n) <= 2**53 - 1``. Outside that range a JavaScript peer cannot represent the
  value exactly, so the two sides would be comparing different numbers.
* ``float`` that is finite. An integral float is normalised to ``int`` first (``30.0`` and ``30``
  are the same plan value, and the peer language may not even distinguish them). A non-integral
  float is accepted only when its ``repr`` is plain decimal notation -- no exponent. ``1e-7`` is
  spelled ``1e-07`` by Python, ``1e-7`` by JavaScript and ``1.0E-7`` by Java; accepting it would
  make the digest depend on the writer's language.
* ``str`` with no code point in ``U+D800..U+DFFF``. A lone surrogate is rejected here, not at
  ``encode("utf-8")`` time when the failure would be far from its cause.
* A ``Mapping`` whose keys are all ``str``. Non-string keys are rejected outright: Python's
  ``str()`` coercion silently collides ``{1: "a", "1": "b"}`` into one key, and no JS peer has a
  non-string key to collide with in the first place.
* ``list`` or ``tuple``, both serialised as arrays. Sets, bytes and other sequences are rejected:
  their JSON spelling is either absent or implementation-defined.

Everything else (``Decimal``, ``datetime``, dataclasses, arbitrary objects) raises
:class:`CanonicalFormError`. There is no ``str(value)`` fallback: silent stringification is how
two code paths disagree about the same logical value, and the default ``str`` of an object embeds
its memory address, which differs between processes by construction.

Object keys are sorted by UTF-16 code unit order, the RFC 8785 JCS rule, because that is the order
a JavaScript peer's ``Array.prototype.sort`` produces. Python's code-point order differs for
astral characters (an emoji sorts after ``U+E000`` by code point but before it in UTF-16), so the
two implementations would genuinely diverge on a legal plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: Prefix of every digest this module returns, so a value is self-describing on the wire.
DIGEST_PREFIX: Final[str] = "sha256:"

#: Name of the canonical profile this module implements.
CANONICAL_PROFILE: Final[str] = "a2at-canonical/1"

#: Largest integer an IEEE-754 double and a JavaScript number both represent exactly.
MAX_SAFE_INTEGER: Final[int] = 2**53 - 1

_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"-?[0-9]+\.[0-9]+")
_SURROGATE_START: Final[int] = 0xD800
_SURROGATE_END: Final[int] = 0xDFFF


class CanonicalFormError(ValueError):
    """A value that is not part of the canonical profile, so no peer can recompute it."""


def canonical_json(value: object) -> str:
    """Return the canonical JSON text of ``value`` under :data:`CANONICAL_PROFILE`.

    Raises:
        CanonicalFormError: If ``value`` (at any depth) is not a canonical value.
    """
    return json.dumps(
        _canonical(value),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
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
    """Return ``value`` normalised to the accepted value set, or raise."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _canonical_int(value)
    if isinstance(value, float):
        return _canonical_float(value)
    if isinstance(value, str):
        _reject_lone_surrogates(value)
        return value
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise CanonicalFormError(
        f"{type(value).__name__} is not a canonical value; convert it at the caller before binding"
    )


def _canonical_int(value: int) -> int:
    if abs(value) > MAX_SAFE_INTEGER:
        raise CanonicalFormError(
            f"integer {value} is outside the exactly representable range "
            f"+/-{MAX_SAFE_INTEGER}"
        )
    return value


def _canonical_float(value: float) -> object:
    if not math.isfinite(value):
        raise CanonicalFormError(f"non-finite float is not canonical: {value!r}")
    if value.is_integer():
        # ``10`` and ``10.0`` are the same plan value; JSON spells them differently, and a digest
        # that depends on the spelling would fail exactly when two implementations use different
        # numeric types.
        return _canonical_int(int(value))
    spelling = repr(value)
    if _DECIMAL_RE.fullmatch(spelling) is None:
        raise CanonicalFormError(
            f"float {spelling} needs exponent notation and cannot be recomputed across languages"
        )
    return value


def _reject_lone_surrogates(value: str) -> None:
    for index, character in enumerate(value):
        code = ord(character)
        if _SURROGATE_START <= code <= _SURROGATE_END:
            raise CanonicalFormError(
                f"string contains a lone surrogate U+{code:04X} at index {index}"
            )


def _canonical_mapping(value: Mapping[object, object]) -> dict[str, object]:
    keys: list[str] = []
    for key in value:
        if not isinstance(key, str):
            raise CanonicalFormError(
                f"mapping key {key!r} is {type(key).__name__}; canonical mappings require str keys"
            )
        _reject_lone_surrogates(key)
        keys.append(key)
    # UTF-16 code unit order is what makes a JavaScript peer's sort agree with ours.
    keys.sort(key=lambda key: key.encode("utf-16-be"))
    return {key: _canonical(value[key]) for key in keys}


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
    """Return whether two plan values are structurally the same value.

    ``bool`` is never a number, numbers compare exactly rather than by float rounding, and
    mappings and sequences compare recursively so a nested ``{"x": 1}`` cannot claim equality
    with ``{"x": True}``.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, float) and isinstance(right, float):
        return left == right
    if isinstance(left, int) and isinstance(right, float):
        return _int_equals_float(left, right)
    if isinstance(left, float) and isinstance(right, int):
        return _int_equals_float(right, left)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return _same_mapping(left, right)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            same_plan_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if type(left) is not type(right):
        return False
    return bool(left == right)


def _int_equals_float(integer: int, number: float) -> bool:
    """Compare an int with a float exactly, at any magnitude.

    Python already compares ``int`` against ``float`` without rounding the int, so this needs no
    safe-range guard: values outside the canonical profile still deserve a truthful deviation
    answer, and refusing to compare them would make the digest and the comparison disagree.
    """
    if not math.isfinite(number):
        return False
    return integer == number


def _same_mapping(left: Mapping[object, object], right: Mapping[object, object]) -> bool:
    if len(left) != len(right):
        return False
    for key, left_value in left.items():
        if key not in right:
            return False
        if not same_plan_value(left_value, right[key]):
            return False
    return True


__all__ = [
    "CANONICAL_PROFILE",
    "DIGEST_PREFIX",
    "MAX_SAFE_INTEGER",
    "CanonicalFormError",
    "PlanDeviation",
    "canonical_digest",
    "canonical_json",
    "plan_deviations",
    "plan_digest",
    "same_plan_value",
]
