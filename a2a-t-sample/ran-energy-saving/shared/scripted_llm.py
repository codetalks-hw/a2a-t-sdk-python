"""Scripted ``LLMClient`` provider for offline and test runs.

The provider is registered with the official SDK factory as ``scripted`` and serves the canned
responses under ``resources/mock_llm/<language>/``. Calls are routed by the **output schema
signature** plus a marker from the user message — never by call order, because the client and the
server processes each start their own call sequence at zero and a sequenced script would serve the
wrong response.

The routing rules and the canned content are the ones of the legacy sample mock, adapted to run as
a regular provider: no SDK method is monkey-patched and no module outside the sample root is
imported. Use :func:`register_scripted_provider` before constructing ``A2ATClient`` /
``A2ATServer``; the registration is idempotent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from a2a_t.llm.factory import LLMClientFactory
from a2a_t.llm.models import LLMClientConfig, LLMResponse
from dotenv import dotenv_values

from shared.scenario import (
    LANGUAGE_ZH,
    NATURAL_LANGUAGE_PROMPT_INPUT_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_ZH,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
    NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_ZH,
    NATURAL_LANGUAGE_PROMPT_INPUT_ZH,
    REGION_STRINGS,
)

#: Provider name registered with ``LLMClientFactory``.
SCRIPTED_PROVIDER_NAME = "scripted"

#: Default response tree, relative to the sample root.
_RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources" / "mock_llm"

#: Routed stage names (``llm_calls`` entries). The complete variants keep their historical names.
STAGE_SLOT_EXTRACTION = "slot_extraction"
STAGE_SLOT_EXTRACTION_MISSING = "slot_extraction_missing"
STAGE_SLOT_EXTRACTION_TARGET = "slot_extraction_target"
STAGE_SLOT_EXTRACTION_FEASIBILITY = "slot_extraction_feasibility"
STAGE_SLOT_EXTRACTION_TARGET_REFILL = "slot_extraction_target_refill"
STAGE_SLOT_EXTRACTION_FEASIBILITY_REFILL = "slot_extraction_feasibility_refill"
STAGE_CONTENT_VALIDATION = "content_validation"
STAGE_CONTENT_VALIDATION_MISSING = "content_validation_missing"
STAGE_CONTENT_VALIDATION_TARGET = "content_validation_target"
STAGE_CONTENT_VALIDATION_FEASIBILITY = "content_validation_feasibility"
STAGE_CONTENT_VALIDATION_TARGET_REFILL = "content_validation_target_refill"
STAGE_CONTENT_VALIDATION_FEASIBILITY_REFILL = "content_validation_feasibility_refill"
STAGE_NEGOTIATION_PROPOSE_EXTRACTION = "negotiation_propose_extraction"
STAGE_NEGOTIATION_ACCEPT_EXTRACTION = "negotiation_accept_extraction"
STAGE_NEGOTIATION_TARGET_PROPOSE_EXTRACTION = "negotiation_target_propose_extraction"
STAGE_NEGOTIATION_TARGET_ACCEPT_EXTRACTION = "negotiation_target_accept_extraction"
STAGE_NEGOTIATION_FEASIBILITY_PROPOSE_EXTRACTION = "negotiation_feasibility_propose_extraction"
STAGE_NEGOTIATION_FEASIBILITY_ACCEPT_EXTRACTION = "negotiation_feasibility_accept_extraction"

#: Stage name -> response file name.
_STAGE_FILES: dict[str, str] = {
    STAGE_SLOT_EXTRACTION: "slot_extraction.json",
    STAGE_SLOT_EXTRACTION_MISSING: "slot_extraction_missing.json",
    STAGE_SLOT_EXTRACTION_TARGET: "slot_extraction_target.json",
    STAGE_SLOT_EXTRACTION_FEASIBILITY: "slot_extraction_feasibility.json",
    STAGE_SLOT_EXTRACTION_TARGET_REFILL: "slot_extraction_target_refill.json",
    STAGE_SLOT_EXTRACTION_FEASIBILITY_REFILL: "slot_extraction_feasibility_refill.json",
    STAGE_CONTENT_VALIDATION: "content_validation.json",
    STAGE_CONTENT_VALIDATION_MISSING: "content_validation_missing.json",
    STAGE_CONTENT_VALIDATION_TARGET: "content_validation_target.json",
    STAGE_CONTENT_VALIDATION_FEASIBILITY: "content_validation_feasibility.json",
    STAGE_CONTENT_VALIDATION_TARGET_REFILL: "content_validation_target_refill.json",
    STAGE_CONTENT_VALIDATION_FEASIBILITY_REFILL: "content_validation_feasibility_refill.json",
    STAGE_NEGOTIATION_PROPOSE_EXTRACTION: "negotiation_propose_extraction.json",
    STAGE_NEGOTIATION_ACCEPT_EXTRACTION: "negotiation_accept_extraction.json",
    STAGE_NEGOTIATION_TARGET_PROPOSE_EXTRACTION: "negotiation_target_propose_extraction.json",
    STAGE_NEGOTIATION_TARGET_ACCEPT_EXTRACTION: "negotiation_target_accept_extraction.json",
    STAGE_NEGOTIATION_FEASIBILITY_PROPOSE_EXTRACTION: "negotiation_feasibility_propose_extraction.json",
    STAGE_NEGOTIATION_FEASIBILITY_ACCEPT_EXTRACTION: "negotiation_feasibility_accept_extraction.json",
}

#: Missing-region scenario inputs that flip the slot-extraction routing to the missing variant.
_MISSING_SCENARIO_INPUTS = (
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_MISSING_ZH,
)

#: Complete scenario inputs; checked before the missing ones because the missing zh input is a
#: substring of the complete zh input.
_COMPLETE_SCENARIO_INPUTS = (NATURAL_LANGUAGE_PROMPT_INPUT_EN, NATURAL_LANGUAGE_PROMPT_INPUT_ZH)

#: Target / feasibility negotiation inputs: complete inputs served by their own slot-extraction
#: variants, whose canned slots preserve the distinguishing values the rendered prompt needs.
_TARGET_SCENARIO_INPUTS = (NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_EN, NATURAL_LANGUAGE_PROMPT_INPUT_TARGET_ZH)
_FEASIBILITY_SCENARIO_INPUTS = (
    NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_EN,
    NATURAL_LANGUAGE_PROMPT_INPUT_FEASIBILITY_ZH,
)

#: Distinguishing values the target/feasibility rendered prompts carry, so the content-validation
#: routing also fires on real rendered-prompt heads and not only on the scenario natural-language
#: inputs. Each marker is a value the failing input carries and the negotiated refill does not.
_TARGET_VALIDATION_MARKER = "20:00~00:00,10Mbps"
_FEASIBILITY_VALIDATION_MARKERS = ("06:00~18:00,10Mbps",)

#: From-data Task-T refill markers (the accepted values are the only distinguishing text).
_TARGET_REFILL_MARKERS = ("23:00~00:00,10Mbps",)
_FEASIBILITY_REFILL_MARKERS = ("power outage from 00:00 to 18:00", "00:00~18:00 停电")

#: Negotiation-T extraction signatures routed most-specific property set first, pinned to the
#: property sets of ``a2a_t.negotiation.generation.json_schema_builder.build_extraction_schema``.
_NEGOTIATION_EXTRACTION_SIGNATURES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"conclusion", "feasibility_summary"}), STAGE_NEGOTIATION_FEASIBILITY_ACCEPT_EXTRACTION),
    (frozenset({"conclusion", "confirmed_intent", "failure_reason"}), STAGE_NEGOTIATION_TARGET_ACCEPT_EXTRACTION),
    (frozenset({"conclusion", "items"}), STAGE_NEGOTIATION_ACCEPT_EXTRACTION),
    (
        frozenset({"feasibility_negotiation_description", "action"}),
        STAGE_NEGOTIATION_FEASIBILITY_PROPOSE_EXTRACTION,
    ),
    (frozenset({"target_negotiation_description"}), STAGE_NEGOTIATION_TARGET_PROPOSE_EXTRACTION),
    (frozenset({"items", "relationship"}), STAGE_NEGOTIATION_PROPOSE_EXTRACTION),
)

#: Localized labels starting the template-content block of the content validation user prompt.
_TEMPLATE_CONTENT_LABELS = ("Template Content:", "模板正文：")

#: Sentence the information-propose input carries when more than one item is missing; the canned
#: propose-extraction response has no relationship by default, so the provider re-adds it.
_PROPOSE_RELATIONSHIP_MARKER = "Multiple items must be provided together."

#: Routed stage names of every served structured call (module-level test hook).
llm_calls: list[str] = []


def reset_call_log() -> None:
    """Clear the module-level routed stage log (test hook)."""
    llm_calls.clear()


def resolve_language(*, env_path: Path | None = None) -> str:
    """Resolve the configured ``A2AT_LANGUAGE`` (defaults to ``zh-CN``)."""
    raw_value = os.environ.get("A2AT_LANGUAGE")
    if not raw_value:
        resolved = env_path or Path.cwd() / ".env"
        values = dotenv_values(resolved) if resolved.exists() else {}
        raw_value = values.get("A2AT_LANGUAGE")
    return str(raw_value or "").strip() or LANGUAGE_ZH


def load_script(language: str, *, resources_dir: Path | None = None) -> dict[str, str]:
    """Load the canned responses of one language as serialized JSON strings.

    Raises:
        FileNotFoundError: when the language tree does not exist.
    """
    language_dir = (resources_dir or _RESOURCES_DIR) / language
    if not language_dir.exists():
        raise FileNotFoundError(f"Scripted responses not found for language: {language} (expected at {language_dir})")
    script: dict[str, str] = {}
    for stage, file_name in _STAGE_FILES.items():
        with (language_dir / file_name).open(encoding="utf-8") as handle:
            script[stage] = json.dumps(json.load(handle), ensure_ascii=False)
    return script


def _head_before_markers(content: str, markers: tuple[str, ...]) -> str:
    """Return the leading part of one message, cut at the earliest of the given markers."""
    cut = len(content)
    for marker in markers:
        index = content.find(marker)
        if 0 <= index < cut:
            cut = index
    return content[:cut]


def _first_user_content(messages: list[dict[str, str]]) -> str:
    """Return the content of the first user message of one structured call."""
    return next(message["content"] for message in messages if message.get("role") == "user")


def _is_target_validation_head(head: str) -> bool:
    """Report whether one content-validation head carries the target negotiation input."""
    return _TARGET_VALIDATION_MARKER in head or any(
        scenario_input in head for scenario_input in _TARGET_SCENARIO_INPUTS
    )


def _is_feasibility_validation_head(head: str) -> bool:
    """Report whether one content-validation head carries the feasibility negotiation input."""
    return all(marker in head for marker in _FEASIBILITY_VALIDATION_MARKERS) or any(
        scenario_input in head for scenario_input in _FEASIBILITY_SCENARIO_INPUTS
    )


def route_structured_call(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any] | None = None,
    *,
    language: str | None = None,
) -> str:
    """Return the stage name one structured call is routed to, keyed on its output schema.

    The schema signature picks the stage family: slot extraction by the ``slots``/``slot_errors``
    properties, content validation by ``semantic_verdict``, and the Negotiation-T extraction
    stages by the most-specific matching signature. The scenario variant is told apart from the
    user message.

    Raises:
        ValueError: when no signature matches the call.
    """
    user_content = _first_user_content(messages)
    properties = json_schema.get("properties") if isinstance(json_schema, dict) else None
    property_names = set(properties) if isinstance(properties, dict) else set()

    if "slots" in property_names and "slot_errors" in property_names:
        if any(scenario_input in user_content for scenario_input in _COMPLETE_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION
        if any(scenario_input in user_content for scenario_input in _MISSING_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION_MISSING
        if any(scenario_input in user_content for scenario_input in _TARGET_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION_TARGET
        if any(scenario_input in user_content for scenario_input in _FEASIBILITY_SCENARIO_INPUTS):
            return STAGE_SLOT_EXTRACTION_FEASIBILITY
        if any(marker in user_content for marker in _FEASIBILITY_REFILL_MARKERS):
            return STAGE_SLOT_EXTRACTION_FEASIBILITY_REFILL
        if any(marker in user_content for marker in _TARGET_REFILL_MARKERS):
            return STAGE_SLOT_EXTRACTION_TARGET_REFILL
        return STAGE_SLOT_EXTRACTION
    if "semantic_verdict" in property_names:
        head = _head_before_markers(user_content, _TEMPLATE_CONTENT_LABELS)
        if _is_target_validation_head(head):
            return STAGE_CONTENT_VALIDATION_TARGET
        if _is_feasibility_validation_head(head):
            return STAGE_CONTENT_VALIDATION_FEASIBILITY
        if any(marker in head for marker in _FEASIBILITY_REFILL_MARKERS):
            return STAGE_CONTENT_VALIDATION_FEASIBILITY_REFILL
        if any(marker in head for marker in _TARGET_REFILL_MARKERS):
            return STAGE_CONTENT_VALIDATION_TARGET_REFILL
        if any(scenario_input in head for scenario_input in _COMPLETE_SCENARIO_INPUTS):
            return STAGE_CONTENT_VALIDATION
        if any(scenario_input in head for scenario_input in _MISSING_SCENARIO_INPUTS):
            return STAGE_CONTENT_VALIDATION_MISSING
        region_string = REGION_STRINGS.get(language or "", "")
        if region_string and region_string not in head:
            return STAGE_CONTENT_VALIDATION_MISSING
        return STAGE_CONTENT_VALIDATION
    for signature, stage in _NEGOTIATION_EXTRACTION_SIGNATURES:
        if signature <= property_names:
            return stage
    raise ValueError(f"The scripted LLM cannot route this structured call: {sorted(property_names)}")


class ScriptedLLMClient:
    """Deterministic ``LLMClient`` serving the canned response tree.

    Attributes:
        language: Language tree the responses are loaded from.
        calls: Routed stage names of every served call on this instance (test hook).
    """

    def __init__(
        self,
        config: LLMClientConfig,
        *,
        logger: Any | None = None,
        language: str | None = None,
        resources_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self.language = language or resolve_language()
        self._script = load_script(self.language, resources_dir=resources_dir)
        self.calls: list[str] = []

    def structured(
        self,
        *,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Return the canned response routed by the output schema and scenario marker."""
        del temperature, max_tokens
        stage = route_structured_call(messages, json_schema, language=self.language)
        self.calls.append(stage)
        llm_calls.append(stage)
        content = self._script[stage]
        if stage == STAGE_NEGOTIATION_PROPOSE_EXTRACTION and _PROPOSE_RELATIONSHIP_MARKER in _first_user_content(
            messages
        ):
            extracted = json.loads(content)
            extracted["relationship"] = _PROPOSE_RELATIONSHIP_MARKER
            content = json.dumps(extracted, ensure_ascii=False)
        return LLMResponse(
            content=content,
            model="scripted",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            metadata={"scripted_stage": stage, "language": self.language},
        )


def register_scripted_provider() -> None:
    """Register :class:`ScriptedLLMClient` with the SDK factory under ``scripted``.

    Idempotent: a second call is a no-op.
    """
    if SCRIPTED_PROVIDER_NAME in LLMClientFactory.available_providers():
        return
    LLMClientFactory.register(SCRIPTED_PROVIDER_NAME, ScriptedLLMClient)


def is_scripted_provider_registered() -> bool:
    """Return whether the ``scripted`` provider is registered."""
    return SCRIPTED_PROVIDER_NAME in LLMClientFactory.available_providers()
