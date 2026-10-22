"""Business scripts of the five RAN energy-saving demonstration scenarios.

Every scenario carries the client-side natural-language request (both languages), the expected
negotiation kind, the exact performative/round sequence from the design, the expected final state,
and the decisions a scripted (offline) client returns. Tests assert the sequence and the rounds
instead of log text.

The ``reject`` scenario reuses the missing-throughput request: the server opens an ``information``
negotiation and the client rejects because it cannot provide the missing content. The ``abort``
scenario reuses the overlapping-throughput ``target`` request and counter-proposes an
unacceptable schedule until the server aborts at ``maxRounds``.

The information request deliberately omits the time-segmented guaranteed throughput target
(``task_context``): the Task-T template marks that slot optional, so the client can still render
the prompt, while the server's caller schema marks it required and opens the information
negotiation. Omitting the region would instead fail the client's required-slot validation before
any A2A message is sent, because the template marks ``task_object`` required.
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

#: Chinese natural-language task request of the information negotiation: the region is present but
#: the time-segmented guaranteed throughput target is omitted. The Task-T template's
#: ``task_context`` slot is optional, so the client renders the prompt; the server's caller schema
#: requires ``guaranteedThroughputTarget`` and reports the missing-parameter content error.
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH = (
    "在松山湖管委会创建无线网络节能任务：基于任务对象和任务上下文，输出无线网络节能方案，"
    "达成任务目标中定义的无线网络节能目标，并按照预期输出中定义的格式返回无线网络节能结果；"
    "在满足任务上下文的前提下最大化节能。"
)

#: English natural-language task request of the information negotiation (throughput target
#: omitted, region present). The explicit task-description clause keeps the request rich enough for
#: a live model to fill the template's required ``task_description`` slot; the omitted slot remains
#: only the time-segmented guaranteed throughput target.
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN = (
    "Create a RAN energy saving task in the Songshanhu Administration Committee area. Based on the "
    "task object and task context, output a wireless network energy saving plan that achieves the "
    "energy saving goal defined in the task target and returns the energy saving result in the "
    "format defined in the expected output; maximize energy saving under the task context."
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

#: Task-T parameters the client refills on accept, per language. The base set is the complete,
#: non-overlapping request; the information kind uses it to supply the throughput target it omitted
#: on purpose, while the target and feasibility kinds override the guaranteed-throughput target with
#: the value their negotiation agrees on.
_ACCEPT_BASE_PARAMS: dict[str, dict[str, object]] = {
    LANGUAGE_EN: {
        "operationType": "create",
        "region": REGION_STRINGS[LANGUAGE_EN],
        "energySavingGoal": "Maximize energy saving under the condition that the task context is met.",
        "guaranteedThroughputTarget": (
            "{00:00~07:00,2Mbps}, {07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}"
        ),
    },
    LANGUAGE_ZH: {
        "operationType": "create",
        "region": REGION_STRINGS[LANGUAGE_ZH],
        "energySavingGoal": "在满足任务上下文的前提下最大化节能。",
        "guaranteedThroughputTarget": (
            "{00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}"
        ),
    },
}

#: Corrected guaranteed-throughput targets agreed by each negotiation kind, per language.
_ACCEPT_TARGET_OVERRIDES: dict[str, dict[str, str]] = {
    KIND_TARGET: {
        LANGUAGE_EN: (
            "{00:00~07:00,2Mbps}, {07:00~09:00,10Mbps}, {09:00~17:30,10Mbps}, "
            "{17:30~23:00,20Mbps}, {23:00~00:00,10Mbps}"
        ),
        LANGUAGE_ZH: (
            "{00:00~07:00,2Mbps}、{07:00~09:00,10Mbps}、{09:00~17:30,10Mbps}、"
            "{17:30~23:00,20Mbps}、{23:00~00:00,10Mbps}"
        ),
    },
    KIND_FEASIBILITY: {
        LANGUAGE_EN: "{00:00~06:00,2Mbps}, {06:00~18:00,2Mbps}",
        LANGUAGE_ZH: "{00:00~06:00,2Mbps}、{06:00~18:00,2Mbps}",
    },
}

#: Site-level context the feasibility accept carries in addition to the base parameters.
_ACCEPT_FEASIBILITY_CONTEXT: dict[str, dict[str, str]] = {
    LANGUAGE_EN: {
        "siteList": "Station 1,Station 2",
        "backgroundEvent": "power outage from 00:00 to 18:00",
    },
    LANGUAGE_ZH: {
        "siteList": "站点名称1,站点名称2",
        "backgroundEvent": "00:00~18:00 停电",
    },
}

#: Confirmed intent of the target negotiation accept, per language.
TARGET_ACCEPT_CONFIRMATIONS: dict[str, str] = {
    LANGUAGE_EN: (
        "The energy saving target is corrected to {00:00~07:00,2Mbps}, {07:00~09:00,10Mbps}, "
        "{09:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~00:00,10Mbps}."
    ),
    LANGUAGE_ZH: (
        "节能目标修正为{00:00~07:00,2Mbps}、{07:00~09:00,10Mbps}、{09:00~17:30,10Mbps}、"
        "{17:30~23:00,20Mbps}、{23:00~00:00,10Mbps}。"
    ),
}

#: Feasibility summary of the feasibility negotiation accept, per language.
FEASIBILITY_ACCEPT_SUMMARIES: dict[str, str] = {
    LANGUAGE_EN: (
        "Agreed to lower the guaranteed throughput target for 06:00~18:00 from 10Mbps to 2Mbps; "
        "the negotiation is confirmed as complete."
    ),
    LANGUAGE_ZH: "同意将06:00~18:00时段的速率保障目标由10Mbps下调至2Mbps，本次协商确认结束。",
}


def _localized(values: dict[str, str], language: str) -> str:
    """Return the value of one language, falling back to ``zh-CN``."""
    return values.get(language, values[LANGUAGE_ZH])


def accept_filled_params(kind: str, language: str = LANGUAGE_ZH) -> dict[str, object]:
    """Return the Task-T parameters the client refills when it accepts one proposal.

    Args:
        kind: Negotiation kind (``information`` / ``target`` / ``feasibility``).
        language: Process-level language selecting the localized values.

    Returns:
        A fresh parameter map with the kind's agreed guaranteed-throughput target (the information
        kind uses the base complete target) and the feasibility site context when applicable.
    """
    params = dict(_ACCEPT_BASE_PARAMS.get(language, _ACCEPT_BASE_PARAMS[LANGUAGE_ZH]))
    override = _ACCEPT_TARGET_OVERRIDES.get(kind)
    if override is not None:
        params["guaranteedThroughputTarget"] = _localized(override, language)
    if kind == KIND_FEASIBILITY:
        params.update(_ACCEPT_FEASIBILITY_CONTEXT.get(language, _ACCEPT_FEASIBILITY_CONTEXT[LANGUAGE_ZH]))
    return params


def accept_confirmation_text(kind: str, language: str = LANGUAGE_ZH) -> str | None:
    """Return the natural-language confirmation an accept of ``kind`` carries, when it has one.

    Args:
        kind: Negotiation kind (``information`` / ``target`` / ``feasibility``).
        language: Process-level language selecting the localized text.

    Returns:
        The corrected target (``target``) or the agreed feasibility summary (``feasibility``);
        ``None`` for the information kind, whose accept lists the filled parameters instead.
    """
    if kind == KIND_TARGET:
        return _localized(TARGET_ACCEPT_CONFIRMATIONS, language)
    if kind == KIND_FEASIBILITY:
        return _localized(FEASIBILITY_ACCEPT_SUMMARIES, language)
    return None


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
            rationale=(
                "The missing time-segmented guaranteed throughput target is supplied, so the "
                "energy-saving task can run."
            ),
        ),
    ),
    description=(
        "Task-T misses the guaranteed throughput target; the client accepts the server's "
        "missing-parameter proposal."
    ),
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
            rationale="The requested guaranteed throughput target is not available from the client side.",
        ),
    ),
    description=(
        "The client cannot provide the missing throughput target; the negotiation ends without a "
        "task."
    ),
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
