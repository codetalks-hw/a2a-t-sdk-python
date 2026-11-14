"""S7: what survives when the client that started the exchange is gone.

A2A-T negotiates before a Task exists, so the only handle to an open negotiation is the id the
server returned in the proposal. Plain A2A creates a Task on the first message, so the Task id is a
handle from the start. These tests pin the observable difference; the design note records which
part of it is a protocol property and which is client storage discipline.
"""

from __future__ import annotations

import httpx
import pytest
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
)
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import CARRIER_MESSAGE, FIDELITY_FAST, PURE_QUESTION_CONFIRM, SimConfig

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
USER = "snbitestDefaultNorth"
VALUE = "Changeme_123"


@pytest.fixture(autouse=True)
def _reset_plans() -> None:
    """Keep plan ids deterministic across tests."""
    ran_data.reset()


def _config(**overrides: object) -> SimConfig:
    fields: dict[str, object] = {
        "fidelity": FIDELITY_FAST,
        "negotiation_carrier": CARRIER_MESSAGE,
        "pure_question": PURE_QUESTION_CONFIRM,
    }
    fields.update(overrides)
    return SimConfig(**fields)  # ty: ignore[invalid-argument-type]


def _prompt(*, region: str = "A2ATestRegion") -> str:
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
            "Energy saving time: 00:00~06:00; guaranteed throughput: 10 Mbps.",
        ]
    )


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL, json={"grantType": "password", "userName": USER, "value": VALUE}
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "A2A-Extensions": f"{TASK_T_EXTENSION_URI},{NEGOTIATION_T_EXTENSION_URI}",
    }


def _task_t_body(prompt: str, *, metadata: dict[str, object] | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"text": "create"}],
        "metadata": {TASK_T_EXTENSION_URI: prompt, **(metadata or {})},
    }
    return {"message": message}


def _negotiation_context(response: httpx.Response) -> dict[str, object]:
    """Return the proposal's negotiationContext from either carrier."""
    document = response.json()
    if isinstance(document.get("message"), dict):
        metadata = document["message"].get("metadata") or {}
    else:
        task = document.get("task") or {}
        metadata = ((task.get("status") or {}).get("message") or {}).get("metadata") or {}
    context = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
    assert isinstance(context, dict), document
    return context


async def test_a_new_request_does_not_resume_an_open_a2a_t_negotiation() -> None:
    """Re-sending the same request starts a *new* negotiation: the id is the only handle."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(
            SEND_URL, json=_task_t_body(_prompt(region="UnknownRegion")), headers=_headers(token)
        )
        second = await client.post(
            SEND_URL, json=_task_t_body(_prompt(region="UnknownRegion")), headers=_headers(token)
        )

    assert first.status_code == 200 and second.status_code == 200
    first_id = _negotiation_context(first)["id"]
    second_id = _negotiation_context(second)["id"]
    assert first_id != second_id


async def test_the_negotiation_id_does_resume_the_exchange() -> None:
    """With the id persisted, the same reply lands on the open negotiation."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        proposal = await client.post(
            SEND_URL, json=_task_t_body(_prompt(region="UnknownRegion")), headers=_headers(token)
        )
        context = _negotiation_context(proposal)

        accepted = await client.post(
            SEND_URL,
            json=_task_t_body(
                _prompt(),
                metadata={
                    NEGOTIATION_T_EXTENSION_URI: "Accept.",
                    NEGOTIATION_CONTEXT_METADATA_KEY: {
                        "id": context["id"],
                        "round": 1,
                        "maxRounds": 5,
                        "performative": "ACCEPT",
                    },
                },
            ),
            headers=_headers(token),
        )

    assert accepted.status_code == 200, accepted.text
    assert isinstance(accepted.json().get("task"), dict)


async def test_a_fresh_plain_client_resumes_from_the_task_id_alone() -> None:
    """Plain A2A has a server-side handle from the first message."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    plain = {
        "message": {
            "messageId": "p-1",
            "role": "ROLE_USER",
            "parts": [
                {
                    "text": (
                        "Implement energy saving in A2ATestRegion, guaranteed throughput "
                        "10 Mbps from 00:00 to 06:00."
                    )
                }
            ],
        }
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(
            SEND_URL, json=plain, headers={"Authorization": f"Bearer {token}"}
        )
        question = first.json()["message"]
        context_id = question["contextId"]

    # A restarted client knows only the context id the first exchange created.
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as restarted:
        token = await _token(restarted)
        follow = {
            "message": {
                "messageId": "p-2",
                "role": "ROLE_USER",
                "parts": [{"text": "Yes, confirm the plan."}],
                "contextId": context_id,
            }
        }
        second = await restarted.post(
            SEND_URL, json=follow, headers={"Authorization": f"Bearer {token}"}
        )
        continued = second.json()["task"]

    assert continued["contextId"] == context_id
