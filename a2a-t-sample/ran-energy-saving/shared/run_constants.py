"""Protocol-level constants shared by the client, the NMS and the gateway.

These values belong to the interaction contract, not to the demonstration scenarios: the round
budget, the terminal client outcomes and the supported process languages. Keeping them here lets the
core modules import them without depending on the scenario catalog.
"""

from __future__ import annotations

from typing import Final, Literal

#: Round budget of one pre-task negotiation (design D11).
MAX_ROUNDS: Final[int] = 3

#: Supported process-level languages.
LANGUAGE_EN: Final[str] = "en-US"
LANGUAGE_ZH: Final[str] = "zh-CN"
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = (LANGUAGE_EN, LANGUAGE_ZH)

#: Terminal client outcomes.
FinalState = Literal[
    "task_completed",
    "no_task_negotiation_ended",
    "no_task_negotiation_aborted",
    "intent_not_confirmed",
    "task_rejected",
    "task_failed",
    "incomplete",
]

FINAL_STATE_TASK_COMPLETED: Final[FinalState] = "task_completed"
FINAL_STATE_NEGOTIATION_ENDED: Final[FinalState] = "no_task_negotiation_ended"
FINAL_STATE_NEGOTIATION_ABORTED: Final[FinalState] = "no_task_negotiation_aborted"
FINAL_STATE_INTENT_NOT_CONFIRMED: Final[FinalState] = "intent_not_confirmed"
FINAL_STATE_TASK_REJECTED: Final[FinalState] = "task_rejected"
FINAL_STATE_TASK_FAILED: Final[FinalState] = "task_failed"
FINAL_STATE_INCOMPLETE: Final[FinalState] = "incomplete"
