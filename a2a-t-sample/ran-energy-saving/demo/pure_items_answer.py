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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EnvelopeAnswer:
    """One resolved envelope: the sentence to send and the rate it agrees to.

    Attributes:
        text: The free-text answer, the only thing a plain-A2A hop can send.
        rate_mbps: The guaranteed rate the answer agrees to, when the envelope named a ceiling.
    """

    text: str
    rate_mbps: float | None = None


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
    return answer_from_envelope(envelope, request_text=request_text, policy=policy)


def answer_from_envelope(
    envelope: MissingItems,
    *,
    request_text: str = "",
    policy: ClientPolicy | None = None,
) -> str | None:
    """Render one answer from an already-parsed envelope, ``None`` when it is not answerable.

    Shared by both hops: the plain client reads the envelope off a message event, an A2A-T client
    reads it off the proposal view. Keeping one renderer is what makes the two comparable.
    """
    resolved = resolve_envelope(envelope, request_text=request_text, policy=policy)
    return resolved.text if resolved is not None else None


def resolve_envelope(
    envelope: MissingItems,
    *,
    request_text: str = "",
    policy: ClientPolicy | None = None,
) -> EnvelopeAnswer | None:
    """Resolve one envelope into both the sentence and the values it agrees to.

    A plain-A2A answer is only the sentence. An A2A-T accept also names the rate it agrees to, so
    the flow can apply that rate to every baseline window the event covers instead of leaving the
    vendor the rejected plan -- which is why the value travels next to the text instead of being
    re-parsed out of it.
    """
    resolved = policy if policy is not None else ClientPolicy()
    sentences: list[str] = []
    rate_mbps: float | None = None
    for item in envelope.items:
        rendered = _item_answer(item, request_text=request_text, policy=resolved)
        if rendered is None:
            return None
        sentence, value = rendered
        sentences.append(sentence)
        if value is not None and rate_mbps is None:
            rate_mbps = value
    if not sentences:
        # An empty list says nothing about what to change; the prose path is still the better bet.
        return None
    return EnvelopeAnswer(text=" ".join(sentences), rate_mbps=rate_mbps)


def _item_answer(
    item: MissingItem, *, request_text: str, policy: ClientPolicy
) -> tuple[str, float | None] | None:
    """Render one item, ``None`` when the caller cannot or must not answer it."""
    if item.source != SOURCE_OPERATOR:
        return None
    max_mbps = _number(item.acceptable.get("maxMbps"))
    if max_mbps is not None:
        value = _rate_value(max_mbps, request_text=request_text, policy=policy)
        if value is None:
            return None
        return OUTAGE_RATE_TEMPLATE.format(value=value), value
    max_percent = _number(item.acceptable.get("maxPercent"))
    if max_percent is not None:
        percent = _percent_value(max_percent, request_text=request_text)
        return SAVING_RATE_TEMPLATE.format(value=percent), None
    window = item.acceptable.get("window")
    if isinstance(window, str) and window.strip():
        return WINDOW_TEMPLATE.format(value=window.strip()), None
    return None


def _rate_value(max_mbps: float, *, request_text: str, policy: ClientPolicy) -> float | None:
    """Answer a rate ceiling without going under the client floor or over the operator's ask."""
    floor = rate_floor(policy)
    if floor is not None and max_mbps < floor:
        # The client is not allowed to guarantee this little; the operator (or the agent) decides.
        return None
    asked = stated_rate(request_text)
    return max_mbps if asked is None else min(max_mbps, asked)


def _percent_value(max_percent: float, *, request_text: str) -> float:
    """Answer a saving-rate ceiling without asking for more than the operator did."""
    asked = stated_percent(request_text)
    return max_percent if asked is None else min(max_percent, asked)


def _number(value: object) -> float | None:
    """Return one numeric envelope value, ``None`` when it is absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "EnvelopeAnswer",
    "answer_from_envelope",
    "answer_missing_items",
    "resolve_envelope",
]
