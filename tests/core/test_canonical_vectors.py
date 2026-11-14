"""Conformance tests for the ``a2at-canonical/1`` profile, driven by the golden fixture.

The fixture is the standard artifact, not a snapshot of one implementation: the JavaScript harness
under ``a2a-t-sample/ran-energy-saving/tests/js`` asserts the same file. A serialisation change
that only Python agrees with therefore fails conformance instead of silently drifting away from a
JavaScript or Java peer.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from a2a_t.core.plan_binding import (
    CANONICAL_PROFILE,
    CanonicalFormError,
    canonical_digest,
    canonical_json,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "canonical_vectors.json"


def _document() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def _cases(kind: str) -> list[dict[str, object]]:
    cases = _document()[kind]
    assert isinstance(cases, list)
    return cases


def _decode(value: object, decode: object) -> object:
    """Materialise the value a fixture case describes.

    JSON cannot carry ``NaN``, a set, bytes, a ``Decimal`` or an opaque object, so those cases
    carry a ``decode`` tag naming the constructor. The JavaScript harness implements the same
    tags, which keeps both sides honest about *what* was rejected.
    """
    if decode is None:
        return value
    if decode == "nan":
        return float("nan")
    if decode == "inf":
        return float("inf")
    if decode == "-inf":
        return float("-inf")
    if decode == "set":
        assert isinstance(value, list)
        return set(value)
    if decode == "bytes":
        assert isinstance(value, list)
        return bytes(value)
    if decode == "decimal":
        assert isinstance(value, str)
        return Decimal(value)
    if decode == "opaque":
        return object()
    if decode == "non-string-key":
        return {1: value}
    if decode == "nested-nan":
        return {"x": float("nan")}
    if decode == "array-with-set":
        assert isinstance(value, list)
        return [set(value)]
    if decode == "tuple":
        assert isinstance(value, list)
        return tuple(value)
    raise AssertionError(f"unknown fixture decode tag: {decode!r}")


_VALID = _cases("valid")
_INVALID = _cases("invalid")


def test_fixture_declares_the_profile_it_pins() -> None:
    assert _document()["profile"] == CANONICAL_PROFILE


def test_fixture_covers_the_required_breadth() -> None:
    assert len(_VALID) >= 45
    assert len(_INVALID) >= 12


@pytest.mark.parametrize("case", _VALID, ids=[str(case["name"]) for case in _VALID])
def test_valid_vector_reaches_the_pinned_canonical_text_and_digest(
    case: dict[str, object],
) -> None:
    value = _decode(case["value"], case.get("decode"))
    assert canonical_json(value) == case["canonical"]
    assert canonical_digest(value) == case["digest"]


@pytest.mark.parametrize("case", _INVALID, ids=[str(case["name"]) for case in _INVALID])
def test_invalid_vector_is_rejected_with_a_reason(case: dict[str, object]) -> None:
    value = _decode(case["value"], case.get("decode"))
    with pytest.raises(CanonicalFormError) as raised:
        canonical_json(value)
    # The message is what a peer's operator reads when two implementations disagree, so an empty
    # rejection is as useless as no rejection.
    assert str(raised.value).strip()
    assert isinstance(case["reason"], str) and case["reason"]
