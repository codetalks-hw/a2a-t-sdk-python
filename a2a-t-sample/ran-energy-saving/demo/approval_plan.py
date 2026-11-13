"""The sample's plan projection for S6 plan binding.

The server binds an approval to a canonical plan (``plan_for`` in ``demo/nms_sim/approval.py``).
For a client to *verify* that binding it has to reach the same object from what it sent. On the
A2A-T hop that is possible: the accepted Task-T carries the operation, the region and the target,
so the client can rebuild the same plan. On a plain-A2A hop there is no accepted document — only a
sequence of sentences — so the plain client can do nothing but echo the digest it was given.

This module is the sample's mapping from Task-T parameter names to the canonical plan. It is
deliberately the inverse of ``plan_for(ParsedIntent)``, and the tests pin the two together.
Nothing server-side (inventory, site names) takes part: a digest the client cannot compute is not
a binding, it is a claim.
"""

from __future__ import annotations

from collections.abc import Mapping

from protocol.target_grammar import find_segments

#: Task-T parameter names this projection reads.
OPERATION_FIELD = "operationType"
REGION_FIELD = "region"
TARGET_FIELD = "guaranteedThroughputTarget"


def plan_from_task_t(filled: Mapping[str, object]) -> dict[str, object]:
    """Return the canonical plan a filled Task-T represents."""
    target = filled.get(TARGET_FIELD)
    segments = find_segments(target)
    segment = segments[0] if segments else None
    return {
        "operation": str(filled.get(OPERATION_FIELD) or ""),
        "region": str(filled.get(REGION_FIELD) or ""),
        "window": f"{segment.start}-{segment.end}" if segment is not None else "",
        "rateMbps": segment.rate_mbps if segment is not None else 0.0,
    }


__all__ = ["plan_from_task_t"]
