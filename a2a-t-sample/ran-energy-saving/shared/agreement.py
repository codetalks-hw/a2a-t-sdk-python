"""An agreement as a durable, revisable object (v2 prototype, pillar B).

In v1 the accepted plan exists only inside one Task: a change is a new negotiation, and a second
execution of the same plan has nothing to point at. This envelope makes the agreement itself the
object -- an id, a revision, one digest and the scopes it covers -- and lets Tasks execute it.

The digest is the same canonical plan digest used by approval, readback and change orders, so an
agreement is not a second truth: it is the named, versioned form of the plan both sides already
hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from a2a_t.core.plan_binding import plan_digest

from shared.plan import PLAN_FIELDS

#: Sample-local metadata key. Prototype key: a registered extension URI replaces it.
AGREEMENT_METADATA_KEY: Final[str] = "a2at-sample/agreement"

#: Envelope version.
AGREEMENT_VERSION: Final[int] = 1

#: Reason codes the server reports when a change order targets a superseded revision.
REASON_UNKNOWN: Final[str] = "agreement-unknown"
REASON_STALE: Final[str] = "agreement-stale"


@dataclass(frozen=True)
class Agreement:
    """One revision of a durable agreement.

    Attributes:
        agreement_id: Identity that survives revisions.
        revision: Revision number, starting at 1.
        plan_digest: Digest of the accepted plan this revision commits to.
        scopes: ``{window, rateMbps}`` entries of this revision.
        schema_uri: Schema the scopes conform to, when the statement was typed.
        expires_at: ISO-8601 instant after which the revision cannot be accepted or executed.
    """

    agreement_id: str
    revision: int
    plan_digest: str
    scopes: tuple[Mapping[str, object], ...] = ()
    schema_uri: str = ""
    expires_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this revision."""
        payload: dict[str, object] = {
            "version": AGREEMENT_VERSION,
            "agreementId": self.agreement_id,
            "revision": self.revision,
            "planDigest": self.plan_digest,
            "scopes": [dict(scope) for scope in self.scopes],
        }
        if self.schema_uri:
            payload["schemaUri"] = self.schema_uri
        if self.expires_at is not None:
            payload["expiresAt"] = self.expires_at
        return payload

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this revision."""
        return {AGREEMENT_METADATA_KEY: self.to_dict()}


def read_agreement(metadata: Mapping[str, object] | None) -> Agreement | None:
    """Read an agreement revision from one message, ``None`` when absent or malformed."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(AGREEMENT_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        version = int(raw.get("version", 0))
        revision = int(raw.get("revision", 0))
    except (TypeError, ValueError):
        return None
    agreement_id = raw.get("agreementId")
    digest = raw.get("planDigest")
    if version != AGREEMENT_VERSION or revision < 1:
        return None
    if not isinstance(agreement_id, str) or not agreement_id:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    scopes = raw.get("scopes")
    expires_at = raw.get("expiresAt")
    schema_uri = raw.get("schemaUri")
    return Agreement(
        agreement_id=agreement_id,
        revision=revision,
        plan_digest=digest,
        scopes=tuple(dict(scope) for scope in scopes) if isinstance(scopes, list) else (),
        schema_uri=schema_uri if isinstance(schema_uri, str) else "",
        expires_at=expires_at if isinstance(expires_at, str) else None,
    )


def next_revision(
    previous: Agreement | None,
    *,
    plan: Mapping[str, object],
    scopes: Sequence[Mapping[str, object]] = (),
    schema_uri: str = "",
    expires_at: str | None = None,
) -> Agreement:
    """Return the revision one accepted plan produces.

    A plan identical to the current revision does not create a new one: re-accepting the same
    agreement is an idempotent restatement, not a change. Anything else bumps the revision.
    """
    digest = plan_digest(plan, PLAN_FIELDS)
    if previous is not None and previous.plan_digest == digest:
        return previous
    return Agreement(
        agreement_id=previous.agreement_id if previous is not None else f"agr-{_short_id()}",
        revision=1 if previous is None else previous.revision + 1,
        plan_digest=digest,
        scopes=tuple(dict(scope) for scope in scopes),
        schema_uri=schema_uri,
        expires_at=expires_at,
    )


def _short_id() -> str:
    """Return a short random id fragment."""
    return uuid.uuid4().hex[:12]


__all__ = [
    "AGREEMENT_METADATA_KEY",
    "AGREEMENT_VERSION",
    "REASON_STALE",
    "REASON_UNKNOWN",
    "Agreement",
    "next_revision",
    "read_agreement",
]
