"""v2 prototype, pillar B: the agreement is a durable, revisable object.

v1 keeps the accepted plan inside one Task. Here the agreement has an id that survives revisions and
a revision counter, so a change order can say "target revision 1" and the server can answer
"revision 1 is no longer current" instead of comparing two digests with no shared name.
"""

from __future__ import annotations

import httpx
import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo import ran_data
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import (
    BASELINE_REQUIRED,
    CARRIER_MESSAGE,
    FIDELITY_FAST,
    PURE_QUESTION_ISSUE,
    WINDOW_POLICY_ANY,
    SimConfig,
)
from shared.agreement import (
    AGREEMENT_METADATA_KEY,
    REASON_STALE,
    REASON_UNKNOWN,
    Agreement,
    next_revision,
    read_agreement,
)
from shared.approval import plan_digest
from shared.change_order import BASELINE_ERROR_METADATA_KEY, BASELINE_METADATA_KEY

TOKEN_URL = "/rest/plat/smapp/v1/sessions"
SEND_URL = "/a2a/json/message:send"
PLAN = {"operation": "Create", "region": "A2ATestRegion", "window": "00:00-06:00", "rateMbps": 30.0}


@pytest.fixture(autouse=True)
def _reset_plans() -> None:
    ran_data.reset()


def _config(**overrides: object) -> SimConfig:
    fields: dict[str, object] = {
        "fidelity": FIDELITY_FAST,
        "negotiation_carrier": CARRIER_MESSAGE,
        "pure_question": PURE_QUESTION_ISSUE,
        "window_policy": WINDOW_POLICY_ANY,
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


def _body(
    prompt: str,
    *,
    task_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"text": "run"}],
        "metadata": {TASK_T_EXTENSION_URI: prompt, **(metadata or {})},
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


def _agreement_of(document: dict[str, object]) -> dict[str, object]:
    task = document.get("task") if isinstance(document.get("task"), dict) else document
    metadata = task.get("metadata") if isinstance(task, dict) else None
    raw = metadata.get(AGREEMENT_METADATA_KEY) if isinstance(metadata, dict) else None
    assert isinstance(raw, dict), document
    return raw


# ---------------------------------------------------------------- object semantics


def test_a_new_agreement_starts_at_revision_one() -> None:
    agreement = next_revision(None, plan=PLAN)

    assert agreement.revision == 1
    assert agreement.agreement_id.startswith("agr-")
    assert agreement.plan_digest == plan_digest(PLAN)


def test_re_accepting_the_same_plan_does_not_create_a_revision() -> None:
    first = next_revision(None, plan=PLAN)

    again = next_revision(first, plan=dict(reversed(list(PLAN.items()))))

    assert again == first


def test_a_different_plan_bumps_the_revision_and_keeps_the_id() -> None:
    first = next_revision(None, plan=PLAN)

    changed = next_revision(first, plan={**PLAN, "rateMbps": 10.0})

    assert changed.agreement_id == first.agreement_id
    assert changed.revision == 2


def test_the_revision_round_trips_through_metadata() -> None:
    agreement = next_revision(None, plan=PLAN, scopes=({"window": "00:00-06:00", "rateMbps": 30},))

    assert read_agreement(agreement.to_metadata()) == agreement


# ---------------------------------------------------------------- end to end


async def test_an_accepted_task_carries_its_agreement_revision() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        listed = await client.get("/a2a/json/agreements", headers={"Authorization": f"Bearer {token}"})

    agreement = _agreement_of(created.json())
    assert agreement["revision"] == 1
    assert agreement["planDigest"] == plan_digest(PLAN)
    assert listed.json()["agreements"][0]["agreementId"] == agreement["agreementId"]


async def test_a_change_order_bumps_the_revision() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        task_id = str(created.json()["task"]["id"])
        changed = await client.post(
            SEND_URL,
            json=_body(
                _prompt(operation="Modify", rate="10 Mbps"),
                task_id=task_id,
                metadata={
                    BASELINE_METADATA_KEY: {
                        "version": 1,
                        "planDigest": agreement["planDigest"],
                        "agreementId": agreement["agreementId"],
                        "revision": 1,
                    }
                },
            ),
            headers=_headers(token),
        )

    after = _agreement_of(changed.json())
    assert after["agreementId"] == agreement["agreementId"]
    assert after["revision"] == 2


async def test_a_change_order_against_a_superseded_revision_is_refused() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        task_id = str(created.json()["task"]["id"])
        refused = await client.post(
            SEND_URL,
            json=_body(
                _prompt(operation="Modify", rate="10 Mbps"),
                task_id=task_id,
                metadata={
                    BASELINE_METADATA_KEY: {
                        "version": 1,
                        "planDigest": agreement["planDigest"],
                        "agreementId": agreement["agreementId"],
                        "revision": 7,
                    }
                },
            ),
            headers=_headers(token),
        )

    message = ((refused.json().get("task") or {}).get("status") or {}).get("message") or {}
    reason = (message.get("metadata") or {}).get(BASELINE_ERROR_METADATA_KEY)
    assert reason == {"reasonCode": REASON_STALE}


async def test_a_change_order_naming_another_agreement_is_refused() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        task_id = str(created.json()["task"]["id"])
        refused = await client.post(
            SEND_URL,
            json=_body(
                _prompt(operation="Modify", rate="10 Mbps"),
                task_id=task_id,
                metadata={
                    BASELINE_METADATA_KEY: {
                        "version": 1,
                        "planDigest": agreement["planDigest"],
                        "agreementId": "agr-somebody-else",
                        "revision": 1,
                    }
                },
            ),
            headers=_headers(token),
        )

    message = ((refused.json().get("task") or {}).get("status") or {}).get("message") or {}
    assert (message.get("metadata") or {}).get(BASELINE_ERROR_METADATA_KEY) == {
        "reasonCode": REASON_UNKNOWN
    }


def test_an_agreement_from_metadata_is_read_only_when_it_is_well_formed() -> None:
    assert read_agreement({AGREEMENT_METADATA_KEY: {"version": 1}}) is None
    assert read_agreement({AGREEMENT_METADATA_KEY: Agreement("a", 1, "d").to_dict()}) == Agreement(
        "a", 1, "d"
    )
