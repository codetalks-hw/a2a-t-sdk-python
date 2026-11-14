"""A hop that does not understand a contract must still carry it honestly."""

from __future__ import annotations

from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from a2a_t.core.plan_binding import canonical_digest
from protocol.contract_carriage import (
    ASSURANCE_DECLARED,
    ASSURANCE_ECHOED,
    CONTRACT_KEYS,
    DELEGATION_KEY,
    DELEGATION_VERSION,
    ROLE_CARRIER,
    append_hop,
    carried,
    contract_digest,
    verify_carried,
)
from shared.agreement import AGREEMENT_METADATA_KEY
from shared.approval import APPROVAL_METADATA_KEY
from shared.change_order import BASELINE_METADATA_KEY
from shared.typed_payload import TYPED_TASK_T_METADATA_KEY


def _hops(metadata: dict[str, object]) -> list[dict[str, object]]:
    """Return the delegation hops of one appended map, narrowed for the assertions."""
    delegation = metadata[DELEGATION_KEY]
    assert isinstance(delegation, dict)
    hops = delegation["hops"]
    assert isinstance(hops, list)
    return hops


def _metadata() -> dict[str, object]:
    return {
        TASK_T_EXTENSION_URI: "## Task Object\nThe area is Region A.",
        NEGOTIATION_T_EXTENSION_URI: "Propose a solution.",
        NEGOTIATION_CONTEXT_METADATA_KEY: {
            "id": "n-1",
            "round": 1,
            "maxRounds": 5,
            "performative": "PROPOSE",
        },
        TEMPLATE_URI_METADATA_KEY: "Negotiation-T/target-negotiation/propose/v1",
        TYPED_TASK_T_METADATA_KEY: {"operation": "Create", "region": "Region A"},
        AGREEMENT_METADATA_KEY: {"agreementId": "agr-1", "revision": 1},
        "a2at-sample/request-id": "req-1",
        BASELINE_METADATA_KEY: {"planDigest": "sha256:old"},
        APPROVAL_METADATA_KEY: {"kind": "approval-request"},
        "sampleRunId": "run-1",
        "pure": {"phase": "question"},
    }


def test_carried_filters_to_the_contract_keys() -> None:
    """Presentation and correlation metadata are not part of the contract."""
    filtered = carried(_metadata())

    assert tuple(filtered) == CONTRACT_KEYS
    assert "sampleRunId" not in filtered
    assert "pure" not in filtered


def test_carried_keeps_the_values_unchanged() -> None:
    """Normalising or copying a value is the alteration the digest must catch."""
    metadata = _metadata()
    filtered = carried(metadata)

    for key in CONTRACT_KEYS:
        assert filtered[key] is metadata[key]


def test_carried_of_nothing_is_empty() -> None:
    assert carried(None) == {}
    assert carried({}) == {}
    assert carried({"unrelated": 1}) == {}


def test_the_digest_is_none_when_nothing_is_carried() -> None:
    assert contract_digest(None) is None
    assert contract_digest({}) is None
    assert contract_digest({"pure": {"phase": "question"}}) is None


def test_the_digest_is_the_canonical_digest_of_the_carried_map() -> None:
    metadata = _metadata()

    assert contract_digest(metadata) == canonical_digest({"carried": carried(metadata)})


def test_the_digest_is_stable_across_key_order() -> None:
    reordered = dict(reversed(list(_metadata().items())))

    assert contract_digest(_metadata()) == contract_digest(reordered)


def test_the_digest_changes_when_a_contract_value_changes() -> None:
    metadata = _metadata()
    changed = {**metadata, TASK_T_EXTENSION_URI: "## Task Object\nThe area is Region B."}

    assert contract_digest(metadata) != contract_digest(changed)


def test_the_digest_ignores_non_contract_keys() -> None:
    """A hop may add a presentation tag without falsifying its digest claim."""
    metadata = _metadata()
    noisy = {**metadata, "sampleRunId": "run-2", "pure": {"phase": "answer"}, "extra": [1, 2]}

    assert contract_digest(metadata) == contract_digest(noisy)


