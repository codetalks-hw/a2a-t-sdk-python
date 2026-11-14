"""Tests for the canonical form and digests that make an agreement recomputable.

The property under test is the one S6/S8 depend on: two peers holding the same plan and the same
field list must reach the same digest, and anything outside that list must not affect it. A digest
that leaks a server-only field turns a binding back into a claim.
"""

from __future__ import annotations

import pytest

from a2a_t.core.plan_binding import (
    CANONICAL_PROFILE,
    DIGEST_PREFIX,
    CanonicalFormError,
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


def test_a_non_json_leaf_is_rejected_instead_of_stringified() -> None:
    class Marker:
        def __str__(self) -> str:
            return "marker"

    with pytest.raises(CanonicalFormError):
        canonical_json({"field": Marker()})


def test_the_profile_is_named_so_a_peer_can_negotiate_it() -> None:
    assert CANONICAL_PROFILE == "a2at-canonical/1"


def test_safe_integer_boundaries_are_canonical_and_one_past_is_not() -> None:
    assert canonical_json(2**53 - 1) == str(2**53 - 1)
    assert canonical_json(-(2**53 - 1)) == str(-(2**53 - 1))
    with pytest.raises(CanonicalFormError):
        canonical_json(2**53)
    with pytest.raises(CanonicalFormError):
        canonical_json(-(2**53))


def test_integral_floats_normalise_and_negative_zero_becomes_zero() -> None:
    assert canonical_json(30.0) == "30"
    assert canonical_json(-0.0) == "0"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e-7, 1e300, 1e21])
def test_non_finite_and_exponent_floats_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalFormError):
        canonical_json(value)


def test_non_string_mapping_keys_are_rejected_instead_of_colliding() -> None:
    with pytest.raises(CanonicalFormError):
        canonical_json({1: "a", "1": "b"})


def test_a_lone_surrogate_is_rejected_in_a_value_and_in_a_key() -> None:
    with pytest.raises(CanonicalFormError):
        canonical_json("\ud800")
    with pytest.raises(CanonicalFormError):
        canonical_json({"\ud800": 1})


def test_keys_sort_by_utf16_code_unit_not_by_code_point() -> None:
    # U+1F600 (D83D DE00 in UTF-16) precedes U+E000 by code unit, though it follows it by code
    # point. This is the order a JavaScript peer's sort produces.
    private_use = chr(0xE000)
    emoji = chr(0x1F600)
    assert canonical_json({private_use: 1, emoji: 2}) == (
        '{"' + emoji + '":2,"' + private_use + '":1}'
    )


def test_sets_and_bytes_are_rejected() -> None:
    with pytest.raises(CanonicalFormError):
        canonical_json({1, 2})
    with pytest.raises(CanonicalFormError):
        canonical_json(b"abc")


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


def test_booleans_are_never_numbers_at_any_depth() -> None:
    assert same_plan_value({"x": 1}, {"x": True}) is False
    assert same_plan_value([1], [True]) is False
    assert same_plan_value([{"x": 0}], [{"x": False}]) is False
    assert same_plan_value({"x": True}, {"x": True}) is True


def test_nested_mappings_and_sequences_compare_structurally() -> None:
    assert same_plan_value({"a": {"b": [1, {"c": 2}]}}, {"a": {"b": [1, {"c": 2}]}}) is True
    assert same_plan_value({"a": [1, 2]}, {"a": [1, 3]}) is False
    assert same_plan_value({"a": 1}, {"b": 1}) is False
    assert same_plan_value({"a": 1, "b": 2}, {"a": 1}) is False
    assert same_plan_value([1, 2], (1, 2)) is True


def test_numeric_comparison_is_exact_without_float_rounding() -> None:
    assert same_plan_value(2**53, 2**53 + 1) is False
    assert same_plan_value(10**400, 10**400) is True
    assert same_plan_value(10**400, float("inf")) is False
    assert same_plan_value(10, float("nan")) is False
    assert same_plan_value(12.5, 12.5) is True
    assert same_plan_value(12.5, 12.500000000000002) is False


def test_a_deviation_keeps_the_value_each_side_reported() -> None:
    deviation = PlanDeviation(field="rateMbps", planned=10.0, actual=None)
    assert deviation.to_dict() == {"field": "rateMbps", "planned": 10.0, "actual": None}


# ---------------------------------------------------------------- generic digest


def test_canonical_digest_is_suitable_for_a_document_that_is_not_a_plan() -> None:
    document = {"operationType": "Create", "region": "A2ATestRegion"}
    reordered = {"region": "A2ATestRegion", "operationType": "Create"}
    assert canonical_digest(document) == canonical_digest(reordered)
    assert canonical_digest(document) != canonical_digest({**document, "region": "Other"})


def test_an_int_and_an_equal_float_compare_the_same_at_any_magnitude() -> None:
    """Comparison must stay truthful where the canonical profile refuses to serialise.

    The profile rejects integers beyond 2**53-1, but a value that cannot be hashed can still be
    compared: failing to compare it would put the digest and the deviation report in disagreement.
    """
    assert same_plan_value(2**60, float(2**60))
    assert not same_plan_value(2**60 + 1, float(2**60))
    assert not same_plan_value(10**400, float("inf"))
