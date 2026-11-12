"""The sample-local machine-readable question envelope (evolution candidate E1).

A2A-T has a typed place to say what a request is missing: the Negotiation-T proposal carries a
kind, a body and the negotiation context. Plain A2A has containers but no vocabulary -- a plain
``Message`` can already hold a ``Part.data`` blob or per-part metadata (the SDK the sample pins
supports both), so "plain A2A cannot carry structure" would be wrong. What it cannot carry is
*negotiation semantics*: no kind, no round, no performative, no terminal rule. A vendor that wants
a structured question therefore invents its own schema and every client is written against that
vendor.

This module holds the envelope both sides of the sample agree on. The key is deliberately
**sample-local and unregistered**, which is the point: adopting the capability on a plain-A2A hop
means inventing a shape, while adopting it on an A2A-T hop means extending a proposal that already
exists. That asymmetry is what the comparison is about, so it must stay visible in the code.

Note for readers of the sample: this client normalizes a message event from ``parts[0].text`` and
``message.metadata`` only (``shared/sse_event_consumer.py``), so a ``Part.data`` payload would not
reach it without widening that normalizer. That is a sample-side limit, not a protocol one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Sample-local metadata key of the envelope. Not a registered A2A-T extension URI.
MISSING_ITEMS_METADATA_KEY: Final[str] = "a2at-sample/missing-items"

#: Envelope schema version, so a reader can refuse a shape it does not know.
MISSING_ITEMS_VERSION: Final[int] = 1

#: Who is expected to produce the value in the client's answer. ``operator``: the caller can answer
#: it, possibly by echoing a ceiling the server itself just named -- answering is not the same as
#: owning the rule. ``server``: the caller cannot answer it, because the value is network inventory
#: (a region, a site, a RAT) that the client must not invent.
SOURCE_OPERATOR: Final[str] = "operator"
SOURCE_SERVER: Final[str] = "server"


@dataclass(frozen=True)
class MissingItem:
    """One item a request is missing.

    Attributes:
        item: Human-readable name, as the prose question would print it.
        field: Machine-readable field name the caller should fill in.
        source: Who is expected to produce the value in the answer (``operator`` or ``server``).
        acceptable: What the server accepts; shape depends on the field, empty when unconstrained.
    """

    item: str
    field: str
    source: str
    acceptable: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this item."""
        payload: dict[str, object] = {
            "item": self.item,
            "field": self.field,
            "source": self.source,
            "acceptable": dict(self.acceptable),
        }
        return payload


@dataclass(frozen=True)
class MissingItems:
    """The whole envelope of one question.

    Attributes:
        kind: Negotiation kind the server reported (``information``/``target``/``feasibility``),
            or ``unknown`` when the server asked without naming one.
        items: The open items, in the order the server listed them.
    """

    kind: str
    items: tuple[MissingItem, ...]

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this envelope."""
        return {
            MISSING_ITEMS_METADATA_KEY: {
                "version": MISSING_ITEMS_VERSION,
                "kind": self.kind,
                "items": [item.to_dict() for item in self.items],
            }
        }


def read_missing_items(metadata: Mapping[str, object] | None) -> MissingItems | None:
    """Read the envelope of one message, ``None`` when absent or malformed.

    A malformed envelope is treated as absent rather than raising: an unknown shape must degrade to
    the prose behaviour, not break the exchange.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(MISSING_ITEMS_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version != MISSING_ITEMS_VERSION:
        return None
    kind = raw.get("kind")
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return None
    items: list[MissingItem] = []
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("item")
        field = entry.get("field")
        source = entry.get("source")
        acceptable = entry.get("acceptable")
        if not isinstance(name, str) or not isinstance(field, str):
            continue
        items.append(
            MissingItem(
                item=name,
                field=field,
                source=source if isinstance(source, str) else SOURCE_OPERATOR,
                acceptable=acceptable if isinstance(acceptable, Mapping) else {},
            )
        )
    return MissingItems(kind=kind if isinstance(kind, str) else "unknown", items=tuple(items))


__all__ = [
    "MISSING_ITEMS_METADATA_KEY",
    "MISSING_ITEMS_VERSION",
    "SOURCE_OPERATOR",
    "SOURCE_SERVER",
    "MissingItem",
    "MissingItems",
    "read_missing_items",
]
