"""A hop states what it did to the contract, and the next hop checks it.

The OSS proxy parses intent and rewrites the prose it forwards. That used to look like a hop that
"understands" the contract, which made it impossible to say whether the contract itself survived.
The distinction this file pins is narrower and checkable: rewriting the *view* is allowed, rewriting
the *contract* is a different act that must be declared and versioned.

Three claims:
- a carrier's output digest equals its input digest;
- a reducer that changes the contract must name the revision it produced;
- the receiving hop records what actually arrived, so a chain that disagrees with itself is caught
  without having to be believed.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import FIDELITY_FAST, WINDOW_POLICY_ANY, SimConfig
from protocol.contract_carriage import DELEGATION_KEY, append_hop, contract_digest
from shared.assurance import (
    ASSURANCE_DECLARED,
    ASSURANCE_ECHOED,
    ASSURANCE_RECOMPUTED,
    ROLE_CARRIER,
    ROLE_REDUCER,
    ROLE_UNKNOWN,
    VIOLATION_CARRIER_CHANGED,
    VIOLATION_CHAIN_GAP,
    VIOLATION_REDUCER_UNREVISED,
    VIOLATION_UNATTRIBUTED,
    Hop,
    check_roles,
    read_hops,
)

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
PLAN_TEXT = "\n".join(
    [
        "## Operation Type",
        "",
        "Create",
        "",
        "## Task Object",
        "",
        "The area is A2ATestRegion.",
        "",
        "## Task Context",
        "",
        "The time-segment-based guaranteed throughput target is {00:00-06:00,30Mbps}.",
    ]
)
OTHER_TEXT = PLAN_TEXT.replace("A2ATestRegion", "OtherRegion")


def _config() -> SimConfig:
    return SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY)


def _body(metadata: dict[str, object], *, message_id: str = "m-1") -> dict[str, object]:
    return {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"text": "run"}],
            "metadata": metadata,
        }
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "A2A-Extensions": TASK_T_EXTENSION_URI}


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL,
        json={"grantType": "password", "userName": "snbitestDefaultNorth", "value": "Changeme_123"},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


# ---------------------------------------------------------------- the vocabulary


def test_a_carrier_that_changed_the_contract_is_a_violation() -> None:
    hops = (Hop("oss", digest="sha256:a", emitted="sha256:b", role=ROLE_CARRIER),)

    violations = check_roles(hops)

    assert [violation.reason for violation in violations] == [VIOLATION_CARRIER_CHANGED]


def test_a_reducer_that_names_its_revision_is_fine() -> None:
    hops = (
        Hop(
            "oss",
            digest="sha256:a",
            emitted="sha256:b",
            role=ROLE_REDUCER,
            revision=2,
        ),
    )

    assert check_roles(hops) == ()


def test_a_reducer_that_changes_the_contract_without_a_revision_is_a_violation() -> None:
    hops = (Hop("oss", digest="sha256:a", emitted="sha256:b", role=ROLE_REDUCER),)

    assert [violation.reason for violation in check_roles(hops)] == [VIOLATION_REDUCER_UNREVISED]


def test_a_hop_with_no_role_that_changed_the_contract_is_unattributed() -> None:
    hops = (Hop("proxy", digest="sha256:a", emitted="sha256:b", role=ROLE_UNKNOWN),)

    assert [violation.reason for violation in check_roles(hops)] == [VIOLATION_UNATTRIBUTED]


def test_a_hop_that_forwards_unchanged_is_never_a_violation() -> None:
    for role in (ROLE_CARRIER, ROLE_REDUCER, ROLE_UNKNOWN):
        assert check_roles((Hop("x", digest="sha256:a", emitted="sha256:a", role=role),)) == ()


def test_a_chain_that_disagrees_with_itself_is_a_gap() -> None:
    hops = (
        Hop("ui", digest="sha256:a", emitted="sha256:a", role=ROLE_CARRIER),
        Hop("oss", digest="sha256:c", role=ROLE_CARRIER),
    )

    assert [violation.reason for violation in check_roles(hops)] == [VIOLATION_CHAIN_GAP]


def test_a_legacy_chain_makes_no_claims_and_earns_no_violation() -> None:
    assert check_roles(read_hops(["ui", "oss"])) == ()


def test_the_appended_hop_records_both_halves_of_its_claim() -> None:
    inbound = {TASK_T_EXTENSION_URI: PLAN_TEXT}

    carried = append_hop(inbound, hop="oss", assurance=ASSURANCE_ECHOED)
    hops = read_hops(carried[DELEGATION_KEY])

    assert hops[-1].hop == "oss"
    assert hops[-1].role == ROLE_CARRIER
    assert hops[-1].digest == contract_digest(inbound)
    assert hops[-1].emitted == hops[-1].digest
    assert check_roles(hops) == ()


def test_a_reducer_states_what_it_produced() -> None:
    inbound = {TASK_T_EXTENSION_URI: PLAN_TEXT}

    forwarded = append_hop(
        inbound,
        hop="oss",
        assurance=ASSURANCE_DECLARED,
        role=ROLE_REDUCER,
        revision=2,
        produces={TASK_T_EXTENSION_URI: OTHER_TEXT},
    )

    hop = read_hops(forwarded[DELEGATION_KEY])[-1]
    assert hop.digest != hop.emitted
    assert hop.revision == 2
    assert check_roles((hop,)) == ()


# ---------------------------------------------------------------- two hops, end to end


async def test_a_carried_contract_reaches_the_server_unchanged() -> None:
    inbound = {TASK_T_EXTENSION_URI: PLAN_TEXT}
    forwarded = append_hop(inbound, hop="oss", assurance=ASSURANCE_ECHOED)

    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        response = await client.post(
            SEND_URL,
            json=_body({TASK_T_EXTENSION_URI: PLAN_TEXT, DELEGATION_KEY: forwarded[DELEGATION_KEY]}),
            headers=_headers(token),
        )

    assert response.status_code == 200, response.text
    task_id = response.json()["task"]["id"]
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        fetched = await client.get(
            f"/a2a/json/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
        )
    recorded = fetched.json()["task"]["delegation"]
    assert [hop["hop"] for hop in recorded] == ["oss", "nms"]
    assert recorded[-1]["assurance"] == ASSURANCE_RECOMPUTED
    assert recorded[-1]["digest"] == recorded[0]["emitted"]


async def test_a_carrier_that_rewrote_the_contract_is_refused() -> None:
    """The proxy rewrote a contract key. Its own role says it should not have."""
    inbound = {TASK_T_EXTENSION_URI: PLAN_TEXT}
    forwarded = append_hop(
        inbound,
        hop="oss",
        assurance=ASSURANCE_ECHOED,
        role=ROLE_CARRIER,
        produces={TASK_T_EXTENSION_URI: OTHER_TEXT},
    )

    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        response = await client.post(
            SEND_URL,
            json=_body({TASK_T_EXTENSION_URI: OTHER_TEXT, DELEGATION_KEY: forwarded[DELEGATION_KEY]}),
            headers=_headers(token),
        )

    assert response.status_code == 409, response.text
    assert response.json()["reasonCode"] == VIOLATION_CARRIER_CHANGED


async def test_a_chain_that_misstates_what_was_sent_is_refused() -> None:
    """No hop has to be believed: the server states what arrived and the two must agree."""
    # The first hop is allowed to rewrite and says so (reducer + revision). The second one reports
    # receiving something other than what the first said it sent: nobody has to be believed, the
    # two statements simply do not meet.
    chain = {
        "version": 2,
        "hops": [
            {
                "hop": "oss",
                "digest": "sha256:what-it-received",
                "emitted": "sha256:what-it-rewrote",
                "role": ROLE_REDUCER,
                "revision": 2,
                "assurance": ASSURANCE_DECLARED,
            },
            {
                "hop": "ran",
                "digest": "sha256:something-else-entirely",
                "emitted": "sha256:something-else-entirely",
                "role": ROLE_CARRIER,
                "assurance": ASSURANCE_ECHOED,
            },
        ],
    }

    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        response = await client.post(
            SEND_URL,
            json=_body({TASK_T_EXTENSION_URI: PLAN_TEXT, DELEGATION_KEY: chain}),
            headers=_headers(token),
        )

    assert response.status_code == 409, response.text
    assert response.json()["reasonCode"] == VIOLATION_CHAIN_GAP


async def test_a_request_without_a_chain_is_unaffected() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        response = await client.post(
            SEND_URL,
            json=_body({TASK_T_EXTENSION_URI: PLAN_TEXT}),
            headers=_headers(token),
        )

    assert response.status_code == 200, response.text
    assert "delegation" not in response.json()["task"]


@pytest.mark.parametrize(
    "entry",
    [
        {"hop": "x"},
        {"hop": "x", "role": "nonsense"},
        {"hop": "x", "revision": True},
        {"hop": "x", "digest": 5},
    ],
)
def test_a_hop_entry_never_raises_on_malformed_fields(entry: Mapping[str, object]) -> None:
    hops = read_hops({"version": 2, "hops": [entry]})

    assert len(hops) == 1
    assert check_roles(hops) == ()
