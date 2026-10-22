"""Tests pinning the five scenario scripts to the design sequence."""

from __future__ import annotations

from shared.scenario import (
    FINAL_STATE_NEGOTIATION_ABORTED,
    FINAL_STATE_NEGOTIATION_ENDED,
    FINAL_STATE_TASK_COMPLETED,
    MAX_ROUNDS,
    SCENARIOS,
    SUPPORTED_LANGUAGES,
    SUPPORTED_SCENARIO_NAMES,
    ExpectedStep,
    resolve_user_request,
)


def _sequence(name: str) -> tuple[tuple[str, str, int | None], ...]:
    return tuple((step.actor, step.performative, step.round) for step in SCENARIOS[name].expected_sequence)


def test_max_rounds_is_three() -> None:
    assert MAX_ROUNDS == 3


def test_scenario_names_and_request_languages() -> None:
    assert SUPPORTED_SCENARIO_NAMES == ("information", "target", "feasibility", "reject", "abort")
    for scenario in SCENARIOS.values():
        for language in SUPPORTED_LANGUAGES:
            assert resolve_user_request(scenario, language)


def test_information_sequence() -> None:
    scenario = SCENARIOS["information"]

    assert scenario.expected_kind == "information"
    assert _sequence("information") == (
        ("server", "PROPOSE", 1),
        ("client", "ACCEPT", 1),
    )
    assert scenario.expected_final_state == FINAL_STATE_TASK_COMPLETED


def test_target_sequence_keeps_counter_propose_in_round_one() -> None:
    scenario = SCENARIOS["target"]

    assert scenario.expected_kind == "target"
    assert _sequence("target") == (
        ("server", "PROPOSE", 1),
        ("client", "PROPOSE", 1),
        ("server", "PROPOSE", 2),
        ("client", "ACCEPT", 2),
    )
    assert scenario.expected_final_state == FINAL_STATE_TASK_COMPLETED
    assert [decision.action for decision in scenario.scripted_client_decisions] == ["counter", "accept"]


def test_feasibility_sequence() -> None:
    scenario = SCENARIOS["feasibility"]

    assert scenario.expected_kind == "feasibility"
    assert _sequence("feasibility") == (
        ("server", "PROPOSE", 1),
        ("client", "ACCEPT", 1),
    )
    assert scenario.expected_final_state == FINAL_STATE_TASK_COMPLETED


def test_reject_sequence_ends_with_plain_message() -> None:
    scenario = SCENARIOS["reject"]

    assert _sequence("reject") == (
        ("server", "PROPOSE", 1),
        ("client", "REJECT", 1),
        ("server", "MESSAGE", None),
    )
    assert scenario.expected_final_state == FINAL_STATE_NEGOTIATION_ENDED
    assert [decision.action for decision in scenario.scripted_client_decisions] == ["reject"]


def test_abort_sequence_aborts_at_max_rounds() -> None:
    scenario = SCENARIOS["abort"]
    sequence = _sequence("abort")

    assert sequence[-1] == ("server", "ABORT", MAX_ROUNDS)
    assert sequence == (
        ("server", "PROPOSE", 1),
        ("client", "PROPOSE", 1),
        ("server", "PROPOSE", 2),
        ("client", "PROPOSE", 2),
        ("server", "PROPOSE", 3),
        ("client", "PROPOSE", 3),
        ("server", "ABORT", 3),
    )
    assert scenario.expected_final_state == FINAL_STATE_NEGOTIATION_ABORTED
    assert [decision.action for decision in scenario.scripted_client_decisions] == ["counter", "counter", "counter"]


def test_expected_step_is_hashable_frozen_dataclass() -> None:
    step = ExpectedStep(actor="server", performative="PROPOSE", round=1)

    assert step.round == 1
