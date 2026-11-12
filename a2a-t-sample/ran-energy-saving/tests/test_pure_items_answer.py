"""Tests of the client half of the missing-item envelope (evolution candidate E1)."""

from __future__ import annotations

from demo.pure_items_answer import answer_missing_items
from shared.missing_items import MISSING_ITEMS_METADATA_KEY, SOURCE_OPERATOR, SOURCE_SERVER


def _event(*items: dict[str, object], kind: str = "feasibility") -> dict[str, object]:
    """Build one normalized downstream question carrying an envelope."""
    return {
        "kind": "message",
        "text": "Not feasible, please confirm",
        "metadata": {
            MISSING_ITEMS_METADATA_KEY: {"version": 1, "kind": kind, "items": list(items)}
        },
    }


def _item(
    name: str,
    field: str,
    acceptable: dict[str, object],
    *,
    source: str = SOURCE_OPERATOR,
) -> dict[str, object]:
    """Build one envelope item."""
    return {"item": name, "field": field, "source": source, "acceptable": acceptable}


def test_outage_ceiling_is_answered_with_the_ceiling_itself() -> None:
    """The vendor rejects only rates above the limit, so agreeing to the limit converges."""
    text = answer_missing_items(
        _event(
            _item(
                "Guaranteed throughput target",
                "guaranteedThroughputTarget",
                {"maxMbps": 2.0, "during": "outage"},
            )
        )
    )

    assert text == "Guarantee 2 Mbps during the power outage period."


def test_saving_rate_ceiling_is_answered_as_a_concession() -> None:
    """A target above the cell maximum becomes the maximum, not a repeated request."""
    text = answer_missing_items(
        _event(
            _item(
                "Energy saving rate",
                "energySavingRatePercent",
                {"maxPercent": 5.0},
                source=SOURCE_OPERATOR,
            ),
            kind="target",
        )
    )

    assert text == "Set the energy saving rate to 5%."


def test_every_operator_item_is_answered_in_one_message() -> None:
    """One message must close the whole list; that is the whole point of the envelope."""
    text = answer_missing_items(
        _event(
            _item("Energy saving time", "energySavingTime", {"window": "00:00-06:00"}),
            _item(
                "Guaranteed throughput target",
                "guaranteedThroughputTarget",
                {"maxMbps": 2.0, "during": "outage"},
            ),
        )
    )

    assert text == (
        "Use the energy saving time range 00:00-06:00. "
        "Guarantee 2 Mbps during the power outage period."
    )


def test_a_server_owned_item_stops_the_structured_answer() -> None:
    """A region only the network knows must not be invented by the client."""
    assert (
        answer_missing_items(
            _event(
                _item("Energy saving region", "energySavingRegion", {}, source=SOURCE_SERVER)
            )
        )
        is None
    )


def test_a_mixed_list_falls_back_to_the_prose_policy() -> None:
    """Answering half a list would leave the exchange open; the caller answers the whole thing."""
    assert (
        answer_missing_items(
            _event(
                _item(
                    "Guaranteed throughput target",
                    "guaranteedThroughputTarget",
                    {"maxMbps": 2.0, "during": "outage"},
                ),
                _item("Energy saving region", "energySavingRegion", {}, source=SOURCE_SERVER),
            )
        )
        is None
    )


def test_an_unconstrained_item_falls_back_to_the_prose_policy() -> None:
    """A required-but-unspecified value cannot be invented from the envelope."""
    assert (
        answer_missing_items(
            _event(
                _item(
                    "Time-segmented guaranteed throughput target",
                    "guaranteedThroughputTarget",
                    {"requiredMbps": True},
                    source=SOURCE_OPERATOR,
                ),
                kind="information",
            )
        )
        is None
    )


def test_an_empty_envelope_falls_back_to_the_prose_policy() -> None:
    """A confirm dialogue may ask without naming anything; silence is not an answer."""
    assert answer_missing_items(_event()) is None


def test_a_question_without_an_envelope_is_untouched() -> None:
    """The prose path keeps working exactly as before when no envelope is offered."""
    assert answer_missing_items({"kind": "message", "text": "Not feasible, please confirm"}) is None
