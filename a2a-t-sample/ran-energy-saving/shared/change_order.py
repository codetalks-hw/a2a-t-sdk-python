"""The baseline a change order is written against (P3).

S6 proved the client can bind an approval to the plan it accepted. A change order asks the same
question one step later: *which* plan is this change meant to replace?

Two operators can edit the same Task, and a Modify that was written against the previous plan can
arrive after someone else already changed it. Without a baseline the server can only apply the new
values over whatever is current, which silently drops the other operator's change.

The envelope is sample-local, like ``a2at-sample/approval``. What makes it usable on an A2A-T hop
is not the key but the value: the digest of a plan both sides hold, computed with the SDK's
canonical form over :data:`shared.plan.PLAN_FIELDS`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from shared.approval import plan_digest

#: Sample-local metadata key of the envelope.
BASELINE_METADATA_KEY: Final[str] = "a2at-sample/baseline"

#: Metadata key of the refusal, on the status message of a Task that rejected a change order.
BASELINE_ERROR_METADATA_KEY: Final[str] = "a2at-sample/baseline-error"

#: Envelope schema version.
BASELINE_VERSION: Final[int] = 1

#: Reason codes the server reports when it refuses a change order.
REASON_MISSING: Final[str] = "baseline-missing"
REASON_STALE: Final[str] = "baseline-stale"


@dataclass(frozen=True)
class Baseline:
    """The plan a change order declares it replaces.

    Attributes:
        plan_digest: Digest of the plan the client believes is current.
        agreement_id: Agreement the baseline belongs to, when the client names one.
        revision: Revision the baseline names, when the client names one.
    """

    plan_digest: str
    agreement_id: str | None = None
    revision: int | None = None


def baseline_metadata(
    plan: Mapping[str, object],
    *,
    task_id: str | None = None,
    agreement_id: str | None = None,
    revision: int | None = None,
) -> dict[str, object]:
    """Return the metadata entry naming the plan a change order replaces.

    Args:
        plan: The plan the client believes is current.
        task_id: Task the baseline belongs to, when known.

    Returns:
        One metadata entry keyed by :data:`BASELINE_METADATA_KEY`.
    """
    baseline: dict[str, object] = {
        "version": BASELINE_VERSION,
        "planDigest": plan_digest(plan),
    }
    if task_id:
        baseline["taskId"] = task_id
    if agreement_id:
        baseline["agreementId"] = agreement_id
    if revision is not None:
        baseline["revision"] = revision
    return {BASELINE_METADATA_KEY: baseline}


def read_baseline(metadata: Mapping[str, object] | None) -> Baseline | None:
    """Return the declared baseline, ``None`` when absent or malformed.

    A malformed envelope degrades to "no baseline": the server then reports ``baseline-missing``
    instead of failing the request with a parse error.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(BASELINE_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version != BASELINE_VERSION:
        return None
    digest = raw.get("planDigest")
    if not isinstance(digest, str) or not digest:
        return None
    agreement_id = raw.get("agreementId")
    revision = raw.get("revision")
    return Baseline(
        plan_digest=digest,
        agreement_id=agreement_id if isinstance(agreement_id, str) else None,
        revision=int(revision) if isinstance(revision, int) and not isinstance(revision, bool) else None,
    )


def read_baseline_digest(metadata: Mapping[str, object] | None) -> str | None:
    """Return the declared baseline digest, ``None`` when absent or malformed."""
    baseline = read_baseline(metadata)
    return baseline.plan_digest if baseline is not None else None


__all__ = [
    "BASELINE_ERROR_METADATA_KEY",
    "Baseline",
    "read_baseline",
    "BASELINE_METADATA_KEY",
    "BASELINE_VERSION",
    "REASON_MISSING",
    "REASON_STALE",
    "baseline_metadata",
    "read_baseline_digest",
]
