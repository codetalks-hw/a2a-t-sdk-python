"""v2 prototype, pillar B: the agreement is a durable, revisable object.

v1 keeps the accepted plan inside one Task. Here the agreement has an id that survives revisions and
a revision counter, so a change order can say "target revision 1" and the server can answer
"revision 1 is no longer current" instead of comparing two digests with no shared name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def _reference_body(
    agreement: dict[str, object], *, request_id: str, statement: dict[str, object] | None = None
) -> dict[str, object]:
    metadata: dict[str, object] = {
        AGREEMENT_METADATA_KEY: {
            "agreementId": agreement["agreementId"],
            "revision": agreement["revision"],
        },
        "a2at-sample/request-id": request_id,
        **(statement or {}),
    }
    return {
        "message": {
            "messageId": request_id,
            "role": "ROLE_USER",
            "parts": [{"text": "execute the agreement"}],
            "metadata": metadata,
        }
    }


async def test_a_stored_agreement_can_be_executed_again_without_resending_it() -> None:
    """The agreement is the durable object: a second execution names it instead of re-describing it."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        again = await client.post(
            SEND_URL,
            json=_reference_body(agreement, request_id="run-2"),
            headers=_headers(token),
        )
        metrics = await client.get("/a2a/json/metrics", headers={"Authorization": f"Bearer {token}"})

    assert again.status_code == 200, again.text
    assert again.json()["task"]["id"] != created.json()["task"]["id"]
    assert _agreement_of(again.json())["planDigest"] == agreement["planDigest"]
    assert metrics.json()["protocols"]["a2a-t"]["paths"].get("agreement") == 1
    assert metrics.json()["protocols"]["a2a-t"]["duplicates"] == 0


