"""v2 prototype: the typed statement is the contract, the rendered prompt is a view.

Two things must hold before the prototype is worth anything:

* reading the data and parsing the text must produce **the same plan** -- otherwise the deterministic
  path is a second implementation that will drift;
* when both are present the data wins, so a stale rendering cannot change what is executed.
"""

from __future__ import annotations

import httpx
import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.approval import plan_for
from demo.nms_sim.config import FIDELITY_FAST, WINDOW_POLICY_ANY, SimConfig
from demo.nms_sim.intent import parse_message_request, read_message_request
from shared.approval import plan_digest
from shared.typed_payload import (
    TYPED_SCHEMA_URI,
    TYPED_TASK_T_METADATA_KEY,
    build_statement,
    read_statement,
    statement_metadata,
)

CONFIG = SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY)
HEADERS = {"A2A-Extensions": TASK_T_EXTENSION_URI}

TEXT_PROMPT = "\n".join(
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
        "Energy saving time: 08:00~10:00; guaranteed throughput: 10 Mbps.",
    ]
)


def _body(*, prompt: str | None = None, statement: dict[str, object] | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if prompt is not None:
        metadata[TASK_T_EXTENSION_URI] = prompt
    if statement is not None:
        metadata[TYPED_TASK_T_METADATA_KEY] = statement
    return {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "run"}], "metadata": metadata}}


def _statement(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schemaUri": TYPED_SCHEMA_URI,
        "operation": "Create",
        "region": "A2ATestRegion",
        "scopes": [{"window": "08:00-10:00", "rateMbps": 10}],
    }
    values.update(overrides)
    return values


def test_the_typed_path_reaches_the_same_plan_as_the_text_path() -> None:
    """Same business values, two paths, one plan: this is the regression the prototype needs."""
    from_text = parse_message_request(_body(prompt=TEXT_PROMPT), HEADERS, CONFIG)
    from_data = parse_message_request(_body(statement=_statement()), HEADERS, CONFIG)

    assert plan_digest(plan_for(from_data)) == plan_digest(plan_for(from_text))


def test_the_statement_wins_when_the_rendered_text_disagrees() -> None:
    """A stale rendering must not decide what is executed."""
    conflicting = TEXT_PROMPT.replace("A2ATestRegion", "SomeOtherRegion")

    intent = parse_message_request(
        _body(prompt=conflicting, statement=_statement()), HEADERS, CONFIG
    )

    assert intent.region == "A2ATestRegion"
    assert intent.source.startswith("typed:")


def test_a_partial_statement_still_negotiates() -> None:
    """Missing values are the negotiation's job on both paths."""
    body = _body(statement=_statement(scopes=[]))

    content = read_message_request(body, HEADERS)

    assert content.windows == ()
    assert content.rate_mbps is None


@pytest.mark.parametrize(
    "broken",
    [
        {"schemaUri": "https://unknown.example/schema", "operation": "Create", "region": "R"},
        {"schemaUri": TYPED_SCHEMA_URI, "operation": "Delete", "region": "R"},
        {"schemaUri": TYPED_SCHEMA_URI, "operation": "Create", "region": ""},
        {
            "schemaUri": TYPED_SCHEMA_URI,
            "operation": "Create",
            "region": "R",
            "scopes": [{"window": "8am-10am", "rateMbps": 10}],
        },
        {
            "schemaUri": TYPED_SCHEMA_URI,
            "operation": "Create",
            "region": "R",
            "scopes": [{"window": "08:00-10:00", "rateMbps": -1}],
        },
    ],
)
def test_a_malformed_statement_is_rejected_not_ignored(broken: dict[str, object]) -> None:
    """A statement claims to *be* the contract; silently ignoring it would execute something else."""
    with pytest.raises(ValueError, match="typed.invalid"):
        read_statement({TYPED_TASK_T_METADATA_KEY: broken})


def test_the_statement_round_trips_through_metadata() -> None:
    statement = build_statement(
        operation="Modify",
        region="A2ATestRegion",
        scopes=[{"window": "08:00-10:00", "rateMbps": 2}],
        saving_rate_percent=30.0,
        sites=["LNR_KPI_7745"],
    )

    read = read_statement(statement_metadata(statement))

    assert read == statement


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.put(
        "/rest/plat/smapp/v1/sessions",
        json={"grantType": "password", "userName": "snbitestDefaultNorth", "value": "Changeme_123"},
    )
    assert response.status_code == 200
    return response.json()["accessSession"]


async def test_the_simulator_takes_the_deterministic_path_and_counts_it() -> None:
    """The counter is the prototype's evidence: a typed request must not be parsed as prose."""
    app = build_sim_app(SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as client:
        token = await _token(client)
        headers = {"Authorization": f"Bearer {token}", **HEADERS}
        sent = await client.post(
            "/a2a/json/message:send", json=_body(statement=_statement()), headers=headers
        )
        metrics = await client.get("/a2a/json/metrics", headers={"Authorization": f"Bearer {token}"})

        text_app = build_sim_app(SimConfig(fidelity=FIDELITY_FAST, window_policy=WINDOW_POLICY_ANY))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=text_app), base_url="http://sim"
        ) as text_client:
            text_token = await _token(text_client)
            await text_client.post(
                "/a2a/json/message:send",
                json=_body(prompt=TEXT_PROMPT),
                headers={"Authorization": f"Bearer {text_token}", **HEADERS},
            )
            text_metrics = await text_client.get(
                "/a2a/json/metrics", headers={"Authorization": f"Bearer {text_token}"}
            )

    assert sent.status_code == 200, sent.text
    # ``message:send`` answers with the first event; completion is pushed afterwards.
    assert sent.json()["task"]["status"]["state"] != "TASK_STATE_REJECTED"
    assert metrics.json()["protocols"]["a2a-t"]["paths"] == {"typed": 1}
    assert text_metrics.json()["protocols"]["a2a-t"]["paths"] == {"text": 1}
