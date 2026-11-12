"""Tests of the sample-local missing-item envelope (evolution candidate E1)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from demo.nms_sim.config import SimConfig
from demo.nms_sim.missing_items import build_missing_items, missing_items_metadata
from demo.nms_sim.negotiation import NegotiationIssue
from shared.energy_rules import FEASIBILITY_RATE_MBPS
from shared.missing_items import (
    MISSING_ITEMS_METADATA_KEY,
    SOURCE_OPERATOR,
    SOURCE_SERVER,
    read_missing_items,
)


def _envelope(issue: NegotiationIssue | None, **config: object) -> Mapping[str, object]:
    """Return the envelope metadata of one issue under the given simulator settings."""
    built = missing_items_metadata(issue, SimConfig(**config))  # ty: ignore[invalid-argument-type]
    raw = built[MISSING_ITEMS_METADATA_KEY]
    assert isinstance(raw, Mapping)
    return raw


def test_region_item_is_server_owned_and_carries_no_candidate() -> None:
    """The vendor never names a candidate region, so the envelope must not either."""
    raw = _envelope(NegotiationIssue("information", ("Energy saving region",), "unknown"))

    items = raw["items"]
    assert isinstance(items, list)
    assert items[0]["source"] == SOURCE_SERVER
    assert items[0]["field"] == "energySavingRegion"
    assert items[0]["acceptable"] == {}


def test_outage_item_carries_the_feasibility_ceiling() -> None:
    """A long-outage feasibility proposal must state the rate the cell can actually guarantee."""
    raw = _envelope(
        NegotiationIssue("feasibility", ("Guaranteed throughput target",), "too high"),
        long_outage_feasibility=True,
    )

    items = raw["items"]
    assert isinstance(items, list)
    assert items[0]["source"] == SOURCE_OPERATOR
    assert items[0]["acceptable"] == {"maxMbps": FEASIBILITY_RATE_MBPS, "during": "outage"}


def test_saving_rate_item_carries_the_cell_maximum() -> None:
    """The saving-rate ceiling is a configured fact, so it travels as a value, not as prose."""
    raw = _envelope(NegotiationIssue("target", ("Energy saving rate",), "too high"), max_saving_rate=7.0)

    items = raw["items"]
    assert isinstance(items, list)
    assert items[0]["acceptable"] == {"maxPercent": 7.0}


def test_window_item_names_the_available_window() -> None:
    """The server names the window it can serve; the client can answer with it, so source stays
    ``operator``: answering a value is not the same as owning the rule that produced it."""
    raw = _envelope(NegotiationIssue("information", ("Energy saving time",), ""), window="01:00-05:00")

    items = raw["items"]
    assert isinstance(items, list)
    assert items[0]["acceptable"] == {"window": "01:00-05:00"}


def test_no_issue_puts_nothing_on_the_wire() -> None:
    """A confirm dialogue asks without an issue; an empty envelope would only add noise."""
    envelope = build_missing_items(None, SimConfig())
    assert envelope.kind == "unknown"
    assert envelope.items == ()
    assert missing_items_metadata(None, SimConfig()) == {}


def test_builder_and_reader_round_trip() -> None:
    """The server half and the client half must agree on the wire shape."""
    issue = NegotiationIssue("feasibility", ("Guaranteed throughput target",), "too high")
    built = build_missing_items(issue, SimConfig(long_outage_feasibility=True))
    read = read_missing_items(built.to_metadata())

    assert read is not None
    assert read.kind == "feasibility"
    assert len(read.items) == 1
    assert read.items[0].field == "guaranteedThroughputTarget"


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {MISSING_ITEMS_METADATA_KEY: "not-a-map"},
        {MISSING_ITEMS_METADATA_KEY: {"version": 99, "items": []}},
        {MISSING_ITEMS_METADATA_KEY: {"version": 1, "items": "not-a-list"}},
    ],
)
def test_malformed_envelopes_read_as_absent(metadata: Mapping[str, object] | None) -> None:
    """An unknown shape must degrade to the prose behaviour instead of raising."""
    assert read_missing_items(metadata) is None


def test_items_without_a_field_are_dropped() -> None:
    """One unreadable entry must not poison the whole envelope."""
    read = read_missing_items(
        {
            MISSING_ITEMS_METADATA_KEY: {
                "version": 1,
                "kind": "information",
                "items": [
                    {"item": "Energy saving time"},
                    {"item": "Energy saving time", "field": "energySavingTime"},
                ],
            }
        }
    )

    assert read is not None
    assert [item.field for item in read.items] == ["energySavingTime"]
