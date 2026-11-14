"""Honest grading: a hop that cannot recompute the contract earns a lower grade, not a fail.

The chain report from :mod:`shared.audit` answers "do these artifacts agree". It cannot answer "how
much of that agreement did any hop actually check", and in a mixed network (A2A-T client, plain-A2A
middle hop, vendor NMS) that is the question a third party asks. These tests pin the vocabulary, the
ordering and the downgrades -- all of which are the point of the module, not incidental helpers.
"""

from __future__ import annotations

import pytest
from shared.assurance import (
    ASSURANCE_DECLARED,
    ASSURANCE_ECHOED,
    ASSURANCE_ORDER,
    ASSURANCE_RECOMPUTED,
    ASSURANCE_UNVERIFIED,
    AssuranceReport,
    Hop,
    read_hops,
    report,
    weakest,
)

DIGEST = "sha256:aaaa"
OTHER = "sha256:bbbb"


@pytest.mark.parametrize("grade", ASSURANCE_ORDER)
def test_every_grade_survives_the_object_form(grade: str) -> None:
    hop = Hop.from_mapping({"hop": "ui", "digest": DIGEST, "assurance": grade})

    assert hop == Hop("ui", DIGEST, grade)


def test_the_order_runs_best_to_worst() -> None:
    assert ASSURANCE_ORDER == (
        ASSURANCE_RECOMPUTED,
        ASSURANCE_ECHOED,
        ASSURANCE_DECLARED,
        ASSURANCE_UNVERIFIED,
    )


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (ASSURANCE_RECOMPUTED, ASSURANCE_ECHOED, ASSURANCE_ECHOED),
        (ASSURANCE_ECHOED, ASSURANCE_DECLARED, ASSURANCE_DECLARED),
        (ASSURANCE_DECLARED, ASSURANCE_UNVERIFIED, ASSURANCE_UNVERIFIED),
        (ASSURANCE_RECOMPUTED, ASSURANCE_DECLARED, ASSURANCE_DECLARED),
        (ASSURANCE_RECOMPUTED, ASSURANCE_RECOMPUTED, ASSURANCE_RECOMPUTED),
    ],
)
def test_the_weakest_hop_decides_the_grade(first: str, second: str, expected: str) -> None:
    hops = (Hop("ui", DIGEST, first), Hop("nms", DIGEST, second))

    assert weakest(hops) == expected


def test_no_hops_means_nothing_was_checked() -> None:
    assert weakest(()) == ASSURANCE_UNVERIFIED


def test_a_legacy_bare_string_can_only_be_declared() -> None:
    """The v1 chain named its hops and carried nothing to recompute: that is ``declared``."""
    assert Hop.from_mapping("ui") == Hop("ui", "", ASSURANCE_DECLARED)


def test_a_v2_entry_carries_the_hop_digest_and_grade() -> None:
    hop = Hop.from_mapping({"hop": "oss", "digest": DIGEST, "assurance": ASSURANCE_ECHOED})

    assert hop == Hop("oss", DIGEST, ASSURANCE_ECHOED)


@pytest.mark.parametrize(
    "raw",
    [None, 42, True, [], {}, {"hop": ""}, {"hop": "   "}, {"hop": 3}, ""],
)
def test_a_malformed_entry_names_no_hop(raw: object) -> None:
    assert Hop.from_mapping(raw) is None


def test_a_hop_name_is_trimmed() -> None:
    assert Hop.from_mapping({"hop": " ui "}) == Hop("ui", "", ASSURANCE_UNVERIFIED)


def test_an_unknown_grade_is_never_upgraded() -> None:
    hop = Hop.from_mapping({"hop": "ui", "digest": DIGEST, "assurance": "trust-me"})

    assert hop == Hop("ui", DIGEST, ASSURANCE_UNVERIFIED)
    assert weakest([hop]) == ASSURANCE_UNVERIFIED


def test_a_non_string_digest_is_read_as_no_digest() -> None:
    hop = Hop.from_mapping({"hop": "ui", "digest": 7, "assurance": ASSURANCE_ECHOED})

    assert hop == Hop("ui", "", ASSURANCE_ECHOED)


