"""Tests of the simulator's S8 readback: the executed plan next to the approved digest."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from a2a_t.core.metadata import NEGOTIATION_T_EXTENSION_URI, TASK_T_EXTENSION_URI
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import (
    CARRIER_MESSAGE,
    FIDELITY_FAST,
    WINDOW_POLICY_ANY,
    SimConfig,
)
from shared.approval import APPROVAL_METADATA_KEY, read_approval_request
from shared.readback import READBACK_METADATA_KEY, read_readback

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
USER = "snbitestDefaultNorth"
VALUE = "Changeme_123"


@pytest.fixture(autouse=True)
def _reset_plans() -> None:
    """Keep plan ids deterministic across tests."""
    ran_data.reset()


def _config(*, deviation: bool = False) -> SimConfig:
    return SimConfig(
        fidelity=FIDELITY_FAST,
        negotiation_carrier=CARRIER_MESSAGE,
        approval=True,
        window_policy=WINDOW_POLICY_ANY,
        readback_deviation=deviation,
    )


def _prompt() -> str:
    return "\n".join(
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


async def _approve_and_collect(client: httpx.AsyncClient, token: str) -> dict[str, object]:
    """Accept, approve, and return the completed Task document."""
    response = await client.post(
        SEND_URL,
        json={
            "message": {
                "messageId": "m-1",
                "role": "ROLE_USER",
                "parts": [{"text": "create"}],
                "metadata": {TASK_T_EXTENSION_URI: _prompt()},
            }
        },
        headers=_headers(token),
    )
    task = response.json()["task"]
    metadata = task["status"]["message"]["metadata"]
    request = read_approval_request({APPROVAL_METADATA_KEY: metadata[APPROVAL_METADATA_KEY]})
    assert request is not None

    await client.post(
        SEND_URL,
        json={
            "message": {
                "messageId": "m-2",
                "role": "ROLE_USER",
                "parts": [{"text": "approval decision"}],
                "metadata": {
                    APPROVAL_METADATA_KEY: {
                        "version": 1,
                        "kind": "approval-decision",
                        "approvalId": request.approval_id,
                        "planDigest": request.plan_digest,
                        "outcome": "approve",
                        "reasonCode": "operator_accepted",
                    }
                },
                "taskId": task["id"],
                "contextId": task["contextId"],
            }
        },
        headers=_headers(token),
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        fetched = await client.get(
            f"/a2a/json/tasks/{task['id']}", headers={"X-Auth-Token": token}
        )
        document = fetched.json()["task"]
        if document["artifacts"] or document["status"]["state"] in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_REJECTED",
        }:
            return document
        await asyncio.sleep(0.01)
    return document


def _readback_of(task: dict[str, object]) -> object:
    artifacts = task["artifacts"]
    assert isinstance(artifacts, list) and artifacts
    artifact = artifacts[-1]
    assert isinstance(artifact, dict)
    metadata = artifact.get("metadata")
    assert isinstance(metadata, dict)
    return read_readback(metadata)


async def test_the_report_carries_the_approved_plan_and_no_deviation() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task = await _approve_and_collect(client, token)

        readback = _readback_of(task)

    assert readback is not None
    assert readback.plan_digest.startswith("sha256:")
    assert readback.deviations == ()
    assert readback.executed["rateMbps"] == 10.0


async def test_the_deviation_switch_is_visible_in_the_readback() -> None:
    """A network that applied another rate must be visible field by field."""
    transport = httpx.ASGITransport(app=build_sim_app(_config(deviation=True)))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task = await _approve_and_collect(client, token)

        readback = _readback_of(task)

    assert readback is not None
    assert readback.deviations == (
        {"field": "rateMbps", "planned": 10.0, "actual": 8.0},
    )
    assert readback.executed["rateMbps"] == 8.0


def test_the_envelope_key_is_the_documented_one() -> None:
    assert READBACK_METADATA_KEY == "a2at-sample/readback"
