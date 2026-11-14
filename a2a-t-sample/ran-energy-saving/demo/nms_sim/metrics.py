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
    #: How the question was carried: ``prose`` (text only) or ``items`` (text plus the
    #: machine-readable missing-item envelope). Kept apart from ``questionsByKind`` so the
    #: negotiation kind is not polluted by a wire-shape experiment.
    questions_by_shape: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    #: How the business values were read: ``typed`` (data statement) or ``text`` (parsed prose).
    #: This is the v2 prototype's central counter: the deterministic path must be taken.
    paths: dict[str, int] = field(default_factory=dict)
    #: Retries recognised by request identity. A duplicate must not execute again.
    duplicates: int = 0

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable copy."""
        return {
            "requests": self.requests,
            "questions": self.questions,
            "questionsByKind": dict(self.questions_by_kind),
            "questionsByShape": dict(self.questions_by_shape),
            "rounds": self.rounds,
            "outcomes": dict(self.outcomes),
            "paths": dict(self.paths),
            "duplicates": self.duplicates,
        }


class ServerMetrics:
    """Count what the simulated NMS did for each protocol."""

    def __init__(self) -> None:
        self._a2a_t = _Counters()
        self._pure = _Counters()
        self._settings: dict[str, object] = {}

    def describe(self, settings: dict[str, object]) -> None:
        """Record the fidelity switches the counters were produced under.

        A count without its switches is not evidence: the same batch run under a stricter rate or
        window policy reports different failures. The snapshot carries them so a report can state
        which configuration produced its numbers.
        """
        self._settings = dict(settings)

    def request(self, protocol: str) -> None:
        """Count one inbound message request."""
        self._bucket(protocol).requests += 1

    def question(self, protocol: str, kind: str, *, shape: str = "prose") -> None:
        """Count one question the server had to ask, by negotiation kind and wire shape."""
        bucket = self._bucket(protocol)
        bucket.questions += 1
        bucket.questions_by_kind[kind] = bucket.questions_by_kind.get(kind, 0) + 1
        bucket.questions_by_shape[shape] = bucket.questions_by_shape.get(shape, 0) + 1

    def round(self, protocol: str) -> None:
        """Count one negotiation round the server advanced."""
        self._bucket(protocol).rounds += 1

    def path(self, protocol: str, path: str) -> None:
        """Count one request by how its business values were read."""
        bucket = self._bucket(protocol)
        bucket.paths[path] = bucket.paths.get(path, 0) + 1

    def duplicate(self, protocol: str) -> None:
        """Count one request recognised as a retry of an already-accepted request."""
        self._bucket(protocol).duplicates += 1

    def outcome(self, protocol: str, outcome: str) -> None:
        """Count one terminal outcome."""
        bucket = self._bucket(protocol)
        bucket.outcomes[outcome] = bucket.outcomes.get(outcome, 0) + 1

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable copy of every counter."""
        return {
            "settings": dict(self._settings),
            "protocols": {
                PROTOCOL_A2A_T: self._a2a_t.snapshot(),
                PROTOCOL_A2A: self._pure.snapshot(),
            },
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
