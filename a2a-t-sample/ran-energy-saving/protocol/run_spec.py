"""Run specification and the demo/production seams of the client runtime.

A client run needs three things the core cannot own:

* the natural-language request and the expected negotiation sequence (a scenario catalog);
* the deterministic decision script used in offline mode;
* the business values filled into an accepted Task-T (a client policy).

The composition root (the demo launcher, the Web UI, the gateway CLI or a customer service) supplies
them; ``protocol`` and ``shared`` only see these interfaces.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.models import NegotiationDecision
from shared.run_constants import LANGUAGE_ZH


@dataclass(frozen=True)
class ExpectedStep:
    """One expected performative of a run, used by the UI and the acceptance tests."""

    actor: str
    performative: str
    round: int | None = None


def _no_filled_params(kind: str, language: str) -> dict[str, object]:
    """Default policy: an accept carries no business values."""
    del kind, language
    return {}


def _no_accept_text(kind: str, language: str) -> str:
    """Default policy: the accept text is derived from the decision rationale."""
    del kind, language
    return ""


@dataclass(frozen=True)
class ClientPolicy:
    """Business values of one OSS integration, supplied by the composition root.

    Attributes:
        filled_params: values filled into the Task-T when the client accepts a proposal.
        accept_text: natural-language accept confirmation, when the integration provides one.
    """

    filled_params: Callable[[str, str], dict[str, object]] = _no_filled_params
    accept_text: Callable[[str, str], str] = _no_accept_text


DEFAULT_CLIENT_POLICY: ClientPolicy = ClientPolicy()


@dataclass(frozen=True)
class RunSpec:
    """Everything the core needs to know about one named run.

    Attributes:
        name: scenario/run name used by the UI and the CLI.
        description: one-line description shown by the UI.
        user_request: natural-language request resolved for the process language.
        scripted_decisions: decisions replayed by the scripted provider and the offline model.
        expected_sequence: expected performative sequence (UI hints and acceptance tests).
        policy: business values filled on accept.
    """

    name: str
    description: str
    user_request: str
    scripted_decisions: tuple[NegotiationDecision, ...] = ()
    expected_sequence: tuple[ExpectedStep, ...] = ()
    policy: ClientPolicy = DEFAULT_CLIENT_POLICY


@runtime_checkable
class RunCatalog(Protocol):
    """Catalog of the named runs a deployment exposes."""

    @property
    def names(self) -> tuple[str, ...]:
        """Supported run names, in display order."""
        ...

    def spec(self, name: str, *, language: str = LANGUAGE_ZH) -> RunSpec:
        """Resolve one run name to its specification.

        Raises:
            KeyError: when the name is unknown.
        """
        ...


@dataclass(frozen=True)
class EmptyRunCatalog:
    """Catalog used when a deployment did not configure one; every lookup fails loudly."""

    @property
    def names(self) -> tuple[str, ...]:
        """No run is available."""
        return ()

    def spec(self, name: str, *, language: str = LANGUAGE_ZH) -> RunSpec:
        """Reject every lookup with the missing-catalog explanation."""
        del language
        raise KeyError(f"Unknown run {name!r}: no run catalog is configured.")
