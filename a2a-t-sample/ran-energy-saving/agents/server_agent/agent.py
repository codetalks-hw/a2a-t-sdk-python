"""ADK server agent of the RAN energy-saving sample.

``root_agent`` is loadable by ``adk run`` / ``adk web``. The model comes from
:func:`shared.config.build_adk_model`, so the agent is deterministic and offline by default and
uses the configured OpenAI-compatible endpoint in ``live`` mode.
"""

from __future__ import annotations

from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from shared.config import build_adk_model

from agents.server_agent.tools import (
    apply_energy_saving_plan,
    evaluate_energy_saving_plan,
    generate_energy_saving_report,
    get_cell_energy_profile,
)

#: Agent name; the offline fake model keys its server script on this name.
AGENT_NAME = "ran_energy_service_agent"

#: Location of the agent instruction.
INSTRUCTION_PATH = Path(__file__).resolve().parent / "prompts" / "service_instruction.md"

#: Instruction served to the ADK model.
SERVICE_INSTRUCTION = INSTRUCTION_PATH.read_text(encoding="utf-8")


def build_server_agent(model: BaseLlm | None = None) -> LlmAgent:
    """Build the RAN energy-saving service agent.

    Args:
        model: Model to use; defaults to :func:`shared.config.build_adk_model` (the fake model in
            offline mode, ``LiteLlm`` in live mode).
    """
    return LlmAgent(
        name=AGENT_NAME,
        model=model if model is not None else build_adk_model(),
        description=(
            "Reads RAN cell energy profiles, evaluates and applies energy-saving plans, and "
            "writes the structured energy-saving report."
        ),
        instruction=SERVICE_INSTRUCTION,
        tools=[
            get_cell_energy_profile,
            evaluate_energy_saving_plan,
            apply_energy_saving_plan,
            generate_energy_saving_report,
        ],
    )


root_agent = build_server_agent()
