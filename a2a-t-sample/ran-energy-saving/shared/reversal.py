"""The plan that undoes an accepted plan (reversal contract).

Every other envelope in this sample says what will happen: the agreement names the revision, the
approval binds the executor to it, the readback proves what ran. None of them says what the network
must go back to if the plan is abandoned. Without that, an abort leaves the network in a
configuration no message can reconstruct, and the protocol has no way to show otherwise.

The reversal is not an instruction to abort. It is part of the accepted agreement: the state that
existed before this revision, captured at the moment the revision was accepted, so a later abort
has an object to execute instead of a guess. A plain-A2A hop cannot express it at all -- it has no
accepted plan, so it has no "before" to restore and no place to record what restore meant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Sample-local metadata key. Prototype key: a registered extension URI replaces it.
REVERSAL_METADATA_KEY: Final[str] = "a2at-sample/reversal"

#: Envelope version.
REVERSAL_VERSION: Final[int] = 1

#: The only operation a reversal performs: put the previous plan back.
OPERATION_RESTORE: Final[str] = "Restore"


@dataclass(frozen=True)
class Reversal:
    """The plan a client may execute to undo one accepted plan.

    Attributes:
        plan: Canonical plan (operation/region/window/rateMbps) the network must return to.
        reason: Optional explanation, kept only when one was stated.
    """

    plan: Mapping[str, object]
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this reversal."""
        payload: dict[str, object] = {
            "version": REVERSAL_VERSION,
            "plan": dict(self.plan),
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this reversal."""
        return {REVERSAL_METADATA_KEY: self.to_dict()}


def read_reversal(metadata: Mapping[str, object] | None) -> Reversal | None:
    """Read a reversal from one message, ``None`` when absent or malformed.

    Nothing about a malformed envelope may raise: reading it must not be able to end a Task or a
    run. A reversal that cannot be read is simply a reversal this hop does not hold.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(REVERSAL_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        return None
    plan = raw.get("plan")
    if version != REVERSAL_VERSION or not isinstance(plan, Mapping):
        return None
    reason = raw.get("reason")
    return Reversal(plan=dict(plan), reason=reason if isinstance(reason, str) else "")


def restoration_for(
    plan: Mapping[str, object],
    previous: Mapping[str, object] | None,
    *,
    baseline_rate: float,
) -> Reversal:
    """Return the plan that undoes ``plan``.

    With a previous plan the reversal is that plan re-stated as a ``Restore``. Without one the
    window returns to full capacity: there was nothing before this revision, so the only thing to
    restore is the baseline the cell starts from.

    Missing keys never raise: they fall back to the new plan's region/window and to the baseline
    rate, because a reversal that cannot be built is worse than one built from the values at hand.
    """
    source: Mapping[str, object] = previous if previous is not None else plan
    # Without a previous plan there is no rate to re-state: the window returns to full capacity.
    # Region and window still come from the new plan, because that is the area the abort undoes.
    previous_rate = previous.get("rateMbps") if previous is not None else None
    return Reversal(
        plan={
            "operation": OPERATION_RESTORE,
            "region": _text(source.get("region"), plan.get("region")),
            "window": _text(source.get("window"), plan.get("window")),
            "rateMbps": _number(previous_rate, baseline_rate),
        }
    )


def _text(value: object, fallback: object) -> str:
    """Return one textual field, falling back without raising."""
    if isinstance(value, str) and value:
        return value
    return fallback if isinstance(fallback, str) else ""


def _number(value: object, fallback: float) -> float:
    """Return one numeric field, falling back without raising."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float(fallback)


__all__ = [
    "OPERATION_RESTORE",
    "REVERSAL_METADATA_KEY",
    "REVERSAL_VERSION",
    "Reversal",
    "read_reversal",
    "restoration_for",
]
