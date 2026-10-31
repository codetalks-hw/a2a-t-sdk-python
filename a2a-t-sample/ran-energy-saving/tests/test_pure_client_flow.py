"""Tests of the pure A2A client flow and its INPUT_REQUIRED continuation."""

from __future__ import annotations

from a2a.client.client import ClientCallContext
from a2a.types import SendMessageRequest
from protocol.pure_client_flow import run_pure_client_flow
from shared.run_constants import FINAL_STATE_TASK_COMPLETED


class _FakeTransport:
    """Return pre-built event batches and record every request."""

    def __init__(self, batches: list[list[dict[str, object]]]) -> None:
        self._batches = list(batches)
        self.requests: list[tuple[SendMessageRequest, ClientCallContext]] = []

    async def send(
        self,
        request: SendMessageRequest,
        call_context: ClientCallContext,
    ) -> list[dict[str, object]]:
        self.requests.append((request, call_context))
        if not self._batches:
            return []
        return self._batches.pop(0)


def _input_required(*, round_number: int = 1) -> dict[str, object]:
    return {
        "kind": "status",
        "task_id": "task-1",
        "context_id": "ctx-1",
        "state": "TASK_STATE_INPUT_REQUIRED",
        "text": "Please provide the target.",
        "metadata": {
            "pure": {
                "kind": "negotiation",
                "phase": "information",
                "round": round_number,
                "maxRounds": 3,
            }
        },
    }


def _completed() -> dict[str, object]:
    return {
        "kind": "status",
        "task_id": "task-1",
        "context_id": "ctx-1",
        "state": "TASK_STATE_COMPLETED",
        "text": "RAN energy-saving task completed.",
    }


async def test_the_flow_continues_the_same_task_after_input_required() -> None:
    transport = _FakeTransport([[_input_required()], [_completed()]])
    spans: list[dict[str, object]] = []

    result = await run_pure_client_flow(
        transport=transport,  # type: ignore[arg-type]
        user_request="Create a RAN energy saving task.",
        answer=lambda event: "Use {00:00~06:00,10Mbps}.",
        trace_sink=spans.append,
    )

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert len(transport.requests) == 2
    first_request, first_context = transport.requests[0]
    assert first_request.message.parts[0].text == "Create a RAN energy saving task."
    assert first_context.service_parameters == {"A2A-Extensions": ""}

    follow_up, follow_context = transport.requests[1]
    assert follow_up.message.task_id == "task-1"
    assert follow_up.message.context_id == "ctx-1"
    assert follow_up.message.parts[0].text == "Use {00:00~06:00,10Mbps}."
    assert follow_context.service_parameters == {"A2A-Extensions": ""}
    assert len(spans) == 2
    assert all(span["detail"]["protocol"] == "pure" for span in spans)


async def test_the_default_answer_uses_the_server_proposal() -> None:
    proposal = "{00:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~23:59,10Mbps}"
    event = _input_required()
    event["metadata"] = {"pure": {"kind": "negotiation", "phase": "target", "proposal": proposal}}
    transport = _FakeTransport([[event], [_completed()]])

    await run_pure_client_flow(
        transport=transport,  # type: ignore[arg-type]
        user_request="Create a RAN energy saving task.",
    )

    assert transport.requests[1][0].message.parts[0].text == proposal


async def test_no_answer_stops_the_flow() -> None:
    transport = _FakeTransport([[_input_required()]])

    result = await run_pure_client_flow(
        transport=transport,  # type: ignore[arg-type]
        user_request="Create a RAN energy saving task.",
        answer=lambda event: "",
    )

    assert len(transport.requests) == 1
    assert result.final_state != FINAL_STATE_TASK_COMPLETED
