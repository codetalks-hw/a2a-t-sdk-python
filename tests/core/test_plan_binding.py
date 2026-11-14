"""Tests for the canonical form and digests that make an agreement recomputable.

The property under test is the one S6/S8 depend on: two peers holding the same plan and the same
field list must reach the same digest, and anything outside that list must not affect it. A digest
that leaks a server-only field turns a binding back into a claim.
"""

from __future__ import annotations

import pytest

from a2a_t.core.plan_binding import (
    DIGEST_PREFIX,
    PlanDeviation,
    canonical_digest,
    canonical_json,
    plan_deviations,
    plan_digest,
    same_plan_value,
)

FIELDS = ("operation", "region", "window", "rateMbps")

PLAN: dict[str, object] = {
    "operation": "Create",
    "region": "A2ATestRegion",
    "window": "00:00-06:00",
    "rateMbps": 10.0,
}


# ---------------------------------------------------------------- canonical form


def test_key_order_and_numeric_spelling_do_not_change_the_digest() -> None:
    same_plan = {
        "rateMbps": 10,
        "window": "00:00-06:00",
        "region": "A2ATestRegion",
        "operation": "Create",
    }
    assert plan_digest(PLAN, FIELDS) == plan_digest(same_plan, FIELDS)


def test_the_digest_is_namespaced_and_stable() -> None:
    digest = plan_digest(PLAN, FIELDS)
    assert digest.startswith(DIGEST_PREFIX)
    assert digest == plan_digest(PLAN, FIELDS)
    assert len(digest) == len(DIGEST_PREFIX) + 64


def test_canonical_json_sorts_nested_mappings_and_keeps_null() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": None, "c": "x"}]}) == (
        '{"a":[2,{"c":"x","d":null}],"b":1}'
    )


def test_a_non_json_leaf_is_stringified_so_two_writers_agree() -> None:
    class Marker:
        def __str__(self) -> str:
            return "marker"

    assert canonical_json({"field": Marker()}) == '{"field":"marker"}'


@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("operation", "Modify"),
        ("region", "OtherRegion"),
        ("window", "08:00-10:00"),
        ("rateMbps", 2.0),
    ],
)
def test_changing_a_contracted_field_changes_the_digest(field: str, other: object) -> None:
    assert plan_digest({**PLAN, field: other}, FIELDS) != plan_digest(PLAN, FIELDS)


def test_fields_outside_the_contract_do_not_change_the_digest() -> None:
    noisy = {**PLAN, "sites": ["site-a", "site-b"], "estimatedSaving": 12.5}
    assert plan_digest(noisy, FIELDS) == plan_digest(PLAN, FIELDS)


def test_a_field_missing_on_one_side_projects_to_none_on_both() -> None:
    partial: dict[str, object] = {"operation": "Create", "region": "A2ATestRegion"}
    explicit = {**partial, "window": None, "rateMbps": None}
    assert plan_digest(partial, FIELDS) == plan_digest(explicit, FIELDS)


# ---------------------------------------------------------------- deviations


def test_only_contracted_fields_that_differ_are_reported() -> None:
    executed = {**PLAN, "rateMbps": 8.0, "sites": ["site-a"]}
    assert [entry.to_dict() for entry in plan_deviations(PLAN, executed, FIELDS)] == [
        {"field": "rateMbps", "planned": 10.0, "actual": 8.0}
    ]


def test_matching_plans_report_no_deviation_even_when_extra_values_differ() -> None:
    executed = {**PLAN, "sites": ["site-a"]}
    assert plan_deviations(PLAN, executed, FIELDS) == ()


def test_numbers_compare_numerically_but_booleans_stay_booleans() -> None:
    assert same_plan_value(10, 10.0) is True
    assert same_plan_value(True, 1) is False
    assert same_plan_value(None, None) is True
    assert same_plan_value("10", 10) is False


def test_a_deviation_keeps_the_value_each_side_reported() -> None:
    deviation = PlanDeviation(field="rateMbps", planned=10.0, actual=None)
    assert deviation.to_dict() == {"field": "rateMbps", "planned": 10.0, "actual": None}


# ---------------------------------------------------------------- generic digest


def test_canonical_digest_is_suitable_for_a_document_that_is_not_a_plan() -> None:
    document = {"operationType": "Create", "region": "A2ATestRegion"}
    reordered = {"region": "A2ATestRegion", "operationType": "Create"}
    assert canonical_digest(document) == canonical_digest(reordered)
    assert canonical_digest(document) != canonical_digest({**document, "region": "Other"})