def test_append_hop_writes_the_v2_shape() -> None:
    result = append_hop(_metadata(), hop="oss", assurance=ASSURANCE_ECHOED)

    delegation = result[DELEGATION_KEY]
    digest = contract_digest(_metadata())
    assert isinstance(delegation, dict)
    assert delegation["version"] == DELEGATION_VERSION
    # A hop records both halves of its claim: what it received and what it sends on. A carrier's
    # two values are equal by construction, which is the statement the next hop can check.
    assert delegation["hops"] == [
        {
            "hop": "oss",
            "digest": digest,
            "emitted": digest,
            "role": ROLE_CARRIER,
            "assurance": ASSURANCE_ECHOED,
        }
    ]


def test_append_hop_returns_a_new_mapping() -> None:
    metadata = _metadata()
    result = append_hop(metadata, hop="oss", assurance=ASSURANCE_ECHOED)

    assert result is not metadata
    assert DELEGATION_KEY not in metadata


def test_append_hop_keeps_the_carried_keys_verbatim() -> None:
    metadata = _metadata()
    result = append_hop(metadata, hop="oss", assurance=ASSURANCE_ECHOED)

    assert carried(result) == carried(metadata)
    assert set(result) == {*CONTRACT_KEYS, DELEGATION_KEY}


def test_append_hop_upgrades_a_legacy_bare_string_entry() -> None:
    metadata = {**_metadata(), DELEGATION_KEY: ["ui"]}

    result = append_hop(metadata, hop="oss", assurance=ASSURANCE_ECHOED)

    assert _hops(result)[0] == {
        "hop": "ui",
        "digest": "",
        "assurance": ASSURANCE_DECLARED,
    }


def test_append_hop_preserves_an_existing_v2_entry() -> None:
    existing = {"hop": "ui", "digest": "sha256:abc", "assurance": "recomputed"}
    metadata = {**_metadata(), DELEGATION_KEY: {"version": 2, "hops": [existing]}}

    result = append_hop(metadata, hop="oss", assurance=ASSURANCE_ECHOED)

    assert _hops(result)[0] == existing
    assert _hops(result)[-1]["hop"] == "oss"


def test_the_appended_digest_is_the_pre_append_digest() -> None:
    """The entry states what this hop received, not what it produced."""
    metadata = _metadata()
    before = contract_digest(metadata)

    result = append_hop(metadata, hop="oss", assurance=ASSURANCE_ECHOED)

    assert _hops(result)[-1]["digest"] == before


def test_append_hop_with_nothing_carried_records_an_empty_digest() -> None:
    """A hop may be in the chain even when this request carried no contract."""
    result = append_hop({"sampleRunId": "run-1"}, hop="oss", assurance=ASSURANCE_ECHOED)

    assert set(result) == {DELEGATION_KEY}
    assert _hops(result)[-1]["digest"] == ""


def test_verify_carried_accepts_an_identical_forward() -> None:
    inbound = _metadata()
    outbound = append_hop(inbound, hop="oss", assurance=ASSURANCE_ECHOED)

    assert verify_carried(inbound, outbound) is True


def test_verify_carried_rejects_a_changed_value() -> None:
    inbound = _metadata()
    outbound = {**_metadata(), TASK_T_EXTENSION_URI: "rewritten"}

    assert verify_carried(inbound, outbound) is False


def test_verify_carried_rejects_a_missing_key() -> None:
    inbound = _metadata()
    outbound = {key: value for key, value in _metadata().items() if key != TYPED_TASK_T_METADATA_KEY}

    assert verify_carried(inbound, outbound) is False


def test_verify_carried_ignores_keys_the_hop_added() -> None:
    inbound = _metadata()
    outbound = {**_metadata(), "hopNote": "carried", DELEGATION_KEY: {"version": 2, "hops": []}}

    assert verify_carried(inbound, outbound) is True


def test_verify_carried_of_an_empty_inbound_is_trivially_true() -> None:
    assert verify_carried(None, None) is True
    assert verify_carried({}, {"anything": 1}) is True
