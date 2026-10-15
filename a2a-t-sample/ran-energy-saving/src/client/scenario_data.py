"""Sample scenario input data for the ran-energy-saving e2e flow.

The client uses the template-directed ``generate_task_prompt_from_text`` API against the
``Task-T/network-layer/ran-energy-saving/v1`` template, so no scenario recognition is needed:
the natural-language input below directly drives the slot extraction.

The prompt input is selected by the configured language (``A2AT_LANGUAGE`` in ``.env``), so the
input language always matches the mock/template language tree (``zh-CN`` / ``en-US``), and by
the sample scenario (``A2AT_SAMPLE_SCENARIO``): the ``default`` scenario keeps the existing
complete input, while the ``negotiation`` scenario uses the missing-region input that drives the
information-negotiation variant (the client input omits the region, the server opens the
negotiation, the client answers with ``negotiation_answers``).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

#: Task-T template addressed by this case.
ENERGY_SAVING_TEMPLATE_URI = "Task-T/network-layer/ran-energy-saving/v1"

#: Sample scenario variants selected by ``A2AT_SAMPLE_SCENARIO``.
SCENARIO_DEFAULT = "default"
SCENARIO_NEGOTIATION = "negotiation"
SUPPORTED_SCENARIOS = (SCENARIO_DEFAULT, SCENARIO_NEGOTIATION)

#: ``.env`` key selecting the sample scenario variant.
SCENARIO_ENV_KEY = "A2AT_SAMPLE_SCENARIO"

#: Chinese natural-language task request used for prompt generation (UC1).
NATURAL_LANGUAGE_PROMPT_INPUT_ZH = (
    "在松山湖管委会创建无线网络节能任务：在满足任务上下文的前提下最大化节能；"
    "分时段保障速率目标为 {00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、"
    "{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}。"
)

#: English natural-language task request used for prompt generation (UC1).
NATURAL_LANGUAGE_PROMPT_INPUT_EN = (
    "Create a RAN energy saving task in the Songshanhu Administration Committee area to maximize "
    "energy saving. The time-segment-based guaranteed throughput target is {00:00~07:00,2Mbps}, "
    "{07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}."
)

#: Chinese natural-language task request of the negotiation scenario: the region phrase is
#: omitted, so the mock extracts the region placeholder for the required Task Object slot
#: (resources/mock_llm/<lang>/slot_extraction_missing.json) and the server opens the negotiation.
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH = (
    "创建无线网络节能任务：在满足任务上下文的前提下最大化节能；"
    "分时段保障速率目标为 {00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、"
    "{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}。"
)

#: English natural-language task request of the negotiation scenario (region omitted).
NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN = (
    "Create a RAN energy saving task to maximize energy saving. The time-segment-based "
    "guaranteed throughput target is {00:00~07:00,2Mbps}, {07:00~17:30,10Mbps}, "
    "{17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}."
)

#: Region display string per language; the scripted mock LLM routes the content-validation
#: variant on its presence in the user-message head, and the negotiation accept answers carry it.
NEGOTIATION_REGION_STRINGS: dict[str, str] = {
    "en-US": "Songshanhu Administration Committee",
    "zh-CN": "松山湖管委会",
}

#: Filled Task-T parameters of the negotiation scenario per language: the four TASK_PARAM_SCHEMA
#: params with the region resolved. Mirrors the ``params`` of the complete
#: ``resources/mock_llm/<lang>/content_validation.json`` of each language (the test suite pins the
#: equality, so the two cannot drift).
_NEGOTIATION_FILLED_PARAMS: dict[str, dict[str, object]] = {
    "en-US": {
        "operationType": "create",
        "region": NEGOTIATION_REGION_STRINGS["en-US"],
        "energySavingGoal": "Maximize energy saving under the condition that the task context is met.",
        "guaranteedThroughputTarget": (
            "{00:00~07:00,2Mbps}, {07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}"
        ),
    },
    "zh-CN": {
        "operationType": "create",
        "region": NEGOTIATION_REGION_STRINGS["zh-CN"],
        "energySavingGoal": "在满足任务上下文的前提下最大化节能。",
        "guaranteedThroughputTarget": (
            "{00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}"
        ),
    },
}


def resolve_language(*, env_path: Path | None = None) -> str:
    """Resolve the configured ``A2AT_LANGUAGE`` (defaults to ``zh-CN``)."""
    resolved = env_path or Path.cwd() / ".env"
    values = dotenv_values(resolved) if resolved.exists() else {}
    return str(values.get("A2AT_LANGUAGE", "")).strip() or "zh-CN"


def resolve_scenario(*, env_path: Path | None = None) -> str:
    """Resolve the configured ``A2AT_SAMPLE_SCENARIO`` (defaults to ``default``).

    The ``.env`` value wins over the OS environment (the sample's ``resolve_server_url``
    convention); an unset or unknown value resolves to the ``default`` scenario.
    """
    resolved = env_path or Path.cwd() / ".env"
    values = dotenv_values(resolved) if resolved.exists() else {}
    raw_value = str(values.get(SCENARIO_ENV_KEY, "")).strip() or os.environ.get(SCENARIO_ENV_KEY, "").strip()
    return raw_value if raw_value in SUPPORTED_SCENARIOS else SCENARIO_DEFAULT


def build_prompt_input(*, env_path: Path | None = None, scenario: str | None = None) -> str:
    """Return the prompt generation input matching the configured language and scenario.

    The explicit ``scenario`` override beats ``resolve_scenario``; an unknown scenario falls back
    to the ``default`` variant like ``resolve_scenario`` does.
    """
    selected_scenario = scenario if scenario is not None else resolve_scenario(env_path=env_path)
    if selected_scenario == SCENARIO_NEGOTIATION and resolve_language(env_path=env_path) == "en-US":
        return NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN
    if selected_scenario == SCENARIO_NEGOTIATION:
        return NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH
    if resolve_language(env_path=env_path) == "en-US":
        return NATURAL_LANGUAGE_PROMPT_INPUT_EN
    return NATURAL_LANGUAGE_PROMPT_INPUT_ZH


def negotiation_filled_params(*, env_path: Path | None = None) -> dict[str, object]:
    """Return the four Task-T parameters with the region filled (negotiation round 2 input)."""
    language = resolve_language(env_path=env_path)
    filled = _NEGOTIATION_FILLED_PARAMS.get(language, _NEGOTIATION_FILLED_PARAMS["zh-CN"])
    return dict(filled)


def negotiation_answers(*, env_path: Path | None = None) -> dict[str, str]:
    """Return the negotiation accept answers: the region value of the resolved language."""
    language = resolve_language(env_path=env_path)
    return {"region": NEGOTIATION_REGION_STRINGS.get(language, NEGOTIATION_REGION_STRINGS["zh-CN"])}


def build_task_request() -> dict[str, object]:
    """Build the sample Task-T request input (scenario name + agent query)."""
    return {
        # The "scenario" key is the request LABEL ("create ran energy saving task") rendered as
        # the message text part — unrelated to the A2AT_SAMPLE_SCENARIO variant switch above.
        "scenario": "create ran energy saving task",
        "agent_card_query": {
            "name": "RAN Domain Agent",
            "organization": "Huawei",
        },
    }
