"""Tests of the simulator's S6 approval gate: park, decide, refuse a stale decision."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest
from a2a_t.core.metadata import NEGOTIATION_T_EXTENSION_URI, TASK_T_EXTENSION_URI
from demo import ran_data
from demo.approval_answer import approval_answer
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import (
    CARRIER_MESSAGE,
    FIDELITY_FAST,
    PURE_QUESTION_ISSUE,
    SimConfig,
)
from protocol.nms_client import NmsClient, NmsConfig, NmsSseTransport
from protocol.pure_client_flow import run_pure_client_flow
from shared.approval import (
    APPROVAL_METADATA_KEY,
    OUTCOME_APPROVE,
    OUTCOME_REJECT,
    REASON_OPERATOR_DECLINED,
    VERIFY_STALE,
    read_approval_request,
)
from shared.run_constants import FINAL_STATE_TASK_COMPLETED

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
        "approval": True,
    }
    fields.update(overrides)
    return SimConfig(**fields)  # ty: ignore[invalid-argument-type]


async def _client(config: SimConfig | None = None) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_sim_app(config or _config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        yield client


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL, json={"grantType": "password", "userName": USER, "value": VALUE}
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _prompt(*, rate: str = "30 Mbps") -> str:
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
            f"Energy saving time: 00:00~06:00; guaranteed throughput: {rate}.",
        ]
    )


def _request_body(
    prompt: str | None, *, text: str, task_id: str | None = None
) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if prompt is not None:
        message["metadata"] = {TASK_T_EXTENSION_URI: prompt}
    if task_id is not None:
        message["taskId"] = task_id
    return {"message": message}


def _decision_body(
    decision: dict[str, object],
    *,
    task_id: str,
    context_id: str,
) -> dict[str, object]:
    return {
        "message": {
            "messageId": "m-2",
            "role": "ROLE_USER",
            "parts": [{"text": "approval decision"}],
            "metadata": {APPROVAL_METADATA_KEY: decision},
            "taskId": task_id,
            "contextId": context_id,
        }
    }


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "A2A-Extensions": f"{TASK_T_EXTENSION_URI},{NEGOTIATION_T_EXTENSION_URI}",
    }


def _task(response: httpx.Response) -> dict[str, object]:
    document = response.json()
    task = document.get("task")
    assert isinstance(task, dict), document
    return task


def _status(task: Mapping[str, object]) -> Mapping[str, object]:
    status = task.get("status")
    assert isinstance(status, Mapping)
    return status


def _metadata(task: Mapping[str, object]) -> Mapping[str, object]:
    message = _status(task).get("message")
    if not isinstance(message, Mapping):
        return {}
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


async def _accept_and_wait(client: httpx.AsyncClient, token: str) -> tuple[dict[str, object], dict[str, object]]:
    """Submit one acceptable request and return the parked Task plus its approval request."""
    response = await client.post(
        SEND_URL,
        json=_request_body(_prompt(), text="create ran energy saving task"),
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    task = _task(response)
    assert _status(task)["state"] == "TASK_STATE_INPUT_REQUIRED"
    raw = _metadata(task).get(APPROVAL_METADATA_KEY)
    assert isinstance(raw, dict)
    request = read_approval_request({APPROVAL_METADATA_KEY: raw})
    assert request is not None
    return task, raw


async def _wait_task(
    client: httpx.AsyncClient, token: str, task_id: str, *, timeout: float = 2.0
) -> dict[str, object]:
    """Poll one Task until it leaves the non-terminal states (the JSON route completes async)."""
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = await client.get(
            f"/a2a/json/tasks/{task_id}", headers={"X-Auth-Token": token}
        )
        assert response.status_code == 200, response.text
        document = response.json()
        task = document.get("task")
        assert isinstance(task, dict)
        last = task
        if _status(task)["state"] in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_REJECTED",
            "TASK_STATE_FAILED",
        }:
            return task
        await asyncio.sleep(0.01)
    return last


def _approve(raw: dict[str, object], **overrides: object) -> dict[str, object]:
    decision: dict[str, object] = {
        "version": 1,
        "kind": "approval-decision",
        "approvalId": raw["approvalId"],
        "planDigest": raw["planDigest"],
        "outcome": OUTCOME_APPROVE,
        "reasonCode": "operator_accepted",
        "reason": "",
        "decidedBy": "engineer:test",
    }
    decision.update(overrides)
    return decision


async def test_an_accepted_plan_waits_for_approval_then_runs() -> None:
    """T1: the plan is parked with a request, and the matching decision executes it."""
    async for client in _client():
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)
        assert _metadata(task)["a2at-sample/approval"] == raw

        response = await client.post(
            SEND_URL,
            json=_decision_body(
                _approve(raw),
                task_id=str(task["id"]),
                context_id=str(task["contextId"]),
            ),
            headers=_headers(token),
        )

        assert response.status_code == 200, response.text
        completed = await _wait_task(client, token, str(task["id"]))
        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"
        assert completed["artifacts"]


async def test_a_rejection_is_terminal_and_carries_its_reason() -> None:
    """T2: a rejection stops the Task and keeps the reason code machine-readable."""
    async for client in _client():
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)
        decision = _approve(
            raw,
            outcome=OUTCOME_REJECT,
            reasonCode=REASON_OPERATOR_DECLINED,
            reason="The outage window is not acceptable.",
        )

        response = await client.post(
            SEND_URL,
            json=_decision_body(
                decision, task_id=str(task["id"]), context_id=str(task["contextId"])
            ),
            headers=_headers(token),
        )

        rejected = _task(response)
        assert _status(rejected)["state"] == "TASK_STATE_REJECTED"
        stored = _metadata(rejected).get(APPROVAL_METADATA_KEY)
        assert isinstance(stored, Mapping)
        assert stored["reasonCode"] == REASON_OPERATOR_DECLINED
        assert stored["reason"] == "The outage window is not acceptable."
        assert not rejected["artifacts"]


async def test_an_approval_for_a_different_plan_is_refused() -> None:
    """T3: identity alone is not enough; the digest must match what is being executed."""
    async for client in _client():
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)

        response = await client.post(
            SEND_URL,
            json=_decision_body(
                _approve(raw, planDigest="sha256:0000"),
                task_id=str(task["id"]),
                context_id=str(task["contextId"]),
            ),
            headers=_headers(token),
        )

        waiting = _task(response)
        assert _status(waiting)["state"] == "TASK_STATE_INPUT_REQUIRED"
        metadata = _metadata(waiting)
        assert metadata["a2at-sample/approval-error"] == VERIFY_STALE
        assert APPROVAL_METADATA_KEY in metadata
        assert not waiting["artifacts"]


async def test_an_expired_request_can_no_longer_be_approved() -> None:
    """T4: expiry is decided by the server clock, not the approver's."""
    async for client in _client(_config(approval_ttl_seconds=0.0)):
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)

        response = await client.post(
            SEND_URL,
            json=_decision_body(
                _approve(raw), task_id=str(task["id"]), context_id=str(task["contextId"])
            ),
            headers=_headers(token),
        )

        expired = _task(response)
        assert _status(expired)["state"] == "TASK_STATE_REJECTED"
        stored = _metadata(expired).get(APPROVAL_METADATA_KEY)
        assert isinstance(stored, Mapping)
        assert stored["reasonCode"] == "expired"


