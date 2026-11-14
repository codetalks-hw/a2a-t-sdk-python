"""Policy guard for the sample overlay's declarative slot checks.

**2026-11-11 (run ``76a9ab9a``): the ban.** The overlay used to carry ``x-a2at-checks`` rules and
they failed in both directions: a correct site name outside a rule's assumed naming convention was
reported as "Site list is not provided", and an incomplete request got through because the word
"needs" matched a ``NE`` prefix branch. The verdict was returned to the extraction model's
``slot_errors`` and the overlay was left clean.

**2026-11-14: the ban is replaced by a narrower one.** The reintroduced rules are a different
shape from the ones that failed:

* they key on **phrases the prose explicitly declares invalid** ("more than three hours", "sites
  such as X", "approximately 1600", "idle period"), never on a value's naming convention;
* they are gated before they can veto anything -- ``A2AT_VALIDATE_SLOT_CONSTRAINTS`` remains off by
  default in the SDK, so a wrong rule cannot silently block a request in a run that did not opt in;
* they are checked against the corpus: ``tests/test_slot_constraints.py`` requires every one of the
  28 sent Task-Ts to pass every rule, and requires the five corpus cases that are expected to be
  blocked for a decidable reason to be caught.

The guard that remains is structural: every check must name a slot that exists in *that* language's
schema (the English and Chinese overlays use different slot names), carry a known error code, and
declare where it reads from. Anything else is an authoring accident.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_OVERLAY = (
    Path(__file__).resolve().parents[1]
    / "prompt_resources/slots/Task-T/network-layer/ran-energy-saving/v1"
)

#: Error codes a declarative check may report. Anything else is mis-coded and would be ignored by
#: the evaluator, hiding the rule.
_ALLOWED_ERRORS = {"slot.not_provided", "slot.constraint_violated"}

#: Where a check may read from. ``source`` defaults to ``both`` in the evaluator, so an explicit
#: value is optional; when present it must be one of these.
_ALLOWED_SOURCES = {"input", "slot", "both"}


def _schema(language: str) -> dict[str, Any]:
    return json.loads((_OVERLAY / language / "slot.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_every_declarative_check_is_well_formed(language: str) -> None:
    schema = _schema(language)
    checks = schema["x-a2at-checks"]
    properties = set(schema["properties"])

    assert checks, "the overlay opts in to the deterministic layer; it must carry rules"
    for check in checks:
        assert check["slot"] in properties, (check, sorted(properties))
        assert isinstance(check["when"], str) and check["when"]
        assert check["error"] in _ALLOWED_ERRORS, check
        if "source" in check:
            assert check["source"] in _ALLOWED_SOURCES, check
        if "slot_label" in check:
            assert isinstance(check["slot_label"], str) and check["slot_label"]


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_a_check_that_reads_the_extracted_slot_is_guarded(language: str) -> None:
    """Slot-scoped inspection is the shape that failed in 2026-11-11.

    A rule that reads the extracted value must either be scoped to the operator's input (``source``
    ``input``) or carry a ``require`` pattern: it may not veto a value on the ``when`` pattern alone.
    """
    for check in _schema(language)["x-a2at-checks"]:
        if check.get("source") == "input":
            continue
        assert "require" in check, check
