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
from datetime import UTC, datetime
from typing import Final

from a2a_t.core.plan_binding import plan_digest

from shared.plan import PLAN_FIELDS

#: Sample-local metadata key. Prototype key: a registered extension URI replaces it.
AGREEMENT_METADATA_KEY: Final[str] = "a2at-sample/agreement"

#: Metadata key a client uses to state the identity of its request (an idempotency key). An
#: agreement reference needs it because the document no longer identifies the request: re-sending
#: the same reference must be distinguishable from asking to execute the revision a second time.
REQUEST_ID_METADATA_KEY: Final[str] = "a2at-sample/request-id"

#: Envelope version.
AGREEMENT_VERSION: Final[int] = 1

#: Reason codes the server reports when a change order targets a superseded revision.
REASON_UNKNOWN: Final[str] = "agreement-unknown"
REASON_EXPIRED: Final[str] = "agreement-expired"
REASON_MISMATCH: Final[str] = "agreement-mismatch"
REASON_STALE: Final[str] = "agreement-stale"


@dataclass(frozen=True)
class Agreement:
    """One revision of a durable agreement.

    Attributes:
        agreement_id: Identity that survives revisions.
        revision: Revision number, starting at 1.
        plan_digest: Digest of the accepted plan this revision commits to.
        operation: Operation the revision commits to.
        region: Region the revision commits to.
        scopes: ``{window, rateMbps}`` entries of this revision.
        schema_uri: Schema the scopes conform to, when the statement was typed.
        expires_at: ISO-8601 instant after which the revision cannot be accepted or executed.
        reversal: Plan that undoes this revision (``a2at-sample/reversal``), when one was captured.
    """

    agreement_id: str
    revision: int
    plan_digest: str
    operation: str = ""
    region: str = ""
    scopes: tuple[Mapping[str, object], ...] = ()
    schema_uri: str = ""
    expires_at: str | None = None
    reversal: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the wire form of this revision."""
        payload: dict[str, object] = {
            "version": AGREEMENT_VERSION,
            "agreementId": self.agreement_id,
            "revision": self.revision,
            "planDigest": self.plan_digest,
            "operation": self.operation,
            "region": self.region,
            "scopes": [dict(scope) for scope in self.scopes],
        }
        if self.schema_uri:
            payload["schemaUri"] = self.schema_uri
        if self.expires_at is not None:
            payload["expiresAt"] = self.expires_at
        if self.reversal is not None:
            # Part of the agreement, not a second object: a reader of the revision gets the state
            # it replaces in the same document that names the state it commits to.
            payload["reversal"] = dict(self.reversal)
        return payload

    def to_metadata(self) -> dict[str, object]:
        """Return the metadata entry carrying this revision."""
        return {AGREEMENT_METADATA_KEY: self.to_dict()}

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether this revision is past its ``expiresAt``.

        A naive instant is read as this host's local time: without an offset it can only mean the
        clock of whoever stored it. A value that cannot be parsed is treated as NOT expired --
        reading data must never fail on a malformed clock -- so code that grants authority from an
        agreement must fail closed itself instead of relying on this predicate.
        """
        if self.expires_at is None:
            return False
        try:
            instant = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if instant.tzinfo is None:
            instant = instant.astimezone()
        moment = now if now is not None else datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment >= instant


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
    if scopes is not None and not isinstance(scopes, list):
        # A scopes field that is not a list cannot describe a set of scopes; the document is
        # malformed rather than an agreement with no scopes.
        return None
    expires_at = raw.get("expiresAt")
    schema_uri = raw.get("schemaUri")
    operation = raw.get("operation")
    region = raw.get("region")
    reversal = raw.get("reversal")
    return Agreement(
        agreement_id=agreement_id,
        revision=revision,
        plan_digest=digest,
        operation=operation if isinstance(operation, str) else "",
        region=region if isinstance(region, str) else "",
        # A single malformed scope entry is skipped: one bad element must not make the whole
        # revision unreadable, because the rest of it still names an executable plan.
        scopes=(
            tuple(dict(scope) for scope in scopes if isinstance(scope, Mapping))
            if isinstance(scopes, list)
            else ()
        ),
        schema_uri=schema_uri if isinstance(schema_uri, str) else "",
        expires_at=expires_at if isinstance(expires_at, str) else None,
        # Only a mapping can be a reversal plan; anything else is absent rather than fatal.
        reversal=dict(reversal) if isinstance(reversal, Mapping) else None,
    )


def next_revision(
    previous: Agreement | None,
    *,
    plan: Mapping[str, object],
    scopes: Sequence[Mapping[str, object]] = (),
    schema_uri: str = "",
    expires_at: str | None = None,
    reversal: Mapping[str, object] | None = None,
) -> Agreement:
    """Return the revision one accepted plan produces.

    A plan identical to the current revision does not create a new one: re-accepting the same
    agreement is an idempotent restatement, not a change. Anything else bumps the revision.

    An idempotent restatement also discards the caller's ``scopes``, ``expires_at`` and
    ``reversal``. Extending the authority an agreement carries -- a longer life, a wider scope, a
    different state to fall back to -- must be an explicit act, never a side effect of restating a
    plan; otherwise a retry could silently widen what the two sides agreed. For the same reason the
    restatement keeps the original reversal: a restatement is not a new promise.
    """
    digest = plan_digest(plan, PLAN_FIELDS)
    if previous is not None and previous.plan_digest == digest:
        return previous
    return Agreement(
        agreement_id=previous.agreement_id if previous is not None else f"agr-{_short_id()}",
        revision=1 if previous is None else previous.revision + 1,
        plan_digest=digest,
        operation=str(plan.get("operation") or ""),
        region=str(plan.get("region") or ""),
        scopes=tuple(dict(scope) for scope in scopes),
        schema_uri=schema_uri,
        expires_at=expires_at,
        reversal=dict(reversal) if reversal is not None else None,
    )


def _short_id() -> str:
    """Return a short random id fragment."""
    return uuid.uuid4().hex[:12]


__all__ = [
    "AGREEMENT_METADATA_KEY",
    "AgreementRef",
    "agreement_ref_metadata",
    "read_agreement_ref",
    "AGREEMENT_VERSION",
    "REASON_EXPIRED",
    "REASON_MISMATCH",
    "REASON_STALE",
    "REQUEST_ID_METADATA_KEY",
    "REASON_UNKNOWN",
    "Agreement",
    "next_revision",
    "read_agreement",
]


@dataclass(frozen=True)
class AgreementRef:
    """A client's reference to an agreement revision it wants executed.

    Attributes:
        agreement_id: Agreement to execute.
        revision: Revision to execute; ``None`` means "the current one".
    """

    agreement_id: str
    revision: int | None = None


def read_agreement_ref(metadata: Mapping[str, object] | None) -> AgreementRef | None:
    """Read a client reference, ``None`` when absent or malformed.

    A reference carries no digest: the server owns the revision. That is what makes "execute the
    agreement I already accepted" expressible without resending the whole statement.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(AGREEMENT_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    agreement_id = raw.get("agreementId")
    if not isinstance(agreement_id, str) or not agreement_id:
        return None
    revision = raw.get("revision")
    return AgreementRef(
        agreement_id=agreement_id,
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
    )


def agreement_ref_metadata(ref: AgreementRef) -> dict[str, object]:
    """Return the metadata entry carrying one client reference."""
    payload: dict[str, object] = {"agreementId": ref.agreement_id}
    if ref.revision is not None:
        payload["revision"] = ref.revision
    return {AGREEMENT_METADATA_KEY: payload}
