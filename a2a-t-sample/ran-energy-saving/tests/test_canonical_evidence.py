"""Evidence must never end a run.

The canonical profile rejects values a peer could not recompute. That is the right answer for a
contract, but these values reach the client through operator text and model output, so a rejection
has to become a missing piece of evidence -- not an exception that kills a run which could still
report what it does and does not know.
"""

from __future__ import annotations

from collections.abc import Mapping

from protocol.client_flow import _plan_digest_for, _plan_for

PLAN: dict[str, object] = {
    "operation": "Create",
    "region": "A2ATestRegion",
    "window": "00:00-06:00",
    "rateMbps": 10.0,
}


def _projection(plan: Mapping[str, object]) -> Mapping[str, object]:
    return dict(plan)


def test_a_canonical_projection_is_kept_and_digested() -> None:
    assert _plan_for(_projection, PLAN) == PLAN
    assert _plan_digest_for(_projection, PLAN) is not None


def test_a_projection_with_no_canonical_spelling_is_dropped() -> None:
    """``1e-05`` has no decimal spelling, so it cannot be part of a contract -- but it can be absent."""
    uncanny = {**PLAN, "rateMbps": 1e-05}

    assert _plan_for(_projection, uncanny) is None
    assert _plan_digest_for(_projection, uncanny) is None


def test_a_projection_that_raises_is_dropped() -> None:
    def broken(_: Mapping[str, object]) -> Mapping[str, object]:
        raise KeyError("missing")

    assert _plan_for(broken, PLAN) is None
