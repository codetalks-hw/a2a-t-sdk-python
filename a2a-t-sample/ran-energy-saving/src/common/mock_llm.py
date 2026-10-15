"""Schema-routed scripted mock LLM for the offline Task-T RAN energy saving demo.

In this demo the client calls
``generate_task_prompt_from_text`` (template-directed slot extraction) and the server calls
``validate_task_prompt_and_data_filling`` (content semantic validation). Both steps issue
structured LLM calls, so the mock routes every call by the **output schema signature** instead
of relying on call order — the two processes (client / server) each start their own call
sequence at zero, so a sequenced mock would serve the wrong response.

=====================================  ==========================================
schema signature                      served response
=====================================  ==========================================
``slots`` + ``slot_errors``           Task-T slot extraction — the ``slot_extraction``
                                      complete variant, or the ``slot_extraction_missing``
                                      variant when the first user message carries the
                                      missing-region scenario input (told apart by the
                                      scenario natural-language constants of
                                      ``client.scenario_data``)
``semantic_verdict`` property         content semantic validation — the
                                      ``content_validation`` complete variant, or the
                                      ``content_validation_missing`` variant when the region
                                      display string is absent from the message head cut at
                                      the template-content labels
=====================================  ==========================================

The response language is read from ``A2AT_LANGUAGE`` in the ``.env`` file (``zh-CN`` / ``en-US``);
both language trees live under ``resources/mock_llm/``. When ``A2AT_LLM_API_KEY`` is empty
the mock injects a placeholder API key so the OpenAI client can be constructed without network
access. A missing ``.env`` is a hard error (copy ``env.example`` to ``.env`` first).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from a2a_t.config.errors import ConfigFileNotFoundError
from a2a_t.llm.models import LLMResponse
from client.scenario_data import (
    NATURAL_LANGUAGE_PROMPT_INPUT_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    NATURAL_LANGUAGE_PROMPT_INPUT_ZH,
    NEGOTIATION_REGION_STRINGS,
)
from dotenv import dotenv_values

_RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources" / "mock_llm"

#: Routed stage names (``llm_calls`` entries). The complete variants keep their historical names.
STAGE_SLOT_EXTRACTION = "slot_extraction"
STAGE_SLOT_EXTRACTION_MISSING = "slot_extraction_missing"
STAGE_CONTENT_VALIDATION = "content_validation"
STAGE_CONTENT_VALIDATION_MISSING = "content_validation_missing"

#: Stage name -> response file name.
_STAGE_FILES: dict[str, str] = {
    STAGE_SLOT_EXTRACTION: "slot_extraction.json",
    STAGE_SLOT_EXTRACTION_MISSING: "slot_extraction_missing.json",
    STAGE_CONTENT_VALIDATION: "content_validation.json",
    STAGE_CONTENT_VALIDATION_MISSING: "content_validation_missing.json",
}

#: Missing-region scenario inputs that flip the slot-extraction routing to the missing variant.
_MISSING_SCENARIO_INPUTS = (
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
)

#: Complete scenario inputs; checked before the missing ones because the missing zh input is a
#: substring of the complete zh input.
_COMPLETE_SCENARIO_INPUTS = (NATURAL_LANGUAGE_PROMPT_INPUT_EN, NATURAL_LANGUAGE_PROMPT_INPUT_ZH)

#: Localized labels starting the template-content block of the content validation user prompt.
_TEMPLATE_CONTENT_LABELS = ("Template Content:", "模板正文：")

#: Placeholder API key injected while the mock is installed (no network access happens).
_PLACEHOLDER_API_KEY = "mock-key-not-real"

#: Language the canned script was loaded for (region-string routing reads it).
_script_language = "zh-CN"

#: Routed stage name of every served structured call (test seam).
llm_calls: list[str] = []

_mock_enabled = False
_script: dict[str, str] = {}


def resolve_language(*, env_path: Path | None = None) -> str:
    """Resolve the configured ``A2AT_LANGUAGE`` (defaults to ``zh-CN``).

    Raises:
        ConfigFileNotFoundError: when the resolved ``.env`` file does not exist.
    """
    resolved = env_path or Path.cwd() / ".env"
    if not resolved.exists():
        raise ConfigFileNotFoundError(resolved)
    values = dotenv_values(resolved)
    return str(values.get("A2AT_LANGUAGE", "")).strip() or "zh-CN"


def is_mock_needed(*, env_path: Path | None = None) -> bool:
    """Check whether the LLM API key is missing/empty and the mock fallback is needed."""
    resolved = env_path or Path.cwd() / ".env"
    values = dotenv_values(resolved) if resolved.exists() else {}
    return not str(values.get("A2AT_LLM_API_KEY", "")).strip()


def is_mock_enabled() -> bool:
    """Return ``True`` once the mock LLM has been installed."""
    return _mock_enabled


def reset_call_log() -> None:
    """Clear the routed stage log (test seam)."""
    llm_calls.clear()


def _load_script(language: str) -> dict[str, str]:
    language_dir = _RESOURCES_DIR / language
    if not language_dir.exists():
        raise FileNotFoundError(f"Mock responses not found for language: {language} (expected at {language_dir})")
    script: dict[str, str] = {}
    for stage, file_name in _STAGE_FILES.items():
        with (language_dir / file_name).open(encoding="utf-8") as handle:
            script[stage] = json.dumps(json.load(handle), ensure_ascii=False)
    return script


def _head_before_markers(content: str, markers: tuple[str, ...]) -> str:
    """Return the leading part of one message, cut at the earliest of the given markers.

    The input block of every routed user message precedes the template content, so the head
    carries the caller input alone — a region display string inside a template example can never
    leak from the template content into the head.
    """
    cut = len(content)
    for marker in markers:
        index = content.find(marker)
        if 0 <= index < cut:
            cut = index
    return content[:cut]


def _first_user_content(messages: list[dict[str, str]]) -> str:
    """Return the content of the first user message of one structured call."""
    return next(message["content"] for message in messages if message.get("role") == "user")


def route_structured_call(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any] | None = None,
) -> str:
    """Return the stage name one structured call is routed to, keyed on its output schema.

    The schema signature picks the stage family; the scenario variant is told apart from the
    user message: slot extraction by the scenario natural-language input it carries, content
    validation by the presence of the region display string in the message head (cut at the
    template-content labels, so template examples never flip the variant).

    Raises:
        ValueError: when no signature matches the call (an unmapped SDK call).
    """
    user_content = _first_user_content(messages)
    properties = json_schema.get("properties") if isinstance(json_schema, dict) else None
    property_names = set(properties) if isinstance(properties, dict) else set()

    if "slots" in property_names and "slot_errors" in property_names:
        if any(scenario_input in user_content for scenario_input in _COMPLETE_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION
        if any(scenario_input in user_content for scenario_input in _MISSING_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION_MISSING
        return STAGE_SLOT_EXTRACTION
    if "semantic_verdict" in property_names:
        region_string = NEGOTIATION_REGION_STRINGS.get(_script_language, "")
        head = _head_before_markers(user_content, _TEMPLATE_CONTENT_LABELS)
        if region_string and region_string not in head:
            return STAGE_CONTENT_VALIDATION_MISSING
        return STAGE_CONTENT_VALIDATION
    raise ValueError(f"The scripted mock LLM cannot route this structured call: {sorted(property_names)}")


def get_routed_response(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any] | None,
) -> LLMResponse:
    """Serve the canned response routed by the output schema of one structured call."""
    stage = route_structured_call(messages, json_schema)
    llm_calls.append(stage)
    return LLMResponse(
        content=_script[stage],
        model="mock-llm",
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        metadata={},
    )


def get_mock_payload(
    *,
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """Build the payload shape logged for a mock request."""
    payload: dict[str, Any] = {
        "model": "mock-llm",
        "messages": messages,
        "json_schema": json_schema,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def install_mock_llm(*, env_path: Path | None = None) -> None:
    """Load the canned responses of the configured language and patch the config source offline.

    Raises:
        ConfigFileNotFoundError: when the ``.env`` file does not exist.
        FileNotFoundError: when the response tree of the configured language does not exist.
    """
    global _mock_enabled, _script, _script_language
    reset_call_log()
    _script_language = resolve_language(env_path=env_path)
    _script = _load_script(_script_language)
    _mock_enabled = True

    from a2a_t.config.source import DotEnvConfigSource

    original_source_load = DotEnvConfigSource.load

    def _patched_source_load(path: Path) -> dict[str, str]:
        values: dict[str, str] = dict(original_source_load(path))
        if not str(values.get("A2AT_LLM_API_KEY", "")).strip():
            values["A2AT_LLM_API_KEY"] = _PLACEHOLDER_API_KEY
        return values

    DotEnvConfigSource.load = staticmethod(_patched_source_load)  # type: ignore[method-assign]


def install_mock_llm_if_needed(*, env_path: Path | None = None) -> bool:
    """Install the mock LLM when the API key is missing; returns whether it was installed."""
    if not is_mock_needed(env_path=env_path):
        return False
    install_mock_llm(env_path=env_path)
    print("[mock-llm] A2AT_LLM_API_KEY not set, using scripted mock LLM responses")
    return True
