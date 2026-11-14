"""Carry an A2A-T contract across a hop that does not implement it.

The OSS Agent is a standard-A2A proxy. It does not render Task-T, does not validate a plan and
does not answer a Negotiation-T proposal. That is not a reason to delete the contract: a hop that
does not *alter* a contract must forward its carrier byte-identically and be accountable for it.
A hop never needs to understand a contract to carry it honestly.

The carrier is the metadata map. :func:`carried` selects the keys a hop must not rewrite,
:func:`contract_digest` names exactly what this hop received, and :func:`append_hop` writes that
digest into the delegation chain before the map continues downstream. A third party holding the
wire frames recomputes the digest from the hop's inbound map and compares it with the claim; a
hop that silently dropped or rewrote a key cannot produce a matching entry.

This module is mechanism only: no OSS import, no I/O, and no opinion about assurance. A hop states
``echoed`` when it merely forwarded the carrier and ``recomputed`` when it genuinely re-derived the
contract; the adjective is the hop's claim, and this module records it verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from a2a_t.core.metadata import (
    NEGOTIATION_CONTEXT_METADATA_KEY,
    NEGOTIATION_T_EXTENSION_URI,
    TASK_T_EXTENSION_URI,
    TEMPLATE_URI_METADATA_KEY,
)
from a2a_t.core.plan_binding import canonical_digest
from shared.agreement import AGREEMENT_METADATA_KEY, REQUEST_ID_METADATA_KEY
from shared.approval import APPROVAL_METADATA_KEY
from shared.assurance import ASSURANCE_DECLARED, ASSURANCE_ECHOED, ROLE_CARRIER
from shared.change_order import BASELINE_METADATA_KEY
from shared.typed_payload import TYPED_TASK_T_METADATA_KEY

#: Metadata keys a hop must forward without rewriting. The extension URIs carry the rendered
#: Task-T/Negotiation-T documents; the sample-local keys carry the typed statement and the
#: envelopes a downstream server binds its answer to.
CONTRACT_KEYS: Final[tuple[str, ...]] = (
    TASK_T_EXTENSION_URI,
    NEGOTIATION_T_EXTENSION_URI,
    # A negotiation is not just its prose: the performative lives in negotiationContext and the
    # template URI selects the reply shape. Dropping either leaves a downstream peer unable to
    # answer, and no digest over the remaining keys can notice the loss.
    NEGOTIATION_CONTEXT_METADATA_KEY,
    TEMPLATE_URI_METADATA_KEY,
    TYPED_TASK_T_METADATA_KEY,
    AGREEMENT_METADATA_KEY,
    REQUEST_ID_METADATA_KEY,
    BASELINE_METADATA_KEY,
    APPROVAL_METADATA_KEY,
)

#: Metadata key of the delegation chain. It is written by hops, never carried verbatim.
DELEGATION_KEY: Final[str] = "a2at-sample/delegation"

#: Version of the structured delegation chain written by :func:`append_hop`.
DELEGATION_VERSION: Final[int] = 2

def carried(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Return the contract keys of ``metadata``, values unchanged.

    The returned map is a new dict, but its values are the caller's objects: normalising or
    copying them is exactly the alteration this module exists to detect. Key order follows
    :data:`CONTRACT_KEYS`, so two writers of the same content produce the same map.
    """
    if metadata is None:
        return {}
    return {key: metadata[key] for key in CONTRACT_KEYS if key in metadata}


def contract_digest(metadata: Mapping[str, object] | None) -> str | None:
    """Return the digest of what this hop received, or ``None`` when nothing was carried.

    The digest is computed over :func:`carried` only, so the map's non-contract keys (run
    correlation, presentation tags) do not change the claim. Any peer that received the same
    frames can recompute the same value with :func:`a2a_t.core.plan_binding.canonical_digest`.
    """
    values = carried(metadata)
    if not values:
        return None
    return canonical_digest({"carried": values})


def append_hop(
    metadata: Mapping[str, object] | None,
    *,
    hop: str,
    assurance: str,
    role: str = ROLE_CARRIER,
    revision: int | None = None,
    produces: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a new metadata map with this hop appended to the delegation chain.

    The carried keys are copied verbatim. The delegation key is upgraded to the v2 form
    ``{"version": 2, "hops": [...]}``; a legacy bare-string entry is upgraded to
    ``{"hop": <name>, "digest": "", "assurance": "declared"}`` because it named a hop but made no
    digest claim.

    Each hop records both halves of its own claim: ``digest`` is what it received and ``emitted`` is
    what it sends on. A carrier's two values are equal by construction; a reducer that changes the
    contract must pass ``revision`` so the change is attributable, and the next hop can then check
    that what it received is what the previous hop said it sent.

    Args:
        metadata: inbound metadata map, or ``None``.
        hop: name of this hop (``"oss"``, ``"nms"``, ...).
        assurance: this hop's claim about how it handled the contract (``"echoed"`` when it only
            forwarded the carrier, ``"recomputed"`` when it re-derived it).
        role: what this hop is allowed to do with the contract: ``"carrier"`` forwards it
            unchanged, ``"reducer"`` may change it and must name the revision it produced.
        revision: revision this hop produced, required when a reducer changes the contract.
        produces: contract keys this hop changed, overriding the carried values. Everything else
            is still forwarded as received, so a reducer states exactly what it rewrote.

    Returns:
        a new map containing only the carried contract keys plus :data:`DELEGATION_KEY`.
    """
    received = contract_digest(metadata)
    result: dict[str, object] = carried(metadata)
    if produces:
        result.update(produces)
    emitted = contract_digest(result)
    entry: dict[str, object] = {
        "hop": hop,
        "digest": received or "",
        "emitted": emitted or "",
        "role": role,
        "assurance": assurance,
    }
    if revision is not None:
        entry["revision"] = revision
    result[DELEGATION_KEY] = {
        "version": DELEGATION_VERSION,
        "hops": [*_existing_hops(metadata), entry],
    }
    return result


def verify_carried(
    inbound: Mapping[str, object] | None,
    outbound: Mapping[str, object] | None,
) -> bool:
    """Return whether every carried key of ``inbound`` survived into ``outbound`` unchanged.

    Extra keys on the outbound side are ignored: a hop may add presentation or correlation
    metadata. A missing key, or a key with a different value, fails the check. Values are compared
    with ``==`` so a JSON round-trip does not look like tampering.
    """
    received = carried(inbound)
    forwarded = carried(outbound)
    return all(key in forwarded and forwarded[key] == value for key, value in received.items())


def _existing_hops(metadata: Mapping[str, object] | None) -> list[dict[str, object]]:
    """Return the delegation entries of ``metadata`` in the v2 hop shape."""
    if metadata is None:
        return []
    raw = metadata.get(DELEGATION_KEY)
    if isinstance(raw, Mapping):
        entries = raw.get("hops")
        if not isinstance(entries, list):
            return []
    elif isinstance(raw, list):
        entries = raw
    else:
        return []
    hops: list[dict[str, object]] = []
    for entry in entries:
        if isinstance(entry, str):
            hops.append({"hop": entry, "digest": "", "assurance": ASSURANCE_DECLARED})
        elif isinstance(entry, Mapping):
            hops.append(dict(entry))
    return hops


__all__ = [
    "ASSURANCE_DECLARED",
    "ASSURANCE_ECHOED",
    "CONTRACT_KEYS",
    "DELEGATION_KEY",
    "DELEGATION_VERSION",
    "ROLE_CARRIER",
    "append_hop",
    "carried",
    "contract_digest",
    "verify_carried",
]
