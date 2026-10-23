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
from integration_helpers import free_tcp_port, offline_direct_config, running_server
from protocol.client_flow import ClientFlowResult
from protocol.decision_provider import ManualDecisionProvider
from shared.config import SampleConfig
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import FINAL_STATE_TASK_COMPLETED, get_scenario, resolve_user_request
from shared.trace import PHASES
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


def _trace_span(
    kind: str,
    *,
    phase: str,
    title: str,
    round_number: int | None = None,
    context_id: str | None = CONTEXT_ID,
    detail: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one synthetic trace span for the runtime double."""
    return {
        "schemaVersion": 1,
        "runId": "",
        "seq": 0,
        "spanId": "",
        "parentSpanId": None,
        "contextId": context_id,
        "taskId": None,
        "phase": phase,
        "phaseStep": 0,
        "round": round_number,
        "actor": "client",
        "side": "client",
        "kind": kind,
        "stage": None,
        "title": title,
        "purpose": None,
        "status": "ok",
        "startedAt": "2026-10-23T00:00:00.000Z",
        "endedAt": None,
        "durationMs": None,
        "direction": "internal",
        "model": None,
        "tokens": None,
        "error": None,
        "detail": detail or {},
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
        on_decision_made: Callable[
            [NegotiationDecision, NegotiationProposeView, NegotiationContext], None
        ]
        | None = None,
        trace_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> ClientFlowResult:
        if trace_sink is not None:
            trace_sink(
                _trace_span(
                    "task_prompt_rendered",
                    phase="task_prompt",
                    title="Task-T prompt rendered",
                    detail={"length": 12},
                )
            )
        if event_sink is not None:
            event_sink(_propose_event())
        if trace_sink is not None:
            trace_sink(
                _trace_span(
                    "decision_requested",
                    phase="negotiation",
                    title="Decision requested",
                    round_number=1,
                )
            )
        if on_decision_requested is not None:
            on_decision_requested(_propose_view(), _session())
        decision = NegotiationDecision(action="accept", rationale="The proposal is acceptable.")
        if self.manual_provider is not None:
            decision = await self.manual_provider.respond(_propose_view(), _session())
        if trace_sink is not None:
            trace_sink(
                _trace_span(
                    "decision_made",
                    phase="resolution",
                    title="Decision made",
                    round_number=1,
                )
            )
        if on_decision_made is not None:
            on_decision_made(decision, _propose_view(), _session())
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

    async def build(
        config: SampleConfig,
        scenario: str,
        mode: str,
        transport: str,
        discovery: str,
    ) -> FakeRuntime:
        del config, scenario, transport, discovery
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
    assert meta["discoveries"] == ["registry", "direct"]
    assert meta["defaultScenario"] == "information"
    assert meta["defaultMode"] == "auto"
    assert meta["defaultTransport"] == "sse"
    assert meta["defaultDiscovery"] == "registry"


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
    assert types[0] == "run_started"
    assert "event" in types
    assert "trace" in types
    # An auto run never exposes a pending manual decision.
    assert "decision_requested" not in types
    assert types[-1] == "finished"
    assert payloads[-1]["finalState"] == FINAL_STATE_TASK_COMPLETED
    assert [payload["seq"] for payload in payloads] == list(range(1, len(payloads) + 1))

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


async def test_auto_run_does_not_expose_a_pending_manual_decision(
    client: httpx.AsyncClient,
    runtimes: list[FakeRuntime],
) -> None:
    start = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "auto", "transport": "sse"},
    )
    assert start.status_code == 202
    run_id = start.json()["runId"]

    events = await client.get(f"/api/runs/{run_id}/events")
    payloads = _sse_payloads(events.text)
    assert [payload["type"] for payload in payloads if payload["type"] == "decision_requested"] == []

    status = await _wait_status(client, run_id, lambda data: data["status"] == "finished")
    assert status["mode"] == "auto"
    assert status["pendingDecision"] is False
    response = await client.post(
        f"/api/runs/{run_id}/decisions",
        json={"action": "accept", "rationale": "There is no manual decision to answer."},
    )
    assert response.status_code == 409


async def test_manual_run_publishes_a_manual_decision_request(
    client: httpx.AsyncClient,
    runtimes: list[FakeRuntime],
) -> None:
    start = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "manual", "transport": "sse"},
    )
    assert start.status_code == 202
    run_id = start.json()["runId"]
    events_task = asyncio.create_task(client.get(f"/api/runs/{run_id}/events"))

    pending = await _wait_status(client, run_id, lambda data: data["pendingDecision"] is True)
    assert pending["mode"] == "manual"

    response = await client.post(
        f"/api/runs/{run_id}/decisions",
        json={"action": "accept", "rationale": "The operator accepts the proposal."},
    )
    assert response.status_code == 200
    events = await asyncio.wait_for(events_task, timeout=5)
    requests = [
        payload for payload in _sse_payloads(events.text) if payload["type"] == "decision_requested"
    ]
    assert len(requests) == 1
    assert requests[0]["mode"] == "manual"
    await _wait_status(client, run_id, lambda data: data["status"] != "running")


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
    assert [entry["seq"] for entry in document["timeline"]] == sorted(
        entry["seq"] for entry in document["timeline"]
    )
    assert markdown_response.status_code == 200
    assert "text/markdown" in markdown_response.headers["content-type"]
    assert "RAN energy-saving negotiation transcript" in markdown_response.text
    assert "| # | Seq | Phase | Step |" in markdown_response.text


async def test_run_started_is_the_first_replayable_frame(client: httpx.AsyncClient) -> None:
    start = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "auto", "transport": "sse", "discovery": "direct"},
    )
    assert start.status_code == 202
    assert start.json()["discovery"] == "direct"
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    payloads = _sse_payloads((await client.get(f"/api/runs/{run_id}/events")).text)
    first = payloads[0]
    spec = get_scenario("information")
    assert first["type"] == "run_started"
    assert first["seq"] == 1
    assert first["runId"] == run_id
    assert first["description"] == spec.description
    assert first["userRequest"] == resolve_user_request(spec, "en-US")
    assert first["language"] == "en-US"
    assert first["mode"] == "auto"
    assert first["transport"] == "sse"
    assert first["discovery"] == "direct"
    assert first["modelMode"] == "offline"
    assert first["modelLabel"] == "offline (scripted)"
    assert first["expectedSequence"] == [
        {"actor": step.actor, "performative": step.performative, "round": step.round}
        for step in spec.expected_sequence
    ]
    assert first["phase"] == "dispatch"
    assert first["spanId"]

    # A reconnect replays the same ordered messages without duplicating seq values.
    replay = _sse_payloads((await client.get(f"/api/runs/{run_id}/events")).text)
    assert replay[0] == first
    assert [payload["seq"] for payload in replay] == list(range(1, len(replay) + 1))


async def test_live_decision_is_recorded_before_the_task_events(client: httpx.AsyncClient) -> None:
    start = await client.post(
        "/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"}
    )
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    payloads = _sse_payloads((await client.get(f"/api/runs/{run_id}/events")).text)
    entries = [payload["event"] for payload in payloads if payload["type"] == "event"]
    decision_index = next(
        index for index, entry in enumerate(entries) if entry["eventKind"] == "decision"
    )
    task_index = next(index for index, entry in enumerate(entries) if entry["eventKind"] == "status")
    assert decision_index < task_index
    assert entries[decision_index]["action"] == "accept"
    assert entries[decision_index]["spanId"]


async def test_trace_json_span_lookup_and_ingestion(client: httpx.AsyncClient) -> None:
    start = await client.post(
        "/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"}
    )
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    trace_response = await client.get(f"/api/runs/{run_id}/trace.json")
    assert trace_response.status_code == 200
    document = trace_response.json()
    assert document["runId"] == run_id
    assert document["schemaVersion"] == 1
    assert document["transcript"]["finalState"] == FINAL_STATE_TASK_COMPLETED
    spans = document["spans"]
    assert spans
    assert [span["seq"] for span in spans] == sorted(span["seq"] for span in spans)
    assert {span["phase"] for span in spans} >= {"dispatch", "task_prompt", "done"}

    lookup = await client.get(f"/api/runs/{run_id}/spans/{spans[0]['spanId']}")
    assert lookup.status_code == 200
    assert lookup.json()["spanId"] == spans[0]["spanId"]
    assert (await client.get(f"/api/runs/{run_id}/spans/missing-span")).status_code == 404

    remote = {
        "spanId": "server-span-1",
        "parentSpanId": "client-span",
        "kind": "llm",
        "phase": "execution",
        "phaseStep": 4,
        "round": None,
        "actor": "llm",
        "side": "server",
        "title": "Server LLM call",
        "status": "ok",
        "detail": {"model": "fake-model"},
    }
    accepted = await client.post(f"/api/runs/{run_id}/trace", json=[remote])
    assert accepted.status_code == 202
    assert accepted.json() == {"accepted": 1, "runId": run_id}

    stored = (await client.get(f"/api/runs/{run_id}/spans/server-span-1")).json()
    assert stored["runId"] == run_id
    assert stored["seq"] > spans[-1]["seq"]
    assert stored["detail"]["model"] == "fake-model"

    replay = _sse_payloads((await client.get(f"/api/runs/{run_id}/events")).text)
    ingested = [
        payload
        for payload in replay
        if payload["type"] == "trace" and payload["event"]["spanId"] == "server-span-1"
    ]
    assert len(ingested) == 1
    assert ingested[0]["seq"] == stored["seq"]

    wrapped = await client.post(
        f"/api/runs/{run_id}/trace",
        json={"spans": [{"spanId": "server-span-2", "kind": "tool", "phase": "execution"}]},
    )
    assert wrapped.status_code == 202
    assert (await client.get(f"/api/runs/{run_id}/spans/server-span-2")).status_code == 200


async def test_trace_ingestion_validates_the_run_and_payload(client: httpx.AsyncClient) -> None:
    start = await client.post(
        "/api/runs", json={"scenario": "information", "mode": "auto", "transport": "sse"}
    )
    run_id = start.json()["runId"]
    await _wait_status(client, run_id, lambda data: data["status"] == "finished")

    assert (await client.post("/api/runs/missing/trace", json=[])).status_code == 404
    assert (await client.post(f"/api/runs/{run_id}/trace", json=42)).status_code == 400
    assert (await client.post(f"/api/runs/{run_id}/trace", json=[1, 2])).status_code == 400
    malformed = await client.post(
        f"/api/runs/{run_id}/trace",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert malformed.status_code == 400


async def test_real_offline_information_run_has_unique_spans_and_all_phases(
    english_language: None,
    tmp_path: Path,
) -> None:
    """The real client flow (not a double) must keep run-wide span ids unique and all 8 phases."""
    del english_language  # fixture side effect: pin the scripted SDK language
    config = offline_direct_config(free_tcp_port())
    with running_server(config):
        app = create_app(config, runs_dir=tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            start = await http_client.post(
                "/api/runs",
                json={
                    "scenario": "information",
                    "mode": "auto",
                    "transport": "sse",
                    "discovery": "direct",
                },
            )
            assert start.status_code == 202
            run_id = start.json()["runId"]
            status = await _wait_status(
                http_client,
                run_id,
                lambda data: data["status"] != "running",
                timeout=30.0,
            )
            assert status["status"] == "finished"
            assert status["finalState"] == FINAL_STATE_TASK_COMPLETED

            payloads = _sse_payloads((await http_client.get(f"/api/runs/{run_id}/events")).text)
            run_started = payloads[0]
            assert run_started["type"] == "run_started"

            looked_up = await http_client.get(
                f"/api/runs/{run_id}/spans/{run_started['spanId']}"
            )
            assert looked_up.status_code == 200
            assert looked_up.json()["kind"] == "run_started"

            document = (await http_client.get(f"/api/runs/{run_id}/trace.json")).json()
            spans = document["spans"]
            span_ids = [span["spanId"] for span in spans]
            assert len(span_ids) == len(set(span_ids))
            assert span_ids.count(run_started["spanId"]) == 1
            assert {span["spanId"]: span["kind"] for span in spans}[run_started["spanId"]] == "run_started"

            phases = {span["phase"] for span in spans}
            assert set(PHASES) <= phases

            order: list[str] = []
            for span in spans:
                phase = str(span["phase"])
                if phase not in order:
                    order.append(phase)
            assert order == list(PHASES)

            grouped: dict[str, list[object]] = {}
            for span in spans:
                grouped.setdefault(str(span["phase"]), []).append(span["phaseStep"])
            for steps in grouped.values():
                assert steps == list(range(1, len(steps) + 1))


async def test_start_accepts_discovery_and_rejects_unknown_values(client: httpx.AsyncClient) -> None:
    accepted = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "auto", "transport": "sse", "discovery": "direct"},
    )
    assert accepted.status_code == 202
    run_id = accepted.json()["runId"]
    status = await _wait_status(client, run_id, lambda data: data["status"] == "finished")
    assert status["discovery"] == "direct"

    rejected = await client.post(
        "/api/runs",
        json={"scenario": "information", "mode": "auto", "transport": "sse", "discovery": "nope"},
    )
    assert rejected.status_code == 400