async def test_a_repeated_decision_runs_the_plan_once() -> None:
    """T5: a retried decision is idempotent; the plan is not applied twice."""
    async for client in _client():
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)
        body = _decision_body(
            _approve(raw), task_id=str(task["id"]), context_id=str(task["contextId"])
        )

        first = await client.post(SEND_URL, json=body, headers=_headers(token))
        assert first.status_code == 200
        completed = await _wait_task(client, token, str(task["id"]))
        second = await client.post(SEND_URL, json=body, headers=_headers(token))

        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"
        repeated = _task(second)
        assert _status(repeated)["state"] == "TASK_STATE_COMPLETED"
        repeated_artifacts = repeated["artifacts"]
        completed_artifacts = completed["artifacts"]
        assert isinstance(repeated_artifacts, list) and isinstance(completed_artifacts, list)
        assert len(repeated_artifacts) == len(completed_artifacts) == 1


async def test_a_client_that_only_echoes_the_digest_accepts_a_tampered_request() -> None:
    """The plain-A2A failure mode: the digest is a server claim, so echoing it proves nothing."""
    async for client in _client(_config(approval_tamper=True)):
        token = await _token(client)
        task, raw = await _accept_and_wait(client, token)

        response = await client.post(
            SEND_URL,
            json=_decision_body(
                _approve(raw), task_id=str(task["id"]), context_id=str(task["contextId"])
            ),
            headers=_headers(token),
        )

        assert response.status_code == 200, response.text
        completed = await _wait_task(client, token, str(task["id"]))
        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"


async def test_a_restarted_client_can_still_answer_from_the_task_state() -> None:
    """S7 at the contract level: the pending approval lives on the Task, not in the client.

    A fresh client has no memory of the request; it reads the Task, finds the request object and
    answers it. Anything held only in the first client's process would be lost here.
    """
    # One simulator, two client instances: the store must survive the first client's lifetime.
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        task, _raw = await _accept_and_wait(client, token)
        task_id = str(task["id"])
        context_id = str(task["contextId"])

    # A new client instance: no state carried over, only the Task id.
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as restarted:
        token = await _token(restarted)
        fetched = await restarted.get(
            f"/a2a/json/tasks/{task_id}", headers={"X-Auth-Token": token}
        )
        stored = fetched.json()["task"]
        raw = _metadata(stored).get(APPROVAL_METADATA_KEY)
        assert isinstance(raw, dict)

        response = await restarted.post(
            SEND_URL,
            json=_decision_body(_approve(raw), task_id=task_id, context_id=context_id),
            headers=_headers(token),
        )
        assert response.status_code == 200

        completed = await _wait_task(restarted, token, task_id)
        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"


