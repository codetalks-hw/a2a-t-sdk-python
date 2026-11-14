"""Is pre-task negotiation load-bearing, or is the machine-readable answer the whole win?

v1 answers a negotiation with a bare message, before any Task exists. That choice is the root of two
weaknesses this sample had to patch around: a proposal cannot be enumerated by the protocol's own task
listing, and an abandoned exchange needs its own TTL because nothing else sweeps it.

The simulator supports both carriers, so the question is measurable instead of arguable. Same request,
same plan, same cost -- and only one of the two carriers leaves the proposal recoverable by a client
that kept no private state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
)
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import (
    CARRIER_MESSAGE,
    CARRIER_TASK,
    FIDELITY_FAST,
    PURE_QUESTION_ISSUE,
    WINDOW_POLICY_ANY,
    SimConfig,
)

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
TASKS_URL = "/a2a/json/tasks"
NEGOTIATIONS_URL = "/a2a/json/negotiations"
METRICS_URL = "/a2a/json/metrics"
USER = "snbitestDefaultNorth"
VALUE = "Changeme_123"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"


def _config(carrier: str) -> SimConfig:
    return SimConfig(
        fidelity=FIDELITY_FAST,
        negotiation_carrier=carrier,
        pure_question=PURE_QUESTION_ISSUE,
        window_policy=WINDOW_POLICY_ANY,
    )


def _prompt(*, region: str = "A2ATestRegion", window: str = "00:00~06:00") -> str:
    return "\n".join(
        [
            "## Operation Type",
            "",
            "Create",
            "",
            "## Task Object",
            "",
            f"The area is {region}.",
            "",
            "## Task Context",
            "",
            f"Energy saving time: {window}; guaranteed throughput: 30 Mbps.",
        ]
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "A2A-Extensions": f"{TASK_T_EXTENSION_URI},{NEGOTIATION_T_EXTENSION_URI}",
    }


def _body(
    prompt: str | None,
    *,
    task_id: str | None = None,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if prompt is not None:
        metadata[TASK_T_EXTENSION_URI] = prompt
    if context is not None:
        metadata[NEGOTIATION_CONTEXT_METADATA_KEY] = context
    message: dict[str, object] = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"text": "run"}],
        "metadata": metadata,
    }
    if task_id is not None:
        message["taskId"] = task_id
    return {"message": message}


async def _client(config: SimConfig) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_sim_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        yield client


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL,
        json={"grantType": "password", "userName": USER, "value": VALUE},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _proposal_context(document: dict[str, object]) -> dict[str, object]:
    """Return the negotiation context a proposal carries, from either response shape."""
    if "task" in document:
        raw_task = document["task"]
        assert isinstance(raw_task, dict)
        metadata = raw_task["status"]["message"]["metadata"]
    else:
        raw_message = document["message"]
        assert isinstance(raw_message, dict)
        metadata = raw_message["metadata"]
    context = metadata[NEGOTIATION_CONTEXT_METADATA_KEY]
    assert isinstance(context, dict)
    return context


async def _propose(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    """Send the under-specified request and return the proposal context."""
    response = await client.post(SEND_URL, json=_body(_prompt(region="")), headers=headers)
    assert response.status_code == 200, response.text
    return _proposal_context(response.json())


async def _accept(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    context: dict[str, object],
    *,
    task_id: str | None = None,
) -> httpx.Response:
    return await client.post(
        SEND_URL,
        json=_body(
            _prompt(),
            task_id=task_id,
            context={
                "id": context["id"],
                "round": context["round"],
                "maxRounds": context["maxRounds"],
                "performative": "ACCEPT",
            },
        ),
        headers=headers,
    )


# ---------------------------------------------------------------- cost


@pytest.mark.parametrize("carrier", [CARRIER_MESSAGE, CARRIER_TASK])
async def test_a_proposal_costs_the_same_under_either_carrier(carrier: str) -> None:
    """Two requests and one round: the carrier does not change the exchange, only where it lives."""
    async for client in _client(_config(carrier)):
        token = await _token(client)
        headers = _headers(token)
        response = await client.post(
            SEND_URL, json=_body(_prompt(region="")), headers=headers
        )
        proposal = _proposal_context(response.json())
        accepted = await _accept(client, headers, proposal)
        metrics = await client.get(METRICS_URL)

    counters = metrics.json()["protocols"]["a2a-t"]
    # The carrier must actually be the one under test, otherwise "same cost" is trivially true.
    document = response.json()
    assert ("task" in document) == (carrier == CARRIER_TASK)
    assert accepted.status_code == 200, accepted.text
    assert counters["requests"] == 2
    assert counters["rounds"] == 1
    assert counters["outcomes"].get("accepted") == 1


# ---------------------------------------------------------------- enumeration


async def test_a_carried_proposal_is_recoverable_from_the_task_listing_alone() -> None:
    """A restarted client needs no private store: the proposal is a task, and the task carries it."""
    async for client in _client(_config(CARRIER_TASK)):
        token = await _token(client)
        headers = _headers(token)
        await _propose(client, headers)

        listing = await client.get(TASKS_URL, headers={"Authorization": f"Bearer {token}"})
        tasks = listing.json()["tasks"]
        waiting = [task for task in tasks if task["status"]["state"] == STATE_INPUT_REQUIRED]

        assert len(waiting) == 1
        recovered = waiting[0]["status"]["message"]["metadata"][
            NEGOTIATION_CONTEXT_METADATA_KEY
        ]
        resumed = await _accept(client, headers, recovered, task_id=waiting[0]["id"])

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["task"]["status"]["state"] != "TASK_STATE_REJECTED"


async def test_a_bare_proposal_is_invisible_to_the_task_listing() -> None:
    """The message carrier leaves nothing for the protocol's own enumeration to find."""
    async for client in _client(_config(CARRIER_MESSAGE)):
        token = await _token(client)
        headers = _headers(token)
        await _propose(client, headers)

        listing = await client.get(TASKS_URL, headers={"Authorization": f"Bearer {token}"})
        negotiations = await client.get(
            NEGOTIATIONS_URL, headers={"Authorization": f"Bearer {token}"}
        )

    assert listing.json()["tasks"] == []
    # Only the sample-invented endpoint can answer, which is exactly the gap being measured.
    assert len(negotiations.json()["negotiations"]) == 1


# ---------------------------------------------------------------- outcome


async def test_both_carriers_accept_the_same_plan() -> None:
    """The carrier is a transport decision; the agreed plan must not depend on it."""
    digests: list[str] = []
    for carrier in (CARRIER_MESSAGE, CARRIER_TASK):
        async for client in _client(_config(carrier)):
            token = await _token(client)
            headers = _headers(token)
            proposal = await _propose(client, headers)
            task_id = None
            if carrier == CARRIER_TASK:
                listing = await client.get(TASKS_URL, headers={"Authorization": f"Bearer {token}"})
                task_id = listing.json()["tasks"][0]["id"]
            accepted = await _accept(client, headers, proposal, task_id=task_id)
            agreement = accepted.json()["task"]["metadata"]["a2at-sample/agreement"]
            digests.append(str(agreement["planDigest"]))

    assert digests[0] == digests[1]
