"""The canonical plan of this sample.

S6 and S8 both landed on the same discipline: a digest is only binding when the peer can recompute
it from what it accepted. That rules out anything the client cannot see -- most importantly the
site inventory, which belongs to the server.

The field list lives here, in one place, because three writers used to spell it out separately: the
server's plan builder, the client's projection from an accepted Task-T, and the readback
comparison. Drift between them would silently break the binding.
"""

from __future__ import annotations

from typing import Final, TypedDict


class CanonicalPlan(TypedDict):
    """The part of a plan both sides can compute from the accepted request.

    Attributes:
        operation: Business operation, e.g. ``Create`` or ``Modify``.
        region: Area the operation applies to.
        window: One ``HH:MM-HH:MM`` energy-saving window, or ``None`` when the request did not
            state one. Absence is recorded as absence: a value the server filled in on the
            client's behalf is a value the client cannot recompute, and the digest exists to make
            exactly that difference visible.
        rateMbps: Guaranteed throughput of that window in Mbps, or ``None`` for the same reason.
    """

    operation: str
    region: str
    window: str | None
    rateMbps: float | None


#: Fields covered by the plan digest and by the readback comparison.
PLAN_FIELDS: Final[tuple[str, ...]] = ("operation", "region", "window", "rateMbps")


__all__ = ["CanonicalPlan", "PLAN_FIELDS"]
