"""Offline tests of the negotiation scenario plumbing of the ran-energy-saving demo.

Covers the scripted mock LLM's variant routing (slot extraction by the scenario input, content
validation by the region display string in the message head), the scenario data of the
``negotiation`` variant (missing-region inputs, filled params, accept answers), and the
client-side feasibility of generating the missing-region task prompt with the A2A-T SDK.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
RESOURCES_DIR = PROJECT_ROOT / "resources" / "mock_llm"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from client import scenario_data
from client.scenario_data import (
    ENERGY_SAVING_TEMPLATE_URI,
    NATURAL_LANGUAGE_PROMPT_INPUT_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_ZH,
    NEGOTIATION_REGION_STRINGS,
    SCENARIO_DEFAULT,
    SCENARIO_ENV_KEY,
    SCENARIO_NEGOTIATION,
    SUPPORTED_SCENARIOS,
    build_prompt_input,
    negotiation_answers,
    negotiation_filled_params,
    resolve_scenario,
)
from common import mock_llm
from common.llm_logger import install_llm_logger
from common.mock_llm import llm_calls, route_structured_call

#: An offline ``.env`` (no API key) written per test; the mock force-installs against it.
_OFFLINE_ENV_TEXT = "A2AT_LANGUAGE={language}\nA2AT_LLM_PROVIDER=openai\nA2AT_LLM_MODEL=mock-llm\nA2AT_LLM_API_KEY=\n"

#: The region slot of each language's packaged slot schema (the slot the negotiation asks for).
_REGION_SLOT_NAMES = {"en-US": "task_object", "zh-CN": "任务对象"}

#: Region placeholder of the missing-variant canned response per language. The packaged slot
#: schema requires the region slot, so the non-blank placeholder keeps the real client
#: generation path working while the region is genuinely unknown; it carries no region display
#: string, so the content-validation routing is unaffected.
_REGION_PLACEHOLDERS = {"en-US": "(to be provided)", "zh-CN": "（待提供）"}


def _write_offline_env(directory: Path, language: str) -> Path:
    """Write the offline ``.env`` of one language and return its path."""
    env_path = directory / ".env"
    env_path.write_text(_OFFLINE_ENV_TEXT.format(language=language), encoding="utf-8")
    return env_path


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force-install the scripted mock LLM (en-US) for every test, regardless of any real ``.env``."""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.delenv(SCENARIO_ENV_KEY, raising=False)
    env_path = _write_offline_env(tmp_path, "en-US")
    mock_llm.install_mock_llm(env_path=env_path)
    install_llm_logger(role="test", verbose=False)
    return env_path


def _slot_extraction_schema() -> dict[str, object]:
    """Output-schema signature of a Task-T slot extraction call."""
    return {
        "type": "object",
        "properties": {"slots": {"type": "object"}, "slot_errors": {"type": "array"}},
    }


def _content_validation_schema() -> dict[str, object]:
    """Output-schema signature of a content semantic validation call."""
    return {
        "type": "object",
        "properties": {
            "semantic_verdict": {"type": "boolean"},
            "errors": {"type": "array"},
            "params": {"type": "object"},
        },
    }


def _slot_extraction_messages(user_input: str) -> list[dict[str, str]]:
    """User-message layout of a real slot extraction call (``[input]`` block + ``[slots]`` tail)."""
    return [
        {"role": "system", "content": "slot extraction system prompt"},
        {
            "role": "user",
            "content": (
                "[user_prompt]\ninstruction text\n\n"
                f"[scenario_code]\n{ENERGY_SAVING_TEMPLATE_URI}\n\n"
                "[language]\nen-US\n\n"
                f"[input]\n{user_input}\n\n"
                '[slots]\n- name: task_object\n  required: true\n  example: Area: "'
                + NEGOTIATION_REGION_STRINGS["en-US"]
                + '"'
            ),
        },
    ]


