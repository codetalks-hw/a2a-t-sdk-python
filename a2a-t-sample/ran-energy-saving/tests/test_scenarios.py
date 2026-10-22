"""End-to-end acceptance tests of the five RAN energy-saving scenarios.

Each test starts a real uvicorn server in a background thread and runs the real client runtime
(``A2ATClient`` + A2A SDK + ADK ``Runner`` with the deterministic fake model) against it over
``http://127.0.0.1:<ephemeral>`` direct discovery. Nothing touches the network and the scripted
responses make every step deterministic.

Asserted per scenario (design §8):

* the exact performative/round sequence, both directions;
* the decisions the ADK-backed client made;
* the terminal state, and that accept scenarios created a Task and a report while reject/abort
  scenarios created no Task at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from a2a.types import TaskState
from a2a_t.core.metadata import NEGOTIATION_T_EXTENSION_URI
from integration_helpers import (
    free_tcp_port,
    offline_direct_config,
    run_scenario_flow,
    running_server,
)
from protocol.client_flow import ClientFlowResult
from protocol.messages import metadata_to_dict, parse_negotiation_context
from shared.scenario import (
    FINAL_STATE_NEGOTIATION_ABORTED,
    FINAL_STATE_NEGOTIATION_ENDED,
    FINAL_STATE_TASK_COMPLETED,
    SUPPORTED_SCENARIO_NAMES,
    Scenario,
    get_scenario,
)

#: Client decision action matching one expected performative.
_ACTION_BY_PERFORMATIVE: dict[str, str] = {
    "ACCEPT": "accept",
    "PROPOSE": "counter",
    "REJECT": "reject",
    "ABORT": "abort",
}

def _expected_actions(scenario: Scenario) -> list[str]:
    """Return the decisions the scenario's client steps imply, in order."""
    return [
        _ACTION_BY_PERFORMATIVE[step.performative]
        for step in scenario.expected_sequence
        if step.actor == "client"
    ]


def _negotiation_contents(
    events: Sequence[Mapping[str, object]], performative: str
) -> list[tuple[int, str]]:
    """Return ``(round, Negotiation-T content)`` of every inbound message of one performative."""
    contents: list[tuple[int, str]] = []
    for event in events:
        if event.get("kind") != "message":
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        metadata_dict = metadata_to_dict(metadata)
        session = parse_negotiation_context(metadata_dict)
        if session is None or session.performative.value != performative:
            continue
        content = metadata_dict.get(NEGOTIATION_T_EXTENSION_URI)
        if isinstance(content, str):
            contents.append((session.round, content))
    return contents


def _completed_status_seen(result: ClientFlowResult) -> bool:
    """Return whether the client observed the terminal ``COMPLETED`` status."""
    return any(
        event.get("kind") == "status"
        and event.get("state") == TaskState.Name(TaskState.TASK_STATE_COMPLETED)
        for event in result.events
    )


@pytest.mark.parametrize("scenario_name", SUPPORTED_SCENARIO_NAMES)
async def test_scenario_matches_the_design_sequence(
    scenario_name: str,
    english_language: None,
) -> None:
    """Run one scenario end to end over SSE and assert design §8 exactly."""
    scenario = get_scenario(scenario_name)
    config = offline_direct_config(free_tcp_port())

    with running_server(config):
        result, exchange = await run_scenario_flow(config, scenario_name, "sse")

    assert exchange.steps == list(scenario.expected_sequence)
    assert [decision.action for decision in result.decisions] == _expected_actions(scenario)
    assert result.final_state == scenario.expected_final_state

    if scenario.expected_final_state == FINAL_STATE_TASK_COMPLETED:
        assert result.task_id
        assert result.report
        assert _completed_status_seen(result) is True
        return

    # reject and abort never create a Task: no task id, no report, no terminal Task status.
    assert scenario.expected_final_state in (
        FINAL_STATE_NEGOTIATION_ENDED,
        FINAL_STATE_NEGOTIATION_ABORTED,
    )
    assert result.task_id is None
    assert result.report is None
    assert _completed_status_seen(result) is False


@pytest.mark.parametrize(
    ("scenario_name", "expected_tail"),
    [
        ("reject", ("server", "MESSAGE", None)),
        ("abort", ("server", "ABORT", 3)),
    ],
)
async def test_no_task_scenarios_end_with_the_plain_end_or_abort(
    scenario_name: str,
    expected_tail: tuple[str, str, int | None],
    english_language: None,
) -> None:
    """Reject ends with a plain end message; abort ends with ``S:Abort(3)`` and no Task."""
    config = offline_direct_config(free_tcp_port())

    with running_server(config):
        result, exchange = await run_scenario_flow(config, scenario_name, "sse")

    last = exchange.steps[-1]
    assert (last.actor, last.performative, last.round) == expected_tail
    assert result.final_state == get_scenario(scenario_name).expected_final_state
    assert result.task_id is None
    assert result.report is None
    assert exchange.events[-1].get("kind") == "message"


async def test_target_round_two_content_is_revised(english_language: None) -> None:
    """The offline scripted extraction must render a different proposal in round 2 than round 1."""
    config = offline_direct_config(free_tcp_port())

    with running_server(config):
        result, _ = await run_scenario_flow(config, "target", "sse")

    proposals = _negotiation_contents(result.events, "PROPOSE")
    assert [round_number for round_number, _ in proposals] == [1, 2]
    assert proposals[0][1] != proposals[1][1]
    assert "Revised proposal for round 2" in proposals[1][1]


async def test_server_abort_reason_names_the_server_and_the_round_limit(
    english_language: None,
) -> None:
    """The abort scenario's server abort must not borrow the canned client-abort reason."""
    config = offline_direct_config(free_tcp_port())

    with running_server(config):
        result, _ = await run_scenario_flow(config, "abort", "sse")

    proposals = _negotiation_contents(result.events, "PROPOSE")
    assert [round_number for round_number, _ in proposals] == [1, 2, 3]
    assert len({content for _, content in proposals}) == 3

    aborts = _negotiation_contents(result.events, "ABORT")
    assert len(aborts) == 1
    reason = aborts[0][1]
    assert "The server ended the negotiation" in reason
    assert "maximum number of rounds (3)" in reason
    assert "client aborted" not in reason.lower()
