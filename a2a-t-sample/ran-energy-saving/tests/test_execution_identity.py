"""v2 prototype, pillar C: one request, one execution, and a receipt that says so.

The identity is computed from the request document, not from a message id, so a client that crashed
and re-sent the same statement still names the same request. That is what makes a retry safe: the
server returns the Task that already answers it instead of executing the plan again.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import FIDELITY_FAST, WINDOW_POLICY_ANY, SimConfig
from shared.readback import read_readback
from shared.typed_payload import build_statement, statement_metadata

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
DELEGATION_KEY = "a2at-sample/delegation"


def _config() -> SimConfig:
    return SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY)


def _statement() -> dict[str, object]:
    return statement_metadata(
        build_statement(
            operation="Create",
            region="A2ATestRegion",
            scopes=[{"window": "00:00-06:00", "rateMbps": 10}],
        )
    )


def _body(*, message_id: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "message": {
            "messageId": message_id,
            "role": "ROLE_USER",
            "parts": [{"text": "run"}],
            "metadata": {**_statement(), **(extra or {})},
        }
    }


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL,
        json={"grantType": "password", "userName": "snbitestDefaultNorth", "value": "Changeme_123"},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _headers(token: str, *, extensions: str = TASK_T_EXTENSION_URI) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "A2A-Extensions": extensions}


async def _wait(client: httpx.AsyncClient, token: str, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    document: dict[str, object] = {}
    while time.monotonic() < deadline:
        fetched = await client.get(f"/a2a/json/tasks/{task_id}", headers=_headers(token))
        document = fetched.json()["task"]
        artifacts = document.get("artifacts")
        if isinstance(artifacts, list) and artifacts:
            return document
        await asyncio.sleep(0.01)
    return document


async def test_a_retry_returns_the_same_task_instead_of_executing_again() -> None:
    """The crash-retry case: a new message id, the same statement, one execution."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(SEND_URL, json=_body(message_id="m-1"), headers=_headers(token))
        second = await client.post(SEND_URL, json=_body(message_id="m-2"), headers=_headers(token))
        metrics = await client.get("/a2a/json/metrics", headers=_headers(token))

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["task"]["id"] == second.json()["task"]["id"]
    assert metrics.json()["protocols"]["a2a-t"]["duplicates"] == 1
    assert metrics.json()["protocols"]["a2a-t"]["requests"] == 2


async def test_two_different_statements_execute_twice() -> None:
    """Identity must distinguish real requests, otherwise retry safety would swallow work."""
    other = statement_metadata(
        build_statement(
            operation="Create",
            region="A2ATestRegion",
            scopes=[{"window": "00:00-06:00", "rateMbps": 5}],
        )
    )
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(SEND_URL, json=_body(message_id="m-1"), headers=_headers(token))
        second = await client.post(
            SEND_URL,
            json=_body(message_id="m-2", extra=other),
            headers=_headers(token),
        )
        metrics = await client.get("/a2a/json/metrics", headers=_headers(token))

    assert first.json()["task"]["id"] != second.json()["task"]["id"]
    assert metrics.json()["protocols"]["a2a-t"]["duplicates"] == 0


async def test_the_receipt_names_the_request_the_execution_and_the_agreement() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        sent = await client.post(SEND_URL, json=_body(message_id="m-1"), headers=_headers(token))
        document = await _wait(client, token, str(sent.json()["task"]["id"]))

    artifacts = document["artifacts"]
    assert isinstance(artifacts, list) and artifacts
    readback = read_readback(artifacts[-1]["metadata"])

    assert readback is not None
    assert readback.request_id.startswith("sha256:")
    assert readback.execution_id.startswith("exe-")
    assert readback.agreement_id.startswith("agr-")
    assert readback.revision == 1


async def test_the_delegation_chain_is_echoed_with_this_hop_appended() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        sent = await client.post(
            SEND_URL,
            json=_body(message_id="m-1", extra={DELEGATION_KEY: ["ui", "oss"]}),
            headers=_headers(token),
        )
        document = await _wait(client, token, str(sent.json()["task"]["id"]))

    artifacts = document["artifacts"]
    assert isinstance(artifacts, list) and artifacts
    readback = read_readback(artifacts[-1]["metadata"])

    assert readback is not None
    assert readback.delegation_chain == ("ui", "oss", "nms")


async def test_a_retry_after_completion_returns_the_completed_task() -> None:
    """The returned Task is the one that already ran, so the client can read its receipt."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(SEND_URL, json=_body(message_id="m-1"), headers=_headers(token))
        task_id = str(first.json()["task"]["id"])
        await _wait(client, token, task_id)
        retried = await client.post(SEND_URL, json=_body(message_id="m-9"), headers=_headers(token))

    assert retried.json()["task"]["id"] == task_id
    assert retried.json()["task"]["artifacts"]
