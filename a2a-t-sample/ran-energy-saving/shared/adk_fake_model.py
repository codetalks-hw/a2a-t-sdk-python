"""Deterministic ADK ``BaseLlm`` for offline runs and tests.

The fake model never touches the network. It selects a script by a **marker** found in the agent's
system instruction or message contents (the ADK identity processor injects the agent name, so the
built-in scripts are keyed by the server/client agent names), then serves the script's turns in
order. Turn progress is derived from the number of function-response parts already present in the
request, so the model is stateless and safe to share across sessions.

Built-in scripts cover the two agent roles:

* the server agent reads the cell profile, evaluates the plan, applies it, and writes the report;
* the client agent returns a structured ``NegotiationDecision``.

Tool arguments may echo a value returned by an earlier tool call with the
``${last_tool_response.<key>}`` placeholder; the built-in server script uses it for the ``plan_id``
of the report call, so the script always targets the plan the apply tool actually created.

A script's progress is normally derived from the function responses of the request (an agent with
tools). The tool-less client decision agent instead advances on the model turns already present in
its session, which is what :func:`build_client_script` uses so a scenario can return one decision
per inbound proposal.

Tests and later tasks can inject their own scripts with :func:`build_fake_model`; the offline
scenarios build their client script with :func:`build_client_script`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import AsyncGenerator

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from shared.models import NegotiationDecision

#: Model name reported by the fake model.
DEFAULT_MODEL_NAME = "fake-ran-energy-saving"

#: Agent names of the sample (design §6.1 / §7.3); the scripts are keyed by them.
SERVER_AGENT_NAME = "ran_energy_service_agent"
CLIENT_AGENT_NAME = "ran_energy_request_agent"

#: Final text served when a script runs out of turns.
DEFAULT_FINAL_TEXT = "RAN energy-saving task completed."

#: Prefix of a tool argument resolved from the most recent tool response.
LAST_RESPONSE_PLACEHOLDER_PREFIX = "${last_tool_response."

#: Progress source of a script: advance on the function responses of the request.
PROGRESS_FUNCTION_RESPONSES = "function_responses"

#: Progress source of a script: advance on the model turns already present in the session.
PROGRESS_MODEL_TURNS = "model_turns"


@dataclass(frozen=True)
class FakeToolCall:
    """One function call the fake model emits.

    Attributes:
        name: Tool name registered on the ADK agent.
        args: JSON-serializable tool arguments.
    """

    name: str
    args: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeTurn:
    """One model turn: either a text response or a set of function calls.

    Attributes:
        text: Text of the turn, when the turn returns text.
        tool_calls: Function calls of the turn, when the turn requests tools.
    """

    text: str | None = None
    tool_calls: tuple[FakeToolCall, ...] = ()

    def to_response(self, last_tool_response: Mapping[str, object] | None = None) -> LlmResponse:
        """Build the ADK response of this turn.

        Args:
            last_tool_response: Payload of the most recent tool response; ``${last_tool_response.<key>}``
                argument placeholders are resolved from it.
        """
        context = last_tool_response or {}
        parts: list[types.Part] = []
        if self.text is not None:
            parts.append(types.Part(text=self.text))
        for call in self.tool_calls:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        name=call.name,
                        args={key: _resolve_arg(value, context) for key, value in call.args.items()},
                    ),
                )
            )
        return LlmResponse(
            content=types.Content(role="model", parts=parts),
            partial=False,
        )


@dataclass(frozen=True)
class FakeScript:
    """A marker-selected response script.

    Attributes:
        marker: Substring identifying the script (usually the ADK agent name).
        turns: Turns served in order.
        default_text: Text served once ``turns`` is exhausted.
        progress: Turn progress source: ``function_responses`` (agents with tools) or
            ``model_turns`` (the tool-less client decision agent).
    """

    marker: str
    turns: tuple[FakeTurn, ...]
    default_text: str = DEFAULT_FINAL_TEXT
    progress: str = PROGRESS_FUNCTION_RESPONSES


#: Server agent script: read profile -> evaluate -> apply -> report -> final answer.
DEFAULT_SERVER_SCRIPT = FakeScript(
    marker=SERVER_AGENT_NAME,
    turns=(
        FakeTurn(
            tool_calls=(
                FakeToolCall(
                    name="get_cell_energy_profile",
                    args={"cell_id": "cell-001", "date": "2026-10-22"},
                ),
            )
        ),
        FakeTurn(
            tool_calls=(
                FakeToolCall(
                    name="evaluate_energy_saving_plan",
                    args={"cell_id": "cell-001", "window": "00:00~06:00", "target_rate": "30"},
                ),
            )
        ),
        FakeTurn(
            tool_calls=(
                FakeToolCall(
                    name="apply_energy_saving_plan",
                    args={"cell_id": "cell-001", "window": "00:00~06:00", "target_rate": "30"},
                ),
            )
        ),
        FakeTurn(
            tool_calls=(
                FakeToolCall(
                    name="generate_energy_saving_report",
                    args={"plan_id": LAST_RESPONSE_PLACEHOLDER_PREFIX + "plan_id}"},
                ),
            )
        ),
        FakeTurn(text=DEFAULT_FINAL_TEXT),
    ),
)

#: Client agent script: one structured decision, no tool call.
DEFAULT_CLIENT_SCRIPT = FakeScript(
    marker=CLIENT_AGENT_NAME,
    turns=(
        FakeTurn(
            text=(
                '{"action": "accept", '
                '"rationale": "The proposed energy-saving plan is feasible and keeps the SLA margin."}'
            )
        ),
    ),
    default_text=DEFAULT_FINAL_TEXT,
)

#: Default scripts served when no custom scripts are injected.
DEFAULT_SCRIPTS: tuple[FakeScript, ...] = (DEFAULT_SERVER_SCRIPT, DEFAULT_CLIENT_SCRIPT)


def _content_texts(llm_request: LlmRequest) -> list[str]:
    """Return the instruction and content text of one request."""
    texts: list[str] = []
    instruction = llm_request.config.system_instruction
    if isinstance(instruction, str):
        texts.append(instruction)
    elif isinstance(instruction, types.Content):
        texts.extend(part.text for part in instruction.parts or [] if part.text)
    for content in llm_request.contents:
        texts.extend(part.text for part in content.parts or [] if part.text)
    return texts


def _function_response_count(llm_request: LlmRequest) -> int:
    """Return how many tool responses the request already carries."""
    return sum(
        1
        for content in llm_request.contents
        for part in content.parts or []
        if part.function_response is not None
    )


def _model_turn_count(llm_request: LlmRequest) -> int:
    """Return how many model turns the session already carries."""
    return sum(1 for content in llm_request.contents if content.role == "model")


def _turn_index(llm_request: LlmRequest, script: FakeScript) -> int:
    """Return the next turn's index according to the script's progress source."""
    if script.progress == PROGRESS_MODEL_TURNS:
        return _model_turn_count(llm_request)
    return _function_response_count(llm_request)


def _last_function_response(llm_request: LlmRequest) -> dict[str, object]:
    """Return the payload of the most recent tool response in the request."""
    latest: dict[str, object] = {}
    for content in llm_request.contents:
        for part in content.parts or []:
            if part.function_response is None:
                continue
            response = part.function_response.response
            if isinstance(response, dict):
                latest = {str(key): value for key, value in response.items()}
    return latest


def _resolve_arg(value: object, tool_response: Mapping[str, object]) -> object:
    """Resolve a ``${last_tool_response.<key>}`` placeholder, leaving other values untouched."""
    if (
        isinstance(value, str)
        and value.startswith(LAST_RESPONSE_PLACEHOLDER_PREFIX)
        and value.endswith("}")
    ):
        key = value[len(LAST_RESPONSE_PLACEHOLDER_PREFIX) : -1]
        return tool_response.get(key, value)
    return value


def _select_script(llm_request: LlmRequest, scripts: Sequence[FakeScript]) -> FakeScript | None:
    """Return the most specific script whose marker appears in the request, if any."""
    haystack = "\n".join(_content_texts(llm_request))
    matches = [script for script in scripts if script.marker and script.marker in haystack]
    if not matches:
        return None
    return max(matches, key=lambda script: len(script.marker))


class FakeLlm(BaseLlm):
    """Deterministic ADK model serving marker-selected scripts.

    Attributes:
        scripts: Scripts searched for a marker match, most specific marker wins.
        default_text: Text served when no script matches or a script is exhausted.
    """

    model: str = DEFAULT_MODEL_NAME
    scripts: tuple[FakeScript, ...] = DEFAULT_SCRIPTS
    default_text: str = DEFAULT_FINAL_TEXT

    @property
    def capabilities(self) -> LlmCapabilities:
        """The fake model serves an output schema even when the agent also has tools."""
        return LlmCapabilities(output_schema_and_tools=True)

    @classmethod
    def supported_models(cls) -> list[str]:
        """Return the model names this fake model answers to."""
        return [DEFAULT_MODEL_NAME]

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Serve the next turn of the matched script.

        Args:
            llm_request: The request built by the ADK flow.
            stream: Accepted for protocol compatibility; the fake model always serves one
                complete response.
        """
        del stream
        script = _select_script(llm_request, self.scripts)
        if script is None:
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=self.default_text)]),
                partial=False,
            )
            return
        index = _turn_index(llm_request, script)
        if index < len(script.turns):
            yield script.turns[index].to_response(_last_function_response(llm_request))
            return
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=script.default_text)]),
            partial=False,
        )