def _content_validation_messages(
    prompt_text: str, template_content: str, *, template_content_label: str = "Template Content: "
) -> list[dict[str, str]]:
    """User-message layout of a real content validation call (head + template-content tail).

    ``template_content_label`` is the localized label starting the template-content block; the
    real zh-CN SDK template uses the full-width ``模板正文：`` with no trailing space.
    """
    return [
        {"role": "system", "content": "content validation system prompt"},
        {
            "role": "user",
            "content": (
                "Extension Name: Task-T\n"
                f"Input Content: {prompt_text}\n"
                f"Template URI: {ENERGY_SAVING_TEMPLATE_URI}\n"
                f"{template_content_label}{template_content}\n"
                "Parameter Schema: {}"
            ),
        },
    ]


#: Template tail carrying the region example, as the packaged ran-energy-saving template does.
_REGION_TEMPLATE_TAIL = (
    "## Task Object\n\nRequirement:\n\n1. Area name: (required)\n    - Must be a concrete and "
    f'physically real community name such as "{NEGOTIATION_REGION_STRINGS["en-US"]}"'
)

#: Collapse-rendered round-1 prompt: the Task Object slot carries the region placeholder, so the
#: rendered text carries no region display string.
_MISSING_REGION_PROMPT = (
    f"## Operation Type\n\nCreate\n\n## Task Object\n\n{_REGION_PLACEHOLDERS['en-US']}"
    "\n\n## Task Target\n\nMaximize energy saving."
)

#: Collapse-rendered round-2 prompt with the region slot filled.
_FILLED_REGION_PROMPT = (
    f"## Operation Type\n\nCreate\n\n## Task Object\n\nThe area is the "
    f"{NEGOTIATION_REGION_STRINGS['en-US']}.\n\n## Task Target\n\nMaximize energy saving."
)


# --- mock routing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_input", "expected_stage"),
    [
        (NATURAL_LANGUAGE_PROMPT_INPUT_EN, mock_llm.STAGE_SLOT_EXTRACTION),
        (NATURAL_LANGUAGE_PROMPT_INPUT_ZH, mock_llm.STAGE_SLOT_EXTRACTION),
        (scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN, mock_llm.STAGE_SLOT_EXTRACTION_MISSING),
        (scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH, mock_llm.STAGE_SLOT_EXTRACTION_MISSING),
    ],
    ids=["complete-en", "complete-zh", "missing-en", "missing-zh"],
)
def test_slot_extraction_routing_selects_the_variant_by_the_scenario_input(
    user_input: str, expected_stage: str
) -> None:
    messages = _slot_extraction_messages(user_input)

    assert route_structured_call(messages, _slot_extraction_schema()) == expected_stage


def test_slot_extraction_routing_defaults_to_the_complete_variant_without_the_scenario_input() -> None:
    """A from-data extraction (e.g. the round-2 filled-params regeneration) keeps the complete variant."""
    messages = _slot_extraction_messages('{"operationType": "create", "region": "Songshanhu Administration Committee"}')

    assert route_structured_call(messages, _slot_extraction_schema()) == mock_llm.STAGE_SLOT_EXTRACTION


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_content_validation_routing_selects_the_variant_by_the_region_string(tmp_path: Path, language: str) -> None:
    mock_llm.install_mock_llm(env_path=_write_offline_env(tmp_path, language))
    region_string = NEGOTIATION_REGION_STRINGS[language]
    filled = _content_validation_messages(f"## Task Object\n\nThe area is the {region_string}.", _REGION_TEMPLATE_TAIL)
    missing = _content_validation_messages(_MISSING_REGION_PROMPT, _REGION_TEMPLATE_TAIL)

    assert route_structured_call(filled, _content_validation_schema()) == mock_llm.STAGE_CONTENT_VALIDATION
    assert route_structured_call(missing, _content_validation_schema()) == mock_llm.STAGE_CONTENT_VALIDATION_MISSING


