"""Tests for the structured negotiation transcript."""

from __future__ import annotations

import json
from pathlib import Path

from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from protocol.client_flow import ClientFlowResult
from protocol.transcript import (
    DIRECTION_CLIENT_TO_SERVER,
    DIRECTION_SERVER_TO_CLIENT,
    FINAL_STATE_FAILED,
    NegotiationTranscript,
)
from shared.models import NegotiationDecision, NegotiationProposeView
from shared.scenario import FINAL_STATE_TASK_COMPLETED

CONTEXT_ID = "ctx-transcript"


def _propose_event() -> dict[str, object]:
    return {
        "kind": "message",
        "text": "Negotiation proposal from the RAN energy saving service",
        "context_id": CONTEXT_ID,
        "message_id": "propose-1",
        "metadata": {
            NEGOTIATION_T_EXTENSION_URI: "Please provide the following missing items: region.",
            TEMPLATE_URI_METADATA_KEY: "Negotiation-T/information-negotiation/propose/v1",
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


def _artifact_event() -> dict[str, object]:
    return {
        "kind": "artifact",
        "task_id": "task-1",
        "context_id": CONTEXT_ID,
        "artifact_id": "artifact-1",
        "name": "energy saving intent report",
        "data": {"estimated_saving_kwh": 12.5},
    }


def _transcript() -> NegotiationTranscript:
    return NegotiationTranscript(
        scenario="information",
        language="en-US",
        model_mode="offline",
        mode="auto",
        transport="sse",
    )


def test_record_event_extracts_round_performative_and_kind() -> None:
    transcript = _transcript()

    entry = transcript.record_event(_propose_event())

    assert entry["direction"] == DIRECTION_SERVER_TO_CLIENT
    assert entry["round"] == 1
    assert entry["maxRounds"] == 3
    assert entry["performative"] == "PROPOSE"
    assert entry["kind"] == "propose"
    assert entry["eventKind"] == "message"
    assert entry["contextId"] == CONTEXT_ID
    assert entry["templateUri"] == "Negotiation-T/information-negotiation/propose/v1"
    assert "missing items" in str(entry["summary"])
    assert entry["rationale"] is None
    assert transcript.context_id == CONTEXT_ID


def test_record_decision_carries_the_rationale_and_params() -> None:
    transcript = _transcript()
    propose = NegotiationProposeView(
        negotiation_id=CONTEXT_ID,
        kind="information",
        round=2,
        max_rounds=3,
        content="proposal",
        template_uri="Negotiation-T/information-negotiation/propose/v1",
        context_id=CONTEXT_ID,
    )
    decision = NegotiationDecision(
        action="accept",
        rationale="The region is now supplied.",
        filled_params={"region": "Songshanhu Administration Committee"},
    )

    entry = transcript.record_decision(decision, propose=propose)

    assert entry["direction"] == DIRECTION_CLIENT_TO_SERVER
    assert entry["kind"] == "accept"
    assert entry["performative"] == "ACCEPT"
    assert entry["round"] == 2
    assert entry["rationale"] == "The region is now supplied."
    assert entry["filledParams"] == {"region": "Songshanhu Administration Committee"}
    assert transcript.decisions == [entry]


def test_record_final_and_write_json_and_markdown(tmp_path: Path) -> None:
    transcript = _transcript()
    transcript.record_event(_propose_event())
    transcript.record_event(_status_event())
    transcript.record_event(_artifact_event())
    transcript.record_decision(
        NegotiationDecision(action="accept", rationale="Acceptable."),
    )
    result = ClientFlowResult(
        final_state=FINAL_STATE_TASK_COMPLETED,
        context_id=CONTEXT_ID,
        task_id="task-1",
        report={"summary": "Plan applied", "estimated_saving_kwh": 12.5},
    )
    transcript.record_final(result)

    paths = transcript.write(tmp_path)

    assert paths.json_path.exists()
    assert paths.markdown_path.exists()
    assert paths.json_path.name.endswith("-information.json")
    document = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert document["finalState"] == FINAL_STATE_TASK_COMPLETED
    assert document["report"]["summary"] == "Plan applied"
    assert document["contextId"] == CONTEXT_ID
    assert document["taskId"] == "task-1"
    assert len(document["events"]) == 3
    assert document["events"][0]["round"] == 1
    assert document["events"][1]["kind"] == "status"
    assert document["events"][2]["kind"] == "artifact"
    markdown = paths.markdown_path.read_text(encoding="utf-8")
    assert "# RAN energy-saving negotiation transcript" in markdown
    assert "| 1 | 1 | server->client | propose | PROPOSE |" in markdown
    assert "task_completed" in markdown
    assert "Plan applied" in markdown


def test_record_error_marks_the_run_failed() -> None:
    transcript = _transcript()

    transcript.record_error("RuntimeError: boom")

    document = transcript.to_dict()
    assert document["finalState"] == FINAL_STATE_FAILED
    assert document["error"] == "RuntimeError: boom"
    assert document["finishedAt"] is not None
