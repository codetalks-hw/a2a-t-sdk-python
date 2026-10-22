"""Business scripts of the five RAN energy-saving demonstration scenarios.

Every scenario carries the client-side natural-language request (both languages), the expected
negotiation kind, the exact performative/round sequence from the design, the expected final state,
and the decisions a scripted (offline) client returns. Tests assert the sequence and the rounds
instead of log text.

The ``reject`` scenario reuses the missing-region request: the server opens an ``information``
negotiation and the client rejects because it cannot provide the missing content. The ``abort``
scenario reuses the overlapping-throughput ``target`` request and counter-proposes an
unacceptable schedule until the server aborts at ``maxRounds``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from shared.models import NegotiationDecision

#: Round budget of every scenario (design D11).
MAX_ROUNDS = 3

#: Supported scenario names.
SCENARIO_INFORMATION = "information"
SCENARIO_TARGET = "target"
SCENARIO_FEASIBILITY = "feasibility"
SCENARIO_REJECT = "reject"
SCENARIO_ABORT = "abort"

#: Supported negotiation kinds (server-side trigger families).
KIND_INFORMATION = "information"
KIND_TARGET = "target"
KIND_FEASIBILITY = "feasibility"

#: Expected terminal states.
FINAL_STATE_TASK_COMPLETED = "task_completed"
FINAL_STATE_NEGOTIATION_ENDED = "no_task_negotiation_ended"
FINAL_STATE_NEGOTIATION_ABORTED = "no_task_negotiation_aborted"

FinalState = Literal[
    "task_completed",
    "no_task_negotiation_ended",
    "no_task_negotiation_aborted",
]

#: Supported process-level languages.
LANGUAGE_EN = "en-US"
LANGUAGE_ZH = "zh-CN"
SUPPORTED_LANGUAGES = (LANGUAGE_EN, LANGUAGE_ZH)

#: Chinese natural-language task request used for prompt generation (UC1, complete input).
NATURAL_LANGUAGE_PROMPT_INPUT_ZH = (
    "在松山湖管委会创建无线网络节能任务：在满足任务上下文的前提下最大化节能；"
    "分时段保障速率目标为 {00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、"
    "{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}。"
)

#: English natural-language task request used for prompt generation (UC1, complete input).
NATURAL_LANGUAGE_PROMPT_INPUT_EN = (
    "Create a RAN energy saving task in the Songshanhu Administration Committee area to maximize "
    "energy saving. The time-segment-based guaranteed throughput target is {00:00~07:00,2Mbps}, "
    "{07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}."
)

#: Chinese natural-language task request of the information negotiation: the region is omitted, so
#: the Task-T validation reports the missing-parameter content error.
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH = (
    "创建无线网络节能任务：在满足任务上下文的前提下最大化节能；"
    "分时段保障速率目标为 {00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、"
    "{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}。"
)

#: English natural-language task request of the information negotiation (region omitted).
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN = (
    "Create a RAN energy saving task to maximize energy saving. The time-segment-based "
    "guaranteed throughput target is {00:00~07:00,2Mbps}, {07:00~17:30,10Mbps}, "
    "{17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}."
)

#: Chinese natural-language task request of the target negotiation: the guaranteed throughput
#: target has two overlapping time segments, so the Task-T validation reports the
#: value-not-allowed content error.
NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_ZH = (
    "在松山湖管委会创建无线网络节能任务：在满足任务上下文的前提下最大化节能；"
    "分时段保障速率目标为 {00:00~07:00,2Mbps}、{07:00~09:00,10Mbps}、{09:00~17:30,10Mbps}、"
    "{17:30~23:00,20Mbps}、{20:00~00:00,10Mbps}。"
)

#: English natural-language task request of the target negotiation (overlapping segments).
NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_EN = (
    "Create a RAN energy saving task in the Songshanhu Administration Committee area to maximize "
    "energy saving. The time-segment-based guaranteed throughput target is {00:00~07:00,2Mbps}, "
    "{07:00~09:00,10Mbps}, {09:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {20:00~00:00,10Mbps}."
)

#: Chinese natural-language task request of the feasibility negotiation: two stations must keep a
#: guaranteed rate through an 18-hour power outage, so the Task-T validation reports the
#: rule-violation content error.
NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_ZH = (
    "在松山湖管委会创建无线网络节能任务：站点列表1为[站点名称1, 站点名称2]；"
    "分时段保障速率目标为 {00:00~06:00,2Mbps}、{06:00~18:00,10Mbps}；"
    "背景事件为在00:00~18:00停电；在满足任务上下文的前提下最大化节能。"
)

#: English natural-language task request of the feasibility negotiation (outage case).
NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_EN = (
    "Create a RAN energy saving task in the Songshanhu Administration Committee area to maximize "
    "energy saving. Station list 1 is [Station 1, Station 2]; the time-segment-based guaranteed "
    "throughput target is {00:00~06:00,2Mbps}, {06:00~18:00,10Mbps}; the background event is a "
    "power outage from 00:00 to 18:00."
)

#: Region display string per language (used by the scripted LLM routing fallback and the accept
#: refill content).
REGION_STRINGS: dict[str, str] = {
    LANGUAGE_EN: "Songshanhu Administration Committee",
    LANGUAGE_ZH: "松山湖管委会",
}


@dataclass(frozen=True)
class ExpectedStep:
    """One expected message of a scenario's negotiation sequence.

    Attributes:
        actor: Side that emits the message.
        performative: Expected performative (``PROPOSE``/``ACCEPT``/``REJECT``/``ABORT``), or
            ``MESSAGE`` for the plain end message that carries no Negotiation-T metadata.
        round: Expected negotiation round, or ``None`` when the message carries no negotiation
            context (the plain end message).
    """

    actor: Literal["server", "client"]
    performative: str
    round: int | None


@dataclass(frozen=True)
class Scenario:
    """One demonstration scenario.

    Attributes:
        name: Scenario name (also the CLI value).
        expected_kind: Negotiation kind the server opens (``information``/``target``/
            ``feasibility``).
        user_request: Client natural-language request per language.
        expected_sequence: Exact performative/round sequence from the design.
        expected_final_state: Terminal state the run must reach.
        scripted_client_decisions: Decisions returned by the offline scripted decision provider,
            in order.
        description: One-line business description.
    """

    name: str
    expected_kind: str
    user_request: Mapping[str, str]
    expected_sequence: tuple[ExpectedStep, ...]
    expected_final_state: FinalState
    scripted_client_decisions: tuple[NegotiationDecision, ...]
    description: str


SCENARIO_INFORMATION_SPEC = Scenario(
    name=SCENARIO_INFORMATION,
    expected_kind=KIND_INFORMATION,
    user_request={
        LANGUAGE_EN: NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
        LANGUAGE_ZH: NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    },
    expected_sequence=(
        ExpectedStep(actor="server", performative="PROPOSE", round=1),
        ExpectedStep(actor="client", performative="ACCEPT", round=1),
    ),
    expected_final_state=FINAL_STATE_TASK_COMPLETED,
    scripted_client_decisions=(
        NegotiationDecision(
            action="accept",
            rationale="The missing coverage region is supplied, so the energy-saving task can run.",
        ),
    ),
    description="Task-T misses the region; the client accepts the server's missing-parameter proposal.",
)

SCENARIO_TARGET_SPEC = Scenario(
    name=SCENARIO_TARGET,
    expected_kind=KIND_TARGET,
    user_request={
        LANGUAGE_EN: NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_EN,
        LANGUAGE_ZH: NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_ZH,
    },
    expected_sequence=(
        ExpectedStep(actor="server", performative="PROPOSE", round=1),
        ExpectedStep(actor="client", performative="PROPOSE", round=1),
        ExpectedStep(actor="server", performative="PROPOSE", round=2),
        ExpectedStep(actor="client", performative="ACCEPT", round=2),
    ),
    expected_final_state=FINAL_STATE_TASK_COMPLETED,
    scripted_client_decisions=(
        NegotiationDecision(
            action="counter",
            rationale="The first revision still overlaps the 17:30~23:00 segment.",
            counter_proposal=(
                "Move the extra capacity to a non-overlapping window: keep {17:30~20:00,20Mbps} "
                "and {20:00~00:00,10Mbps}."
            ),
        ),
        NegotiationDecision(
            action="accept",
            rationale="The revised plan has non-overlapping segments and meets the throughput target.",
        ),
    ),
    description="Overlapping throughput segments; the client counter-proposes once, then accepts the revision.",
)

SCENARIO_FEASIBILITY_SPEC = Scenario(
    name=SCENARIO_FEASIBILITY,
    expected_kind=KIND_FEASIBILITY,
    user_request={
        LANGUAGE_EN: NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_EN,
        LANGUAGE_ZH: NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_ZH,
    },
    expected_sequence=(
        ExpectedStep(actor="server", performative="PROPOSE", round=1),
        ExpectedStep(actor="client", performative="ACCEPT", round=1),
    ),
    expected_final_state=FINAL_STATE_TASK_COMPLETED,
    scripted_client_decisions=(
        NegotiationDecision(
            action="accept",
            rationale="The outage-aware alternative plan is feasible and keeps the SLA margin.",
        ),
    ),
    description="A guaranteed rate conflicts with a long power outage; the client accepts the alternative plan.",
)

SCENARIO_REJECT_SPEC = Scenario(
    name=SCENARIO_REJECT,
    expected_kind=KIND_INFORMATION,
    user_request={
        LANGUAGE_EN: NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
        LANGUAGE_ZH: NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    },
    expected_sequence=(
        ExpectedStep(actor="server", performative="PROPOSE", round=1),
        ExpectedStep(actor="client", performative="REJECT", round=1),
        ExpectedStep(actor="server", performative="MESSAGE", round=None),
    ),
    expected_final_state=FINAL_STATE_NEGOTIATION_ENDED,
    scripted_client_decisions=(
        NegotiationDecision(
            action="reject",
            rationale="The requested coverage region is not available from the client side.",
        ),
    ),
    description="The client cannot provide the missing content; the negotiation ends without a task.",
)

SCENARIO_ABORT_SPEC = Scenario(
    name=SCENARIO_ABORT,
    expected_kind=KIND_TARGET,
    user_request={
        LANGUAGE_EN: NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_EN,
        LANGUAGE_ZH: NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_ZH,
    },
    expected_sequence=(
        ExpectedStep(actor="server", performative="PROPOSE", round=1),
        ExpectedStep(actor="client", performative="PROPOSE", round=1),
        ExpectedStep(actor="server", performative="PROPOSE", round=2),
        ExpectedStep(actor="client", performative="PROPOSE", round=2),
        ExpectedStep(actor="server", performative="PROPOSE", round=3),
        ExpectedStep(actor="client", performative="PROPOSE", round=3),
        ExpectedStep(actor="server", performative="ABORT", round=3),
    ),
    expected_final_state=FINAL_STATE_NEGOTIATION_ABORTED,
    scripted_client_decisions=(
        NegotiationDecision(
            action="counter",
            rationale="The client insists on the original overlapping high-throughput window.",
            counter_proposal="Keep the {20:00~00:00,10Mbps} segment as originally requested.",
        ),
        NegotiationDecision(
            action="counter",
            rationale="The client still cannot accept dropping the overlapping window.",
            counter_proposal="Keep the {17:30~23:00,20Mbps} and {20:00~00:00,10Mbps} segments.",
        ),
        NegotiationDecision(
            action="counter",
            rationale="No acceptable compromise remains; the client repeats its position.",
            counter_proposal="Keep the full original segmented throughput target unchanged.",
        ),
    ),
    description="The client keeps counter-proposing an infeasible plan until the server aborts at maxRounds.",
)

#: All scenarios keyed by name, in design order.
SCENARIOS: dict[str, Scenario] = {
    SCENARIO_INFORMATION_SPEC.name: SCENARIO_INFORMATION_SPEC,
    SCENARIO_TARGET_SPEC.name: SCENARIO_TARGET_SPEC,
    SCENARIO_FEASIBILITY_SPEC.name: SCENARIO_FEASIBILITY_SPEC,
    SCENARIO_REJECT_SPEC.name: SCENARIO_REJECT_SPEC,
    SCENARIO_ABORT_SPEC.name: SCENARIO_ABORT_SPEC,
}

#: Supported scenario names, in design order.
SUPPORTED_SCENARIO_NAMES = tuple(SCENARIOS)


def get_scenario(name: str) -> Scenario:
    """Return the scenario with the given name.

    Raises:
        KeyError: when the name is not one of the five supported scenarios.
    """
    try:
        return SCENARIOS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown scenario '{name}'. Supported: {', '.join(SUPPORTED_SCENARIO_NAMES)}") from exc


def resolve_user_request(scenario: Scenario, language: str) -> str:
    """Return the scenario's natural-language request for one language."""
    if language not in scenario.user_request:
        raise KeyError(f"Scenario '{scenario.name}' has no request for language '{language}'")
    return scenario.user_request[language]