async def test_the_same_agreement_can_run_twice_with_explicit_request_identities() -> None:
    """A second execution is a new request, not a retry: the client says so with its own id."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        first = await client.post(
            SEND_URL, json=_reference_body(agreement, request_id="run-a"), headers=_headers(token)
        )
        second = await client.post(
            SEND_URL, json=_reference_body(agreement, request_id="run-b"), headers=_headers(token)
        )

    assert first.json()["task"]["id"] != second.json()["task"]["id"]


async def test_an_expired_agreement_is_refused() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config(agreement_ttl_seconds=0.0)))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        refused = await client.post(
            SEND_URL, json=_reference_body(agreement, request_id="later"), headers=_headers(token)
        )

    assert refused.status_code == 409
    assert "agreement-expired" in refused.text


async def test_a_statement_that_disagrees_with_the_agreement_is_refused() -> None:
    """Naming a revision and describing another plan must not silently execute the description."""
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        refused = await client.post(
            SEND_URL,
            json=_reference_body(
                agreement, request_id="run-3", statement={TASK_T_EXTENSION_URI: _prompt(rate="5 Mbps")}
            ),
            headers=_headers(token),
        )

    assert refused.status_code == 409
    assert "agreement-mismatch" in refused.text


async def test_an_unknown_agreement_is_refused() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        refused = await client.post(
            SEND_URL,
            json=_reference_body({"agreementId": "agr-missing", "revision": 1}, request_id="run-x"),
            headers=_headers(token),
        )

    assert refused.status_code == 409
    assert "agreement-unknown" in refused.text


# ---------------------------------------------------------------- reading malformed data


def test_a_scopes_field_that_is_not_a_list_is_malformed() -> None:
    raw = Agreement("a", 1, "d").to_dict()
    raw["scopes"] = "not-a-list"

    assert read_agreement({AGREEMENT_METADATA_KEY: raw}) is None


def test_a_non_mapping_scope_entry_is_skipped_instead_of_raising() -> None:
    raw = Agreement("a", 1, "d").to_dict()
    raw["scopes"] = [{"window": "00:00-06:00", "rateMbps": 30}, "garbage", None]

    agreement = read_agreement({AGREEMENT_METADATA_KEY: raw})

    assert agreement is not None
    assert agreement.scopes == ({"window": "00:00-06:00", "rateMbps": 30},)


# ---------------------------------------------------------------- expiry predicate


def test_an_agreement_without_an_expiry_never_expires() -> None:
    assert Agreement("a", 1, "d").is_expired() is False


def test_an_agreement_past_its_expiry_expires() -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    assert Agreement("a", 1, "d", expires_at=past).is_expired() is True


def test_an_agreement_before_its_expiry_does_not_expire() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    assert Agreement("a", 1, "d", expires_at=future).is_expired() is False


def test_a_naive_expiry_is_read_as_this_hosts_clock() -> None:
    """A naive instant means the writer's clock, so both directions must follow this host.

    The past case is the one that decides it: read as UTC instead of host-local, a fresh-looking
    value still lands in the future on any positive-offset host, so only the expired side can tell
    the two readings apart.
    """
    moment = datetime.now(UTC)
    future = (moment.astimezone() + timedelta(hours=1)).replace(tzinfo=None)
    past = (moment.astimezone() - timedelta(hours=1)).replace(tzinfo=None)

    assert Agreement("a", 1, "d", expires_at=future.isoformat()).is_expired(moment) is False
    assert Agreement("a", 1, "d", expires_at=past.isoformat()).is_expired(moment) is True


def test_an_unparsable_expiry_is_not_expired_and_never_raises() -> None:
    assert Agreement("a", 1, "d", expires_at="not-a-time").is_expired() is False


def test_expiry_is_measured_against_the_supplied_instant() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    agreement = Agreement("a", 1, "d", expires_at=moment.isoformat())

    assert agreement.is_expired(moment - timedelta(seconds=1)) is False
    assert agreement.is_expired(moment) is True


# ---------------------------------------------------------------- agreement as a resource


async def test_every_revision_is_readable_newest_first() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        created = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(created.json())
        task_id = str(created.json()["task"]["id"])
        await client.post(
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
        fetched = await client.get(
            f"/a2a/json/agreements/{agreement['agreementId']}",
            headers={"Authorization": f"Bearer {token}"},
        )

    document = fetched.json()
    assert fetched.status_code == 200
    assert document["agreementId"] == agreement["agreementId"]
    assert document["currentRevision"] == 2
    assert [item["revision"] for item in document["revisions"]] == [2, 1]
    assert document["revisions"][0]["planDigest"] != document["revisions"][1]["planDigest"]


async def test_an_unknown_agreement_document_is_a_typed_404() -> None:
    transport = httpx.ASGITransport(app=build_sim_app(_config()))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        fetched = await client.get(
            "/a2a/json/agreements/agr-missing",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert fetched.status_code == 404
    assert fetched.json() == {"reasonCode": REASON_UNKNOWN}


async def test_the_listing_survives_eviction_of_the_task_that_produced_the_agreement() -> None:
    """The agreement decides executability, so it must not be read through the Task list."""
    transport = httpx.ASGITransport(app=build_sim_app(_config(task_limit=1)))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
        token = await _token(client)
        first = await client.post(SEND_URL, json=_body(_prompt()), headers=_headers(token))
        agreement = _agreement_of(first.json())
        evicting = await client.post(
            SEND_URL, json=_body(_prompt(rate="10 Mbps")), headers=_headers(token)
        )
        tasks = await client.get(
            "/a2a/json/tasks", headers={"Authorization": f"Bearer {token}"}
        )
        listed = await client.get(
            "/a2a/json/agreements", headers={"Authorization": f"Bearer {token}"}
        )
        fetched = await client.get(
            f"/a2a/json/agreements/{agreement['agreementId']}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert [task["id"] for task in tasks.json()["tasks"]] == [evicting.json()["task"]["id"]]
    ids = [entry["agreementId"] for entry in listed.json()["agreements"]]
    assert agreement["agreementId"] in ids
    assert fetched.status_code == 200
    assert fetched.json()["currentRevision"] == 1
