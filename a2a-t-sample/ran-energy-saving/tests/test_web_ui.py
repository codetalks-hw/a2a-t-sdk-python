"""ASGI tests of the RAN energy-saving Web UI (no network)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    NegotiationContext,
    NegotiationPerformative,
)
from protocol.client_flow import ClientFlowResult
from protocol.decision_provider import ManualDecisionProvider
from shared.config import SampleConfig
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import FINAL_STATE_TASK_COMPLETED
from web.app import create_app

CONTEXT_ID = "ctx-web"


def _propose_view() -> NegotiationProposeView:
    return NegotiationProposeView(
        negotiation_id=CONTEXT_ID,
        kind="information",
        round=1,
        max_rounds=3,
        content="Please provide the following missing items: region.",
        template_uri="Negotiation-T/information-negotiation/propose/v1",
        context_id=CONTEXT_ID,
    )


def _session() -> NegotiationContext:
    return NegotiationContext(
        id=CONTEXT_ID,
        round=1,
        max_rounds=3,
        performative=NegotiationPerformative.PROPOSE,
    )


def _propose_event() -> dict[str, object]:
    return {
        "kind": "message",
        "text": "Negotiation proposal from the RAN energy saving service",
        "context_id": CONTEXT_ID,
        "message_id": "propose-1",
        "metadata": {
            NEGOTIATION_T_EXTENSION_URI: "Please provide the following missing items: region.",
            NEGOTIATION_CONTEXT_METADATA_KEY: {
                "id": CONTEXT_ID,
                "round": 1,
                "maxRounds": 3,
                "performative": "PROPOSE",
            },
        },
    }


def _status_event() -> dict[str, object]:
    return {
        "kind": "status",
        "task_id": "task-1",
        "context_id": CONTEXT_ID,
        "state": "TASK_STATE_COMPLETED",
    }


class FakeRuntime:
    """Deterministic runtime double: one proposal, an optional manual wait, then completion."""

    def __init__(self, *, manual: bool = False, fail: bool = False) -> None:
        self.manual_provider = ManualDecisionProvider(timeout_seconds=5.0) if manual else None
        self.fail = fail
        self.closed = False

    async def run(
        self,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        on_decision_requested: Callable[[NegotiationProposeView, NegotiationContext], None] | None = None,
    ) -> ClientFlowResult:
        if event_sink is not None:
            event_sink(_propose_event())
        if on_decision_requested is not None:
            on_decision_requested(_propose_view(), _session())
        decision = NegotiationDecision(action="accept", rationale="The proposal is acceptable.")
        if self.manual_provider is not None:
            decision = await self.manual_provider.respond(_propose_view(), _session())
        if event_sink is not None:
            event_sink(_status_event())
        if self.fail:
            raise RuntimeError("boom")
        return ClientFlowResult(
            final_state=FINAL_STATE_TASK_COMPLETED,
            decisions=[decision],
            context_id=CONTEXT_ID,
            task_id="task-1",
            report={"summary": "Plan applied", "estimated_saving_kwh": 12.5},
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def runtimes() -> list[FakeRuntime]:
    """Runtimes built by the injected factory, in start order."""
    return []


@pytest.fixture
def factory(runtimes: list[FakeRuntime]) -> Callable[..., Any]:
    """Runtime factory creating a runtime according to the requested mode."""

    async def build(config: SampleConfig, scenario: str, mode: str, transport: str) -> FakeRuntime:
        del config, scenario, transport
        runtime = FakeRuntime(manual=(mode == "manual"))
        runtimes.append(runtime)
        return runtime

    return build


@pytest.fixture
async def client(
    offline_config: SampleConfig,
    factory: Callable[..., Any],
    tmp_path: Path,
) -> Any:
    """HTTP client bound to the ASGI app."""
    app = create_app(offline_config, runs_dir=tmp_path, runtime_factory=factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def _wait_status(
    client: httpx.AsyncClient,
    run_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Poll the run status until ``predicate`` holds."""
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        latest = response.json()
        if predicate(latest):
            return latest
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach the expected state: {latest}")


def _sse_payloads(body: str) -> list[dict[str, Any]]:
    """Parse the SSE data frames of one response body."""
    payloads: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            payloads.append(json.loads(line[len("data:") :].strip()))
    return payloads


