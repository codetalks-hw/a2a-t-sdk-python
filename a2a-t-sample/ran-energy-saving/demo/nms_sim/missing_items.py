"""Server half of the machine-readable question envelope (evolution candidate E1).

The simulator already knows exactly what a request is missing; the prose question throws that away.
This module turns the issue into the shared envelope so a client can answer every item at once.

Off unless ``A2AT_SAMPLE_NMS_SIM_MISSING_ITEMS`` is set: the prose-only behaviour is the baseline
the comparison needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shared.missing_items import SOURCE_OPERATOR, SOURCE_SERVER, MissingItem, MissingItems

if TYPE_CHECKING:
    from demo.nms_sim.config import SimConfig
    from demo.nms_sim.negotiation import NegotiationIssue

#: Item names used by :class:`NegotiationIssue` and the field each one asks the caller to edit.
_FIELDS: Final[dict[str, str]] = {
    "Energy saving region": "energySavingRegion",
    "Energy saving time": "energySavingTime",
    "Time-segmented guaranteed throughput target": "guaranteedThroughputTarget",
    "Guaranteed throughput target": "guaranteedThroughputTarget",
    "Energy saving rate": "energySavingRatePercent",
}


def build_missing_items(
    issue: NegotiationIssue | None,
    config: SimConfig,
) -> MissingItems:
    """Build the envelope of one issue.

    Args:
        issue: The issue the simulator would otherwise ask about in prose, or ``None`` when it
            found none (a ``confirm`` dialogue still asks, so the envelope may be empty).
        config: Simulator settings; the acceptable values come from the configured rules.

    Returns:
        The envelope, ready to render into metadata.
    """
    names = issue.items if issue is not None else ()
    return MissingItems(
        kind=issue.kind if issue is not None else "unknown",
        items=tuple(_item(name, config) for name in names),
    )


def missing_items_metadata(
    issue: NegotiationIssue | None,
    config: SimConfig,
) -> dict[str, object]:
    """Return the metadata entry carrying one issue, empty when there is nothing to name.

    An envelope with no items would be pure noise on the wire and would make a no-op case look
    different from the baseline; the capability only speaks when it has something to say.
    """
    envelope = build_missing_items(issue, config)
    return envelope.to_metadata() if envelope.items else {}


def _item(name: str, config: SimConfig) -> MissingItem:
    """Describe one named item: what field it is, what the server accepts, and who can answer."""
    field = _FIELDS.get(name, name)
    source = SOURCE_OPERATOR
    acceptable: dict[str, object] = {}
    if name == "Energy saving region":
        # The vendor never names a candidate region: the client cannot resolve this one itself.
        source = SOURCE_SERVER
    elif name == "Energy saving time":
        acceptable = {"window": config.window}
    elif name == "Guaranteed throughput target":
        # The long-outage rule: a rate above this limit cannot be guaranteed through the outage.
        acceptable = {"maxMbps": config.feasibility_rate_mbps, "during": "outage"}
    elif name == "Time-segmented guaranteed throughput target":
        acceptable = {"requiredMbps": True}
    elif name == "Energy saving rate":
        acceptable = {"maxPercent": config.max_saving_rate}
    return MissingItem(item=name, field=field, source=source, acceptable=acceptable)


__all__ = ["build_missing_items", "missing_items_metadata"]
