"""P3: a change order must name the plan it replaces.

S6 showed a client can bind an approval to the plan it accepted. A change order asks the same
question one step later, and the answer differs by protocol: an A2A-T client holds the accepted
plan and can compute its digest; a plain-A2A client holds a sentence. With ``baseline=required``
the simulator refuses the change orders that cannot name their baseline, which is exactly the set
the plain arm can produce.
"""

from __future__ import annotations

import httpx
import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.approval import plan_for
from demo.nms_sim.config import (
    BASELINE_REQUIRED,
    CARRIER_MESSAGE,
    FIDELITY_FAST,
    PURE_QUESTION_ISSUE,
    SimConfig,
)
from demo.nms_sim.intent import parse_message_request
from shared.approval import plan_digest
from shared.change_order import (
    BASELINE_ERROR_METADATA_KEY,
    BASELINE_METADATA_KEY,
    REASON_MISSING,
    REASON_STALE,
    baseline_metadata,
    read_baseline_digest,
)

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
        "pure_question": PURE_QUESTION_ISSUE,
        "baseline": BASELINE_REQUIRED,
    }
    fields.update(overrides)
    return SimConfig(**fields)  # ty: ignore[invalid-argument-type]


def _prompt(*, operation: str = "Create", rate: str = "30 Mbps") -> str:
    return "\n".join(
        [
            "## Operation Type",
            "",
            operation,
            "",
            "## Task Object",
            "",
            "The area is A2ATestRegion.",
            "",
            "## Task Context",
            "",
            f"Energy saving time: 00:00~06:00; guaranteed throughput: {rate}.",
        ]
    )


def _task_t_body(
    prompt: str,
    *,
    task_id: str | None = None,
    metadata: dict[str, object] | None = None,
    message_id: str = "m-1",
) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": "create"}],
        "metadata": {TASK_T_EXTENSION_URI: prompt, **(metadata or {})},
    }
    if task_id is not None:
        message["taskId"] = task_id
    return {"message": message}


def _plain_body(text: str, *, task_id: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": "p-1",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if task_id is not None:
        message["taskId"] = task_id
    return {"message": message}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "A2A-Extensions": TASK_T_EXTENSION_URI,
    }


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL, json={"grantType": "password", "userName": USER, "value": VALUE}
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _digest_of(prompt: str, config: SimConfig) -> str:
    """Return the digest the server stores for one accepted Task-T.

    This is the value an A2A-T client can recompute from the document it accepted. It is built with
    the simulator's own parser only so the test does not have to guess the window spelling.
    """
    body = _task_t_body(prompt)
    intent = parse_message_request(body, _headers("token"), config)
    return plan_digest(plan_for(intent))


def _baseline_of(digest: str, task_id: str) -> dict[str, object]:
    """Return the envelope naming one already-computed baseline digest."""
    return {
        BASELINE_METADATA_KEY: {
            "version": 1,
            "planDigest": digest,
            "taskId": task_id,
        }
    }


async def _create(client: httpx.AsyncClient, token: str, prompt: str) -> str:
    response = await client.post(SEND_URL, json=_task_t_body(prompt), headers=_headers(token))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    return str(task["id"])


def _reason(response: httpx.Response) -> str | None:
    message = ((response.json().get("task") or {}).get("status") or {}).get("message") or {}
    metadata = message.get("metadata") or {}
    raw = metadata.get(BASELINE_ERROR_METADATA_KEY)
    return raw.get("reasonCode") if isinstance(raw, dict) else None


def test_the_envelope_round_trips_the_digest_of_the_named_plan() -> None:
    plan = {"operation": "Create", "region": "A2ATestRegion", "window": "00:00-06:00", "rateMbps": 30.0}
    metadata = baseline_metadata(plan, task_id="task-1")

    assert read_baseline_digest(metadata) == plan_digest(plan)
    assert read_baseline_digest({BASELINE_METADATA_KEY: {"version": 9, "planDigest": "x"}}) is None
    assert read_baseline_digest(None) is None


async def test_a_change_order_without_a_baseline_is_refused() -> None:
    config = _config()
    transport = httpx.ASGITransport(app=build_sim_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task_id = await _create(client, token, _prompt())
        change = await client.post(
            SEND_URL,
            json=_task_t_body(_prompt(operation="Modify", rate="10 Mbps"), task_id=task_id),
            headers=_headers(token),
        )

    assert change.status_code == 200, change.text
    assert change.json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert _reason(change) == REASON_MISSING


async def test_a_change_order_against_the_current_plan_is_accepted() -> None:
    config = _config()
    accepted = _prompt()
    transport = httpx.ASGITransport(app=build_sim_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task_id = await _create(client, token, accepted)
        change = await client.post(
            SEND_URL,
            json=_task_t_body(
                _prompt(operation="Modify", rate="10 Mbps"),
                task_id=task_id,
                metadata=_baseline_of(_digest_of(accepted, config), task_id),
            ),
            headers=_headers(token),
        )

    assert change.status_code == 200, change.text
    assert change.json()["task"]["status"]["state"] != "TASK_STATE_REJECTED"


async def test_a_change_order_against_a_superseded_plan_is_refused() -> None:
    """``baseline_stale`` stands in for a second operator who changed the Task first."""
    config = _config(baseline_stale=True)
    accepted = _prompt()
    transport = httpx.ASGITransport(app=build_sim_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task_id = await _create(client, token, accepted)
        change = await client.post(
            SEND_URL,
            json=_task_t_body(
                _prompt(operation="Modify", rate="10 Mbps"),
                task_id=task_id,
                metadata=_baseline_of(_digest_of(accepted, config), task_id),
            ),
            headers=_headers(token),
        )

    assert change.status_code == 200, change.text
    assert change.json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert _reason(change) == REASON_STALE


async def test_a_plain_change_order_cannot_name_a_baseline_at_all() -> None:
    """The plain-A2A client holds a sentence, not a plan: with ``required`` it cannot pass."""
    config = _config()
    transport = httpx.ASGITransport(app=build_sim_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(
            SEND_URL,
            json=_plain_body(
                "Implement energy saving in A2ATestRegion, guaranteed throughput 30 Mbps "
                "from 00:00 to 06:00."
            ),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 200, created.text
        document = created.json()
        task = document.get("task") or {}
        task_id = str(task["id"])

        change = await client.post(
            SEND_URL,
            json=_plain_body(
                "Modify: lower the guaranteed throughput to 10 Mbps from 00:00 to 06:00.",
                task_id=task_id,
            ),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert change.status_code == 200, change.text
    assert change.json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert _reason(change) == REASON_MISSING
