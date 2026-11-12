"""Server-side work counters of the NMS simulator.

A protocol comparison that only measures the client hides half of the story: the same request can
cost the server one validation or three question rounds. These counters make the server side of the
exchange observable, per protocol, without changing any simulated behaviour.

The snapshot is served at ``GET /a2a/json/metrics`` and is meant to be read after a batch, not to
drive decisions inside the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Protocol keys the counters are split by. A request that declares no extension is pure A2A.
PROTOCOL_A2A_T: Final[str] = "a2a-t"
PROTOCOL_A2A: Final[str] = "a2a"

#: Task outcomes the simulator can end a request with.
OUTCOME_ACCEPTED: Final[str] = "accepted"
OUTCOME_REJECTED: Final[str] = "rejected"
OUTCOME_ABORTED: Final[str] = "aborted"
OUTCOME_REFUSED: Final[str] = "refused"
OUTCOME_INVALID: Final[str] = "invalid"


@dataclass
class _Counters:
    """Mutable counters of one protocol."""

    requests: int = 0
    questions: int = 0
    rounds: int = 0
    questions_by_kind: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable copy."""
        return {
            "requests": self.requests,
            "questions": self.questions,
            "questionsByKind": dict(self.questions_by_kind),
            "rounds": self.rounds,
            "outcomes": dict(self.outcomes),
        }


class ServerMetrics:
    """Count what the simulated NMS did for each protocol."""

    def __init__(self) -> None:
        self._a2a_t = _Counters()
        self._pure = _Counters()

    def request(self, protocol: str) -> None:
        """Count one inbound message request."""
        self._bucket(protocol).requests += 1

    def question(self, protocol: str, kind: str) -> None:
        """Count one question the server had to ask, by negotiation kind."""
        bucket = self._bucket(protocol)
        bucket.questions += 1
        bucket.questions_by_kind[kind] = bucket.questions_by_kind.get(kind, 0) + 1

    def round(self, protocol: str) -> None:
        """Count one negotiation round the server advanced."""
        self._bucket(protocol).rounds += 1

    def outcome(self, protocol: str, outcome: str) -> None:
        """Count one terminal outcome."""
        bucket = self._bucket(protocol)
        bucket.outcomes[outcome] = bucket.outcomes.get(outcome, 0) + 1

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable copy of every counter."""
        return {
            "protocols": {
                PROTOCOL_A2A_T: self._a2a_t.snapshot(),
                PROTOCOL_A2A: self._pure.snapshot(),
            }
        }

    def _bucket(self, protocol: str) -> _Counters:
        """Return the counter bucket of one protocol, defaulting to pure A2A."""
        return self._a2a_t if protocol == PROTOCOL_A2A_T else self._pure


__all__ = [
    "OUTCOME_ABORTED",
    "OUTCOME_ACCEPTED",
    "OUTCOME_INVALID",
    "OUTCOME_REFUSED",
    "OUTCOME_REJECTED",
    "PROTOCOL_A2A",
    "PROTOCOL_A2A_T",
    "ServerMetrics",
]
