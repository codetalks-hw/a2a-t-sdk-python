"""A typed Task-T statement: the contract is data, the rendered prompt is a view (v2 prototype).

In v1 the Task-T "structure" is a rendered document with ``## Heading`` sections. Every party that
wants to read it parses prose: the sample's simulator with regular expressions, a real NMS with a
model. The digest needs a separately agreed field list, and the constraints live in prose.

This module carries the same statement as data. The rendered prompt stays for humans and for peers
that only speak v1; when a statement is present, *it* is the source of truth and the text is
derived from it. That is what makes a digest a protocol value instead of a product convention.

The metadata key is sample-local for now. Promoting it means registering an extension URI; the
shape below is the candidate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: Metadata key of the typed statement. Prototype key: a registered extension URI replaces it.
TYPED_TASK_T_METADATA_KEY: Final[str] = "a2at-sample/task-t-v2"

#: Schema the statement conforms to. A peer that does not know it must fall back to the text.
TYPED_SCHEMA_URI: Final[str] = "https://a2at-sample.local/schemas/ran-energy-saving/v2"

#: Operations a statement may name.
OPERATION_CREATE: Final[str] = "Create"
OPERATION_MODIFY: Final[str] = "Modify"
SUPPORTED_OPERATIONS: Final[tuple[str, ...]] = (OPERATION_CREATE, OPERATION_MODIFY)

#: One ``HH:MM-HH:MM`` window, as the target grammar and the readback already spell it.
_WINDOW: Final[re.Pattern[str]] = re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")


@dataclass(frozen=True)
class Scope:
    """One time-segmented rate guarantee.

    Attributes:
        window: ``HH:MM-HH:MM`` window the guarantee applies to.
        rate_mbps: Guaranteed throughput in Mbps.
    """

    window: str
    rate_mbps: float


@dataclass(frozen=True)
class TypedStatement:
    """The business statement of one Task-T, as data.

    Attributes:
        operation: ``Create`` or ``Modify``.
        region: Region the request acts on.
        scopes: Time-segmented rate guarantees, in the operator's order.
        saving_rate_percent: Energy-saving gain target, when the operator stated one.
        sites: Concrete site names for a site-level request, empty for an area-level one.
        schema_uri: Schema this statement conforms to.
    """

    operation: str
    region: str
    scopes: tuple[Scope, ...] = ()
    saving_rate_percent: float | None = None
    sites: tuple[str, ...] = ()
    schema_uri: str = TYPED_SCHEMA_URI


def build_statement(
    *,
    operation: str,
    region: str,
    scopes: Sequence[Mapping[str, object]] = (),
    saving_rate_percent: float | None = None,
    sites: Sequence[str] = (),
) -> TypedStatement:
    """Build a statement from already-parsed values.

    Args:
        operation: ``Create`` or ``Modify``.
        region: Region the request acts on.
        scopes: Mappings with ``window`` and ``rateMbps``.
        saving_rate_percent: Gain target, when stated.
        sites: Concrete site names, when the request is site-level.

    Returns:
        The statement.
    """
    return TypedStatement(
        operation=operation,
        region=region,
        scopes=tuple(
            Scope(
                window=str(scope.get("window") or ""),
                rate_mbps=_rate(scope.get("rateMbps")),
            )
            for scope in scopes
        ),
        saving_rate_percent=saving_rate_percent,
        sites=tuple(str(site) for site in sites),
    )


def _rate(value: object) -> float:
    """Return one non-negative Mbps value, ``0.0`` when it is not a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def statement_metadata(statement: TypedStatement) -> dict[str, object]:
    """Return the metadata entry carrying one statement."""
    return {
        TYPED_TASK_T_METADATA_KEY: {
            "schemaUri": statement.schema_uri,
            "operation": statement.operation,
            "region": statement.region,
            "scopes": [
                {"window": scope.window, "rateMbps": scope.rate_mbps}
                for scope in statement.scopes
            ],
            **(
                {"savingRatePercent": statement.saving_rate_percent}
                if statement.saving_rate_percent is not None
                else {}
            ),
            **({"sites": list(statement.sites)} if statement.sites else {}),
        }
    }


def read_statement(metadata: Mapping[str, object] | None) -> TypedStatement | None:
    """Read and validate one statement, ``None`` when absent.

    A statement that is present but malformed raises :class:`ValueError`: unlike the sample's
    optional envelopes, a typed statement claims to *be* the contract, so silently ignoring it
    would execute something the peer did not state.

    Raises:
        ValueError: when the statement is malformed or names an unsupported schema/operation.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(TYPED_TASK_T_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("typed.invalid: the statement must be an object")
    schema_uri = raw.get("schemaUri")
    if schema_uri != TYPED_SCHEMA_URI:
        raise ValueError(f"typed.invalid: unsupported schemaUri {schema_uri!r}")
    operation = raw.get("operation")
    if operation not in SUPPORTED_OPERATIONS:
        raise ValueError(f"typed.invalid: operation must be one of {SUPPORTED_OPERATIONS}")
    region = raw.get("region")
    if not isinstance(region, str) or not region.strip():
        raise ValueError("typed.invalid: region is required")
    scopes = raw.get("scopes") or []
    if not isinstance(scopes, list):
        raise ValueError("typed.invalid: scopes must be a list")
    parsed: list[Scope] = []
    for entry in scopes:
        if not isinstance(entry, Mapping):
            raise ValueError("typed.invalid: each scope must be an object")
        window = entry.get("window")
        if not isinstance(window, str) or _WINDOW.match(window) is None:
            raise ValueError("typed.invalid: scope.window must be HH:MM-HH:MM")
        rate = entry.get("rateMbps")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0:
            raise ValueError("typed.invalid: scope.rateMbps must be a non-negative number")
        parsed.append(Scope(window=window, rate_mbps=float(rate)))
    percent = raw.get("savingRatePercent")
    if percent is not None and (
        not isinstance(percent, (int, float)) or isinstance(percent, bool) or percent < 0
    ):
        raise ValueError("typed.invalid: savingRatePercent must be a non-negative number")
    sites = raw.get("sites") or []
    if not isinstance(sites, list) or any(not isinstance(site, str) for site in sites):
        raise ValueError("typed.invalid: sites must be a list of names")
    return TypedStatement(
        operation=str(operation),
        region=region.strip(),
        scopes=tuple(parsed),
        saving_rate_percent=float(percent) if percent is not None else None,
        sites=tuple(str(site) for site in sites),
        schema_uri=TYPED_SCHEMA_URI,
    )


__all__ = [
    "OPERATION_CREATE",
    "OPERATION_MODIFY",
    "SUPPORTED_OPERATIONS",
    "TYPED_SCHEMA_URI",
    "TYPED_TASK_T_METADATA_KEY",
    "Scope",
    "TypedStatement",
    "build_statement",
    "read_statement",
    "statement_metadata",
]
