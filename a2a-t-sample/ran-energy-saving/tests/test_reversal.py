"""The reversal contract: every accepted plan names the state it replaces.

A contract that can only move forward is an instruction. An abort has to be able to put the
network back to something a message already recorded, or the protocol cannot show that the network
is not still in the abandoned configuration. The simulator executes that reversal on cancel and
publishes it in the same readback envelope a client already reads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

import httpx
import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import FIDELITY_FAST, WINDOW_POLICY_ANY, SimConfig
from demo.nms_sim.tasks import TASK_CANCELED, TASK_COMPLETED, TASK_INPUT_REQUIRED, TASK_WORKING
from shared.agreement import AGREEMENT_METADATA_KEY, next_revision, read_agreement
from shared.approval import plan_digest
from shared.readback import Readback, compare_plans, read_readback
from shared.reversal import (
    OPERATION_RESTORE,
    REVERSAL_METADATA_KEY,
    Reversal,
    read_reversal,
    restoration_for,
)

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
PLAN = {"operation": "Create", "region": "A2ATestRegion", "window": "00:00-06:00", "rateMbps": 30.0}
BASELINE_RATE = 100.0


@pytest.fixture(autouse=True)
def _reset_plans() -> None:
    ran_data.reset()


# ---------------------------------------------------------------- building a reversal


def test_a_previous_plan_is_restated_as_the_restore_plan() -> None:
    previous = {"operation": "Create", "region": "A2ATestRegion", "window": "00:00-02:00", "rateMbps": 12.0}
    plan = {"operation": "Modify", "region": "A2ATestRegion", "window": "00:00-04:00", "rateMbps": 30.0}

    reversal = restoration_for(plan, previous, baseline_rate=BASELINE_RATE)

    assert reversal.plan == {
        "operation": OPERATION_RESTORE,
        "region": "A2ATestRegion",
        "window": "00:00-02:00",
        "rateMbps": 12.0,
    }


def test_without_a_previous_plan_the_window_returns_to_the_baseline_rate() -> None:
    plan = {"operation": "Create", "region": "OtherRegion", "window": "01:00-05:00", "rateMbps": 30.0}

    reversal = restoration_for(plan, None, baseline_rate=BASELINE_RATE)

    assert reversal.plan == {
        "operation": OPERATION_RESTORE,
        "region": "OtherRegion",
        "window": "01:00-05:00",
        "rateMbps": BASELINE_RATE,
    }


def test_missing_keys_never_raise() -> None:
    reversal = restoration_for({}, {}, baseline_rate=7.5)

    assert reversal.plan == {
        "operation": OPERATION_RESTORE,
        "region": "",
        "window": "",
        "rateMbps": 7.5,
    }


def test_a_missing_previous_key_falls_back_to_the_new_plans_value() -> None:
    plan = {"operation": "Modify", "region": "A2ATestRegion", "window": "00:00-04:00", "rateMbps": 5.0}

    reversal = restoration_for(plan, {"region": "A2ATestRegion"}, baseline_rate=9.0)

    assert reversal.plan["window"] == "00:00-04:00"
    assert reversal.plan["rateMbps"] == 9.0


# ---------------------------------------------------------------- reading a reversal


def test_a_reversal_round_trips_through_metadata() -> None:
    reversal = Reversal(plan=dict(PLAN), reason="operator abort")

    assert read_reversal(reversal.to_metadata()) == reversal


def test_a_reversal_without_a_reason_omits_it() -> None:
    payload = Reversal(plan=dict(PLAN)).to_dict()

    assert "reason" not in payload
    assert read_reversal({REVERSAL_METADATA_KEY: payload}) == Reversal(plan=dict(PLAN))


def test_malformed_reversals_degrade_to_absent() -> None:
    assert read_reversal(None) is None
    assert read_reversal({REVERSAL_METADATA_KEY: "nope"}) is None
    assert read_reversal({REVERSAL_METADATA_KEY: {"version": 9, "plan": dict(PLAN)}}) is None
    assert read_reversal({REVERSAL_METADATA_KEY: {"version": 1}}) is None
    assert read_reversal({REVERSAL_METADATA_KEY: {"version": "not-a-number"}}) is None


def test_the_restore_rate_defaults_and_rejects_a_negative_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2AT_SAMPLE_NMS_SIM_RESTORE_RATE_MBPS", raising=False)
    assert SimConfig.from_env().restore_rate_mbps == BASELINE_RATE

    monkeypatch.setenv("A2AT_SAMPLE_NMS_SIM_RESTORE_RATE_MBPS", "-1")
    with pytest.raises(ValueError):
        SimConfig.from_env()


# ---------------------------------------------------------------- carrying it on the agreement


def test_next_revision_stores_the_reversal() -> None:
    reversal = restoration_for(PLAN, None, baseline_rate=BASELINE_RATE)

    agreement = next_revision(None, plan=PLAN, reversal=reversal.to_dict())

    assert agreement.reversal == reversal.to_dict()
    reread = read_agreement(agreement.to_metadata())
    assert reread is not None
    assert reread.reversal == reversal.to_dict()


def test_an_idempotent_restatement_keeps_the_original_reversal() -> None:
    original = restoration_for(PLAN, None, baseline_rate=BASELINE_RATE)
    first = next_revision(None, plan=PLAN, reversal=original.to_dict())
    other = Reversal(plan={"operation": OPERATION_RESTORE, "rateMbps": 1.0})

    again = next_revision(first, plan=dict(PLAN), reversal=other.to_dict())

    assert again == first
    assert again.reversal == original.to_dict()


# ---------------------------------------------------------------- end to end in the simulator


def _config(**overrides: object) -> SimConfig:
    fields: dict[str, object] = {
        "fidelity": FIDELITY_FAST,
        "window_policy": WINDOW_POLICY_ANY,
        "hold_working": True,
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


def _body(prompt: str, *, task_id: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"text": "run"}],
        "metadata": {TASK_T_EXTENSION_URI: prompt},
    }
    if task_id is not None:
        message["taskId"] = task_id
    return {"message": message}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "A2A-Extensions": TASK_T_EXTENSION_URI}


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        TOKEN_URL,
        json={"grantType": "password", "userName": "snbitestDefaultNorth", "value": "Changeme_123"},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


def _task_of(document: Mapping[str, object]) -> dict[str, object]:
    task = document.get("task")
    return dict(task) if isinstance(task, Mapping) else dict(document)


def _agreement_of(document: Mapping[str, object]) -> dict[str, object]:
    metadata = document.get("metadata")
    raw = metadata.get(AGREEMENT_METADATA_KEY) if isinstance(metadata, Mapping) else None
    assert isinstance(raw, dict), document
    return raw


def _state_of(task: Mapping[str, object]) -> object:
    status = task.get("status")
    return status.get("state") if isinstance(status, Mapping) else None


def _artifacts_of(task: Mapping[str, object]) -> list[object]:
    artifacts = task.get("artifacts")
    return list(artifacts) if isinstance(artifacts, list) else []


def _artifact_with(task: Mapping[str, object], artifact_id: str) -> dict[str, object] | None:
    for artifact in _artifacts_of(task):
        if isinstance(artifact, Mapping) and artifact.get("artifactId") == artifact_id:
            return dict(artifact)
    return None


def _readback_of(artifact: Mapping[str, object]) -> Readback | None:
    metadata = artifact.get("metadata")
    return read_readback(metadata if isinstance(metadata, Mapping) else None)


async def _cancel(
    client: httpx.AsyncClient,
    token: str,
    task_id: str,
    *,
    body: dict[str, object] | None = None,
) -> httpx.Response:
    url = f"/a2a/json/tasks/{task_id}:cancel"
    if body is None:
        return await client.post(url, headers=_headers(token))
    return await client.post(url, headers=_headers(token), json=body)


async def _await_state(
    client: httpx.AsyncClient, token: str, task_id: str, state: str, timeout: float = 5.0
) -> dict[str, object]:
    """Poll until the Task reaches one state; a background execution needs a moment to settle."""
    deadline = time.monotonic() + timeout
    document: dict[str, object] = {}
    while time.monotonic() < deadline:
        fetched = await client.get(f"/a2a/json/tasks/{task_id}", headers=_headers(token))
        document = fetched.json()["task"]
        status = document.get("status")
        if isinstance(status, Mapping) and status.get("state") == state:
            return document
        await asyncio.sleep(0.01)
    return document


async def test_the_reversal_is_visible_on_the_task_document_under_the_agreement() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        fetched = await client.get(f"/a2a/json/tasks/{task_id}", headers=_headers(token))

    for document in (created.json(), fetched.json()):
        agreement = _agreement_of(_task_of(document))
        reversal = agreement.get("reversal")
        assert isinstance(reversal, Mapping), agreement
        assert reversal.get("plan") == {
            "operation": OPERATION_RESTORE,
            "region": "A2ATestRegion",
            "window": "00:00-06:00",
            "rateMbps": BASELINE_RATE,
        }
        assert REVERSAL_METADATA_KEY not in document


async def test_aborting_a_task_cancels_it_and_publishes_the_restoration() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        await _await_state(client, token, task_id, TASK_WORKING)
        cancelled = await _cancel(client, token, task_id)

    task = _task_of(cancelled.json())
    assert _state_of(task) == TASK_CANCELED
    artifact = _artifact_with(task, f"{task_id}-restoration")
    assert artifact is not None
    readback = _readback_of(artifact)
    assert readback is not None
    baseline_plan = {
        "operation": OPERATION_RESTORE,
        "region": "A2ATestRegion",
        "window": "00:00-06:00",
        "rateMbps": BASELINE_RATE,
    }
    assert readback.executed == baseline_plan
    assert readback.plan_digest == plan_digest(baseline_plan)
    assert readback.restored_from == plan_digest(PLAN)
    assert readback.deviations == compare_plans(PLAN, baseline_plan)


async def test_a_second_cancel_does_not_restore_twice() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        await _await_state(client, token, task_id, TASK_WORKING)
        first = _task_of((await _cancel(client, token, task_id)).json())
        second = _task_of((await _cancel(client, token, task_id)).json())

    artifact_id = f"{task_id}-restoration"
    assert len(_artifacts_of(first)) == len(_artifacts_of(second))
    assert sum(
        1
        for artifact in _artifacts_of(second)
        if isinstance(artifact, Mapping) and artifact.get("artifactId") == artifact_id
    ) == 1


async def test_aborting_a_change_order_restores_the_plan_it_replaced() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        await _await_state(client, token, task_id, TASK_WORKING)
        changed = await client.post(
            SEND_URL,
            json=_body(_prompt(operation="Modify", rate="10 Mbps"), task_id=task_id),
            headers=_headers(token),
        )
        changed_task = _task_of(changed.json())
        await _await_state(client, token, task_id, TASK_COMPLETED)
        cancelled = await _cancel(client, token, task_id)

    changed_plan = {**PLAN, "operation": "Modify", "rateMbps": 10.0}
    previous_plan = {**PLAN, "operation": OPERATION_RESTORE, "rateMbps": 30.0}
    assert _agreement_of(changed_task)["revision"] == 2
    task = _task_of(cancelled.json())
    artifact = _artifact_with(task, f"{task_id}-restoration")
    assert artifact is not None
    readback = _readback_of(artifact)
    assert readback is not None
    assert readback.executed == previous_plan
    assert readback.plan_digest == plan_digest(previous_plan)
    assert readback.restored_from == plan_digest(changed_plan)


async def test_a_task_created_with_no_predecessor_restores_the_configured_baseline_rate() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config(restore_rate_mbps=77.0)))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        await _await_state(client, token, task_id, TASK_WORKING)
        cancelled = await _cancel(client, token, task_id)

    artifact = _artifact_with(_task_of(cancelled.json()), f"{task_id}-restoration")
    assert artifact is not None
    readback = _readback_of(artifact)
    assert readback is not None
    assert readback.executed["rateMbps"] == 77.0


async def test_a_cancel_with_another_reason_does_not_restore() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        task_id = str(_task_of(created.json())["id"])
        await _await_state(client, token, task_id, TASK_WORKING)
        cancelled = await _cancel(client, token, task_id, body={"reason": "operator"})

    task = _task_of(cancelled.json())
    assert _state_of(task) == TASK_CANCELED
    assert _artifact_with(task, f"{task_id}-restoration") is None


async def test_a_task_without_an_agreement_cancels_without_a_restoration() -> None:
    """A plain-A2A question never accepted a plan, so there is nothing to restore."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        asked = await client.post(
            SEND_URL,
            json={
                "message": {
                    "messageId": "m-plain",
                    "role": "ROLE_USER",
                    "parts": [{"text": "please save energy"}],
                }
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        listed = await client.get("/a2a/json/tasks", headers={"Authorization": f"Bearer {token}"})
        task = listed.json()["tasks"][0]
        task_id = str(task["id"])
        assert task["status"]["state"] == TASK_INPUT_REQUIRED
        cancelled = await _cancel(client, token, task_id)

    assert asked.status_code == 200
    result = _task_of(cancelled.json())
    assert _state_of(result) == TASK_CANCELED
    assert _artifacts_of(result) == []