def build_client_script(decisions: Sequence[NegotiationDecision]) -> FakeScript:
    """Build the client decision script returning one structured decision per turn.

    The script uses the same marker as :data:`DEFAULT_CLIENT_SCRIPT`, so it matches the
    ``ran_energy_request_agent`` instruction, and advances on the model turns of the ADK session:
    one offline negotiation round asks the tool-less client agent for exactly one decision.

    Args:
        decisions: Decisions returned in order; each becomes one JSON-text model turn.
    """
    return FakeScript(
        marker=CLIENT_AGENT_NAME,
        turns=tuple(
            FakeTurn(text=decision.model_dump_json(exclude_none=True)) for decision in decisions
        ),
        progress=PROGRESS_MODEL_TURNS,
    )


def build_fake_model(
    scripts: Sequence[FakeScript] | None = None,
    *,
    default_text: str = DEFAULT_FINAL_TEXT,
) -> FakeLlm:
    """Build the deterministic ADK model used in offline mode.

    Args:
        scripts: Scripts to serve; defaults to :data:`DEFAULT_SCRIPTS`.
        default_text: Text served when no script matches or a script is exhausted.
    """
    return FakeLlm(
        model=DEFAULT_MODEL_NAME,
        scripts=tuple(scripts) if scripts is not None else DEFAULT_SCRIPTS,
        default_text=default_text,
    )