async def test_without_the_switch_an_accepted_plan_runs_immediately() -> None:
    """T6: an unaware peer keeps the old behaviour; the gate is opt-in."""
    async for client in _client(_config(approval=False)):
        token = await _token(client)
        response = await client.post(
            SEND_URL,
            json=_request_body(_prompt(), text="create ran energy saving task"),
            headers=_headers(token),
        )

        task = _task(response)
        completed = await _wait_task(client, token, str(task["id"]))
        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"


async def test_a_plain_a2a_plan_waits_for_the_same_object() -> None:
    """The gate is not an A2A-T feature: a plain message parks and resumes the same way."""
    async for client in _client(
        _config(pure_question=PURE_QUESTION_ISSUE, rate_policy="optional")
    ):
        token = await _token(client)
        plain = "Implement energy saving in A2ATestRegion, guaranteed throughput 5 Mbps from 00:00 to 06:00."
        response = await client.post(
            SEND_URL,
            json=_request_body(None, text=plain),
            headers={"Authorization": f"Bearer {token}"},
        )
        task = _task(response)
        assert _status(task)["state"] == "TASK_STATE_INPUT_REQUIRED"
        raw = _metadata(task).get(APPROVAL_METADATA_KEY)
        assert isinstance(raw, dict)

        await client.post(
            SEND_URL,
            json=_decision_body(
                _approve(raw), task_id=str(task["id"]), context_id=str(task["contextId"])
            ),
            headers={"Authorization": f"Bearer {token}"},
        )

        completed = await _wait_task(client, token, str(task["id"]))
        assert _status(completed)["state"] == "TASK_STATE_COMPLETED"


async def _run_pure_client(*, tamper: bool, deviation: bool = False) -> object:
    """Drive the pure client against the in-process simulator and return its result."""
    config = _config(
        pure_question=PURE_QUESTION_ISSUE,
        rate_policy="optional",
        approval_tamper=tamper,
        readback_deviation=deviation,
    )
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_sim_app(config)), base_url="http://sim"
    )
    nms = NmsClient(
        NmsConfig(
            a2a_base="http://sim/a2a/json",
            token_url=TOKEN_URL,
            token_user=USER,
            token_value=VALUE,
            idle_timeout=1.0,
            request_timeout=5.0,
            poll_interval=0.0,
        ),
        http_client=http_client,
    )
    transport = NmsSseTransport(nms, protocol="a2a")
    plain = "Implement energy saving in A2ATestRegion, guaranteed throughput 5 Mbps from 00:00 to 06:00."
    try:
        return await run_pure_client_flow(
            transport=transport,
            user_request=plain,
            approval_answer=approval_answer(OUTCOME_APPROVE),
            max_rounds=3,
        )
    finally:
        await http_client.aclose()


async def test_the_client_answers_the_request_and_the_task_completes() -> None:
    """End to end: the pure client sees the request, answers it, and the Task finishes."""
    result = await _run_pure_client(tamper=False)

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert any(_is_approval_event(event) for event in result.events)


async def test_a_plain_client_executes_a_misstated_plan() -> None:
    """The plain hop has no accepted document, so it can only echo the server's digest.

    With the tamper switch the request names a plan the server will not execute; the client echoes
    it, the server compares the decision with its own claim, and the Task completes. Nothing in the
    exchange ever compared the approval with what was actually agreed.
    """
    result = await _run_pure_client(tamper=True)

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert any(_is_approval_event(event) for event in result.events)


async def test_the_plain_client_reports_the_readback_as_unverified() -> None:
    """S8 on the plain hop: the object arrives, but nothing can vouch for it."""
    result = await _run_pure_client(tamper=False)

    assert result.readback is not None
    assert result.readback["status"] == "unverified"
    assert result.readback["deviations"] == []


async def test_the_plain_client_cannot_tell_a_deviation_from_a_match() -> None:
    """The deviation is visible in the payload, but the client cannot verify it happened."""
    result = await _run_pure_client(tamper=False, deviation=True)

    assert result.readback is not None
    assert result.readback["status"] == "unverified"
    assert result.readback["deviations"]


def _is_approval_event(event: Mapping[str, object]) -> bool:
    """Return whether one normalized event is an approval request."""
    metadata = event.get("metadata")
    return (
        event.get("state") == "TASK_STATE_INPUT_REQUIRED"
        and isinstance(metadata, Mapping)
        and APPROVAL_METADATA_KEY in metadata
    )