@pytest.mark.parametrize("template_content_label", ["Template Content: ", "模板正文："], ids=["en-label", "zh-label"])
def test_content_validation_routing_ignores_the_region_string_in_the_template_tail(
    template_content_label: str,
) -> None:
    """A region example inside the template content must not flip the missing-params variant."""
    messages = _content_validation_messages(
        _MISSING_REGION_PROMPT, _REGION_TEMPLATE_TAIL, template_content_label=template_content_label
    )

    assert route_structured_call(messages, _content_validation_schema()) == mock_llm.STAGE_CONTENT_VALIDATION_MISSING


def test_route_structured_call_rejects_unmapped_calls() -> None:
    messages = [{"role": "user", "content": "unrelated"}]
    json_schema = {"type": "object", "properties": {"unexpected": {"type": "string"}}}

    with pytest.raises(ValueError, match="cannot route"):
        route_structured_call(messages, json_schema)


def test_negotiation_variant_call_sequence_is_pinned() -> None:
    """slot-extraction(missing input) -> content-validation(missing prompt) -> content-validation(filled)."""
    mock_llm.reset_call_log()

    mock_llm.get_routed_response(
        _slot_extraction_messages(scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN), _slot_extraction_schema()
    )
    mock_llm.get_routed_response(
        _content_validation_messages(_MISSING_REGION_PROMPT, _REGION_TEMPLATE_TAIL), _content_validation_schema()
    )
    mock_llm.get_routed_response(
        _content_validation_messages(_FILLED_REGION_PROMPT, _REGION_TEMPLATE_TAIL), _content_validation_schema()
    )

    assert llm_calls == [
        mock_llm.STAGE_SLOT_EXTRACTION_MISSING,
        mock_llm.STAGE_CONTENT_VALIDATION_MISSING,
        mock_llm.STAGE_CONTENT_VALIDATION,
    ]


# --- mock resources ------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_missing_mock_resources_mirror_the_complete_variants(language: str) -> None:
    region_slot = _REGION_SLOT_NAMES[language]
    complete_slots = json.loads((RESOURCES_DIR / language / "slot_extraction.json").read_text(encoding="utf-8"))
    missing_slots = json.loads((RESOURCES_DIR / language / "slot_extraction_missing.json").read_text(encoding="utf-8"))
    complete_validation = json.loads((RESOURCES_DIR / language / "content_validation.json").read_text(encoding="utf-8"))
    missing_validation = json.loads(
        (RESOURCES_DIR / language / "content_validation_missing.json").read_text(encoding="utf-8")
    )

    # The region slot carries the region placeholder (the packaged slot schema requires the slot,
    # so client generation must succeed with the region genuinely unknown); every other slot
    # stays unchanged and no slot error is reported.
    assert missing_slots["slots"][region_slot] == _REGION_PLACEHOLDERS[language]
    assert _REGION_PLACEHOLDERS[language] not in json.dumps(complete_slots["slots"], ensure_ascii=False)
    # The placeholder carries no region display string in any language.
    assert not any(region in _REGION_PLACEHOLDERS[language] for region in NEGOTIATION_REGION_STRINGS.values())
    assert missing_slots["slot_errors"] == []
    assert {name: value for name, value in complete_slots["slots"].items() if name != region_slot} == {
        name: value for name, value in missing_slots["slots"].items() if name != region_slot
    }
    # The content validation reports the missing region param over the four schema params.
    assert missing_validation["semantic_verdict"] is False
    assert missing_validation["errors"] == [
        {"slot_name": "region", "code": "content.param_missing", "facts": {"section_label": region_slot}}
    ]
    assert missing_validation["params"] == {**complete_validation["params"], "region": None}


# --- scenario data -------------------------------------------------------------------------


def test_supported_scenarios_constant() -> None:
    assert SUPPORTED_SCENARIOS == (SCENARIO_DEFAULT, SCENARIO_NEGOTIATION)


