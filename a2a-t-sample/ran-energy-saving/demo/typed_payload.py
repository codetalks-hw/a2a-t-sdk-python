"""Project the filled Task-T values onto a typed statement (v2 prototype).

The rendered Task-T and the typed statement carry the same business values. Keeping the projection
here means the protocol layer only knows "a callable that turns filled values into a statement", and
the sample owns its field names -- the same split the plan projection already uses.
"""

from __future__ import annotations

from collections.abc import Mapping

from protocol.target_grammar import find_segments
from shared.typed_payload import TypedStatement, build_statement

#: Task-T parameter names this projection reads.
OPERATION_FIELD = "operationType"
REGION_FIELD = "region"
TARGET_FIELD = "guaranteedThroughputTarget"
SAVING_RATE_FIELD = "savingRate"


def typed_statement_from_task_t(filled: Mapping[str, object]) -> TypedStatement | None:
    """Return the typed statement of one filled Task-T, ``None`` when it is not yet stateable.

    A statement is only built when the operation and the region are known: sending a partial
    statement would claim to be the contract while carrying less than the request. Negotiation
    is what completes a partial request, and it works on the text path.
    """
    operation = str(filled.get(OPERATION_FIELD) or "").strip()
    region = str(filled.get(REGION_FIELD) or "").strip()
    if not operation or not region:
        return None
    scopes = [
        {"window": f"{segment.start}-{segment.end}", "rateMbps": segment.rate_mbps}
        for segment in find_segments(filled.get(TARGET_FIELD))
    ]
    percent = filled.get(SAVING_RATE_FIELD)
    return build_statement(
        operation=operation,
        region=region,
        scopes=scopes,
        saving_rate_percent=(
            float(percent)
            if isinstance(percent, (int, float)) and not isinstance(percent, bool)
            else None
        ),
    )


__all__ = ["typed_statement_from_task_t"]
