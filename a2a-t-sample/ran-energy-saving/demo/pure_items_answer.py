"""Answer a plain-A2A question that carries a machine-readable missing-item list.

Plain A2A defines no negotiation vocabulary: a vendor offering a structured question decides its
own schema, and the client is written against that vendor. This module is the client half of that
invention (evolution candidate E1): it reads the list and answers every item the caller can ground
in **one** message, so the exchange converges in one round instead of a prose ping-pong.

It never answers an item the network side owns, and it applies the same acceptance limits the prose
plan applies -- it must not concede below the client's own floor, and it must not offer more than
the operator asked for. A refused item falls back to the ordinary answer policy, exactly as a
refused deterministic plan does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from protocol.client_policy import ClientPolicy
from shared.missing_items import SOURCE_OPERATOR, MissingItem, MissingItems, read_missing_items

from demo.pure_concession import rate_floor, stated_percent, stated_rate

#: Value the client concedes to when the server names an acceptable ceiling for an outage rate.
#: The vendor's rule is strict (``rate > limit`` is infeasible), so the ceiling itself is feasible.
OUTAGE_RATE_TEMPLATE: Final[str] = "Guarantee {value:g} Mbps during the power outage period."
#: The ceiling of the cell's saving rate.
SAVING_RATE_TEMPLATE: Final[str] = "Set the energy saving rate to {value:g}%."
#: A window the server named.
WINDOW_TEMPLATE: Final[str] = "Use the energy saving time range {value}."


def answer_missing_items(
    event: Mapping[str, object],
    *,
    request_text: str = "",
    policy: ClientPolicy | None = None,
) -> str | None:
    """Return one free-text answer covering every operator-owned item, or ``None``.

    ``None`` means "this event is not answerable here": no envelope, a malformed one, an item only
    the network side can supply, or a value the client's own limits forbid. The caller then falls
    back to its ordinary answer policy.

    Args:
        event: One normalized downstream question.
        request_text: The operator's request, used to avoid offering more than it asked for.
        policy: The client's acceptance limits, used to avoid conceding below its floor.
    """
    metadata = event.get("metadata")
    envelope = read_missing_items(metadata if isinstance(metadata, Mapping) else None)
    if envelope is None:
        return None
    resolved = policy if policy is not None else ClientPolicy()
    return _render(envelope, request_text=request_text, policy=resolved)


def _render(envelope: MissingItems, *, request_text: str, policy: ClientPolicy) -> str | None:
    """Render one answer, ``None`` when any item is not the caller's to answer."""
    sentences: list[str] = []
    for item in envelope.items:
        sentence = _sentence(item, request_text=request_text, policy=policy)
        if sentence is None:
            return None
        sentences.append(sentence)
    if not sentences:
        # An empty list says nothing about what to change; the prose path is still the better bet.
        return None
    return " ".join(sentences)


def _sentence(item: MissingItem, *, request_text: str, policy: ClientPolicy) -> str | None:
    """Render the concession one item asks for, ``None`` when the caller cannot or must not."""
    if item.source != SOURCE_OPERATOR:
        return None
    max_mbps = _number(item.acceptable.get("maxMbps"))
    if max_mbps is not None:
        return _rate_sentence(max_mbps, request_text=request_text, policy=policy)
    max_percent = _number(item.acceptable.get("maxPercent"))
    if max_percent is not None:
        return _percent_sentence(max_percent, request_text=request_text)
    window = item.acceptable.get("window")
    if isinstance(window, str) and window.strip():
        return WINDOW_TEMPLATE.format(value=window.strip())
    return None


def _rate_sentence(max_mbps: float, *, request_text: str, policy: ClientPolicy) -> str | None:
    """Answer a rate ceiling without going under the client floor or over the operator's ask."""
    floor = rate_floor(policy)
    if floor is not None and max_mbps < floor:
        # The client is not allowed to guarantee this little; the operator (or the agent) decides.
        return None
    asked = stated_rate(request_text)
    value = max_mbps if asked is None else min(max_mbps, asked)
    return OUTAGE_RATE_TEMPLATE.format(value=value)


def _percent_sentence(max_percent: float, *, request_text: str) -> str:
    """Answer a saving-rate ceiling without asking for more than the operator did."""
    asked = stated_percent(request_text)
    value = max_percent if asked is None else min(max_percent, asked)
    return SAVING_RATE_TEMPLATE.format(value=value)


def _number(value: object) -> float | None:
    """Return one numeric envelope value, ``None`` when it is absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = ["answer_missing_items"]