def test_resolve_scenario_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env next to the cwd

    # No .env and no OS environment entry -> default.
    assert resolve_scenario() == SCENARIO_DEFAULT

    # Unknown value -> default.
    env_path = tmp_path / ".env"
    env_path.write_text("A2AT_SAMPLE_SCENARIO=bogus\n", encoding="utf-8")
    assert resolve_scenario(env_path=env_path) == SCENARIO_DEFAULT

    # Known value -> itself.
    env_path.write_text("A2AT_SAMPLE_SCENARIO=negotiation\n", encoding="utf-8")
    assert resolve_scenario(env_path=env_path) == SCENARIO_NEGOTIATION

    # OS environment fallback when the .env does not carry the key.
    env_path.write_text("A2AT_LANGUAGE=en-US\n", encoding="utf-8")
    monkeypatch.setenv(SCENARIO_ENV_KEY, SCENARIO_NEGOTIATION)
    assert resolve_scenario(env_path=env_path) == SCENARIO_NEGOTIATION

    # The .env value wins over the OS environment.
    env_path.write_text("A2AT_SAMPLE_SCENARIO=default\n", encoding="utf-8")
    assert resolve_scenario(env_path=env_path) == SCENARIO_DEFAULT


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_build_prompt_input_negotiation_returns_the_missing_region_input(tmp_path: Path, language: str) -> None:
    env_path = _write_offline_env(tmp_path, language)
    complete = {
        "en-US": NATURAL_LANGUAGE_PROMPT_INPUT_EN,
        "zh-CN": NATURAL_LANGUAGE_PROMPT_INPUT_ZH,
    }[language]
    missing = {
        "en-US": scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
        "zh-CN": scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    }[language]

    assert build_prompt_input(env_path=env_path) == complete
    assert build_prompt_input(env_path=env_path, scenario=SCENARIO_NEGOTIATION) == missing
    assert build_prompt_input(env_path=env_path, scenario=resolve_scenario(env_path=env_path)) == complete
    # The missing input omits the region display string; the complete one carries it.
    assert NEGOTIATION_REGION_STRINGS[language] not in missing
    assert NEGOTIATION_REGION_STRINGS[language] in complete


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_negotiation_filled_params_and_answers_are_region_filled(tmp_path: Path, language: str) -> None:
    env_path = _write_offline_env(tmp_path, language)

    params = negotiation_filled_params(env_path=env_path)
    answers = negotiation_answers(env_path=env_path)

    assert set(params) == {"operationType", "region", "energySavingGoal", "guaranteedThroughputTarget"}
    assert params["region"] == NEGOTIATION_REGION_STRINGS[language]
    assert answers == {"region": NEGOTIATION_REGION_STRINGS[language]}
    # The filled params mirror the complete content_validation resource of the language.
    resource_params = json.loads((RESOURCES_DIR / language / "content_validation.json").read_text(encoding="utf-8"))[
        "params"
    ]
    assert params == resource_params


# --- client-side feasibility ---------------------------------------------------------------


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_negotiation_client_generation_renders_the_missing_region_prompt(tmp_path: Path, language: str) -> None:
    """generate_task_prompt_from_text renders the missing-region prompt through the real client path."""
    from a2a_t.client.a2at_client import A2ATClient

    env_path = _write_offline_env(tmp_path, language)
    mock_llm.install_mock_llm(env_path=env_path)
    missing_input = {
        "en-US": scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
        "zh-CN": scenario_data.NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    }[language]

    client = A2ATClient(env_path=env_path)
    prompt = client.generate_task_prompt_from_text(missing_input, ENERGY_SAVING_TEMPLATE_URI)

    # The required region slot renders as the region placeholder, never as a region display
    # string, so the server-side content validation routes to the missing variant.
    assert NEGOTIATION_REGION_STRINGS[language] not in prompt.prompt_text
    assert _REGION_PLACEHOLDERS[language] in prompt.prompt_text