async def test_meta_exposes_the_read_only_process_language(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/meta")

    assert response.status_code == 200
    meta = response.json()
    assert meta["language"] == "en-US"
    assert meta["modelMode"] == "offline"
    assert meta["modelLabel"] == "offline (scripted)"
    assert "information" in meta["scenarios"]
    assert meta["modes"] == ["auto", "manual"]
    assert meta["defaultScenario"] == "information"
    assert meta["defaultMode"] == "auto"
    assert meta["defaultTransport"] == "sse"


async def test_cli_defaults_are_exposed_and_used_when_the_body_omits_them(
    offline_config: SampleConfig,
    factory: Callable[..., Any],
    tmp_path: Path,
) -> None:
    app = create_app(
        offline_config,
        runs_dir=tmp_path,
        runtime_factory=factory,
        default_scenario="reject",
        default_mode="auto",
        default_transport="webhook",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        meta = (await http_client.get("/api/meta")).json()
        assert meta["defaultScenario"] == "reject"
        assert meta["defaultMode"] == "auto"
        assert meta["defaultTransport"] == "webhook"

        start = await http_client.post("/api/runs", json={})
        assert start.status_code == 202
        run_id = start.json()["runId"]
        status = await _wait_status(http_client, run_id, lambda data: data["status"] == "finished")

    assert status["scenario"] == "reject"
    assert status["mode"] == "auto"
    assert status["transport"] == "webhook"


async def test_root_serves_the_single_page(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "RAN energy saving negotiation" in response.text
    assert "EventSource" in response.text


async def test_run_lifecycle_streams_sse_events(client: httpx.AsyncClient, runtimes: list[FakeRuntime]) -> None:
    start = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "auto", "transport": "sse"},
    )

    assert start.status_code == 202
    run_id = start.json()["runId"]
    events = await client.get(f"/api/runs/{run_id}/events")
    payloads = _sse_payloads(events.text)
    types = [payload["type"] for payload in payloads]
    assert "event" in types
    assert "decision_requested" in types
    assert types[-1] == "finished"
    assert payloads[-1]["finalState"] == FINAL_STATE_TASK_COMPLETED

    status = await _wait_status(client, run_id, lambda data: data["status"] == "finished")
    assert status["taskId"] == "task-1"
    assert status["report"]["summary"] == "Plan applied"
    assert status["transcript"]["finalState"] == FINAL_STATE_TASK_COMPLETED
    assert runtimes[0].closed is True


async def test_manual_decision_submission_advances_the_run(
    client: httpx.AsyncClient,
    runtimes: list[FakeRuntime],
) -> None:
    start = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "manual", "transport": "sse"},
    )
    assert start.status_code == 202
    run_id = start.json()["runId"]

    pending = await _wait_status(client, run_id, lambda data: data["pendingDecision"] is True)
    assert pending["status"] == "running"

    decision = {
        "action": "accept",
        "rationale": "The region is supplied by the operator.",
        "filled_params": {"region": "Songshanhu Administration Committee"},
    }
    response = await client.post(f"/api/runs/{run_id}/decisions", json=decision)

    assert response.status_code == 200
    finished = await _wait_status(client, run_id, lambda data: data["status"] != "running")
    assert finished["finalState"] == FINAL_STATE_TASK_COMPLETED
    assert finished["transcript"]["decisions"][0]["action"] == "accept"
    assert finished["transcript"]["decisions"][0]["rationale"] == decision["rationale"]
    assert runtimes[0].closed is True


async def test_second_start_is_rejected_while_a_run_is_active(
    client: httpx.AsyncClient,
    runtimes: list[FakeRuntime],
) -> None:
    first = await client.post("/api/runs", json={"scenario": "information", "mode": "manual", "transport": "sse"})
    assert first.status_code == 202
    run_id = first.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["pendingDecision"] is True)

    second = await client.post("/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"})

    assert second.status_code == 409
    await client.post(
        f"/api/runs/{run_id}/decisions",
        json={"action": "accept", "rationale": "ok"},
    )
    await _wait_status(client, run_id, lambda data: data["status"] != "running")


async def test_unknown_run_returns_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/runs/missing")).status_code == 404
    assert (await client.get("/api/runs/missing/events")).status_code == 404
    assert (
        await client.post("/api/runs/missing/decisions", json={"action": "accept", "rationale": "ok"})
    ).status_code == 404
    assert (await client.get("/api/runs/missing/transcript.json")).status_code == 404
    assert (await client.get("/api/runs/missing/transcript.md")).status_code == 404


async def test_decision_without_a_pending_request_returns_409(client: httpx.AsyncClient) -> None:
    start = await client.post("/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"})
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    response = await client.post(
        f"/api/runs/{run_id}/decisions",
        json={"action": "accept", "rationale": "too late"},
    )

    assert response.status_code == 409


async def test_invalid_start_bodies_are_rejected(client: httpx.AsyncClient) -> None:
    bad_scenario = {"scenario": "nope", "mode": "auto", "transport": "sse"}
    bad_mode = {"scenario": "information", "mode": "x", "transport": "sse"}
    bad_transport = {"scenario": "information", "mode": "auto", "transport": "x"}

    assert (await client.post("/api/runs", json=bad_scenario)).status_code == 400
    assert (await client.post("/api/runs", json=bad_mode)).status_code == 400
    assert (await client.post("/api/runs", json=bad_transport)).status_code == 400


async def test_transcript_downloads(client: httpx.AsyncClient) -> None:
    start = await client.post("/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"})
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    json_response = await client.get(f"/api/runs/{run_id}/transcript.json")
    markdown_response = await client.get(f"/api/runs/{run_id}/transcript.md")

    assert json_response.status_code == 200
    assert "application/json" in json_response.headers["content-type"]
    document = json_response.json()
    assert document["finalState"] == FINAL_STATE_TASK_COMPLETED
    assert document["scenario"] == "information"
    assert markdown_response.status_code == 200
    assert "text/markdown" in markdown_response.headers["content-type"]
    assert "RAN energy-saving negotiation transcript" in markdown_response.text