def test_a_recomputed_claim_without_a_digest_is_not_believable() -> None:
    hop = Hop("ui", "", ASSURANCE_RECOMPUTED)

    assert weakest([hop]) == ASSURANCE_DECLARED
    assert report([hop]).weakest == ASSURANCE_DECLARED


def test_a_matching_digest_changes_nothing() -> None:
    graded = report([Hop("ui", DIGEST, ASSURANCE_RECOMPUTED)], expected_digest=DIGEST)

    assert graded.changed_at == ()
    assert graded.weakest == ASSURANCE_RECOMPUTED


def test_a_different_digest_marks_the_hop_that_changed_the_contract() -> None:
    graded = report(
        [Hop("ui", DIGEST, ASSURANCE_RECOMPUTED), Hop("nms", OTHER, ASSURANCE_RECOMPUTED)],
        expected_digest=DIGEST,
    )

    assert graded.changed_at == ("nms",)


def test_a_changed_contract_caps_the_chain_at_declared() -> None:
    graded = report(
        [Hop("ui", DIGEST, ASSURANCE_RECOMPUTED), Hop("nms", OTHER, ASSURANCE_RECOMPUTED)],
        expected_digest=DIGEST,
    )

    assert graded.weakest == ASSURANCE_DECLARED


def test_a_changed_contract_does_not_upgrade_a_weaker_grade() -> None:
    graded = report([Hop("nms", OTHER, ASSURANCE_UNVERIFIED)], expected_digest=DIGEST)

    assert graded.weakest == ASSURANCE_UNVERIFIED
    assert graded.changed_at == ("nms",)


def test_without_an_expected_digest_nothing_is_named_as_changed() -> None:
    graded = report([Hop("ui", DIGEST, ASSURANCE_RECOMPUTED)])

    assert graded.changed_at == ()


def test_the_report_keeps_the_delegated_order() -> None:
    graded = report(
        [
            Hop("ui", DIGEST, ASSURANCE_RECOMPUTED),
            Hop("oss", "", ASSURANCE_DECLARED),
            Hop("nms", DIGEST, ASSURANCE_ECHOED),
        ],
        expected_digest=DIGEST,
    )

    assert [hop.hop for hop in graded.hops] == ["ui", "oss", "nms"]


def test_a_hop_round_trips_through_its_dict_form() -> None:
    hop = Hop("nms", DIGEST, ASSURANCE_ECHOED)

    assert Hop.from_mapping(hop.to_dict()) == hop


def test_an_empty_digest_is_omitted_from_the_dict_form() -> None:
    assert Hop("ui", "", ASSURANCE_DECLARED).to_dict() == {
        "hop": "ui",
        "assurance": ASSURANCE_DECLARED,
    }


def test_a_report_round_trips_through_its_dict_form() -> None:
    graded = AssuranceReport(
        hops=(Hop("ui", DIGEST, ASSURANCE_RECOMPUTED),),
        weakest=ASSURANCE_RECOMPUTED,
        changed_at=(),
    )

    document = graded.to_dict()

    assert document == {
        "hops": [{"hop": "ui", "digest": DIGEST, "assurance": ASSURANCE_RECOMPUTED}],
        "weakest": ASSURANCE_RECOMPUTED,
        "changedAt": [],
    }
    assert read_hops(document["hops"]) == graded.hops


def test_read_hops_accepts_both_forms_in_one_value() -> None:
    value: list[object] = [
        "ui",
        {"hop": "oss", "digest": DIGEST, "assurance": ASSURANCE_ECHOED},
        42,
        "",
    ]

    assert read_hops(value) == (
        Hop("ui", "", ASSURANCE_DECLARED),
        Hop("oss", DIGEST, ASSURANCE_ECHOED),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        (42, ()),
        ({}, ()),
        ("ui", (Hop("ui", "", ASSURANCE_DECLARED),)),
        ({"hop": "nms", "digest": DIGEST, "assurance": ASSURANCE_ECHOED}, (Hop("nms", DIGEST, ASSURANCE_ECHOED),)),
    ],
)
def test_read_hops_handles_a_lone_value_or_nothing(value: object, expected: tuple[Hop, ...]) -> None:
    assert read_hops(value) == expected
