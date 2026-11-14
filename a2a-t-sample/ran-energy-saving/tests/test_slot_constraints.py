"""The declarative slot checks: deterministic verdicts for the rules that are decidable.

The overlay's ``x-a2at-value-constraint`` blocks are prose. A model reads them while extracting, and
a model that misreads one costs a task. ``x-a2at-checks`` turns the decidable subset into rules the
SDK evaluates on its own (``A2AT_VALIDATE_SLOT_CONSTRAINTS``), so the verdict does not depend on the
extraction model remembering a sentence.

Two things are pinned here:

* **No false positives on the corpus.** Every case that has an expected Task-T must pass every
  check. The expected Task-T is the best oracle available for "this is a valid answer".
* **The corpus' own negative cases are caught.** Five cases are expected to be blocked because the
  request states a duration, a site list or a time window in a form the prose forbids; a regex can
  see the same thing without a model.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from a2a_t.client.prompt_generation.prompt_generation_orchestrator import (
    _declarative_check_errors,
)
from eval.osp.compare import parse_sections

CASE_DIR = Path("eval/osp/cases")
SLOT_SCHEMA = Path(
    "prompt_resources/slots/Task-T/network-layer/ran-energy-saving/v1/en-US/slot.json"
)

#: Task-T heading -> slot name. Only the slots the checks inspect are mapped.
HEADINGS: dict[str, str] = {
    "Operation Type": "operation_type",
    "Task Object": "task_object",
    "Task Target": "task_target",
    "Task Context": "task_context",
}


def _schema() -> dict[str, Any]:
    return json.loads(SLOT_SCHEMA.read_text(encoding="utf-8"))


def _slots(task_t: str) -> dict[str, str]:
    sections = parse_sections(task_t)
    return {
        name: sections[heading].strip()
        for heading, name in HEADINGS.items()
        if heading in sections
    }


def _case(name_fragment: str) -> dict[str, Any]:
    path = glob.glob(str(CASE_DIR / f"{name_fragment}-*.yaml"))[0]
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _labels(errors: list[Any]) -> list[str]:
    return [str((error.facts or {}).get("slot_label")) for error in errors]


def _codes_and_labels(errors: list[Any]) -> list[tuple[str, str]]:
    return [
        (str(error.code), str((error.facts or {}).get("slot_label"))) for error in errors
    ]


def _errors(case: dict[str, Any], task_t: str) -> list[Any]:
    return _declarative_check_errors(
        _schema(), _slots(task_t), "en-US", case.get("user_input")
    )


def _expected_task_t(case: dict[str, Any]) -> str:
    protocols = (case.get("expect") or {}).get("protocols") or {}
    return str(((protocols.get("a2a-t") or {}).get("task_t")) or "")


def _expected_verdict(case: dict[str, Any]) -> str | None:
    protocols = (case.get("expect") or {}).get("protocols") or {}
    verdict = (protocols.get("a2a-t") or {}).get("validate")
    return verdict if isinstance(verdict, str) else None


def test_the_corpus_expected_task_t_passes_every_check() -> None:
    """A rule that rejects a correct answer is worse than no rule: sweep the whole corpus."""
    checked = 0
    failures: list[str] = []
    for path in sorted(glob.glob(str(CASE_DIR / "*.yaml"))):
        case = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        task_t = _expected_task_t(case)
        # A blocked case still carries the Task-T the extractor produced before the gate stopped
        # it; that text is expected to violate a rule, so it cannot be the valid-answer oracle.
        if not task_t or _expected_verdict(case) == "blocked":
            continue
        checked += 1
        errors = _errors(case, task_t)
        if errors:
            failures.append(f"{Path(path).name}: {[(e.code, e.facts) for e in errors]}")

    # The canonical baseline is the 28 cases that reach the server (20 and 22 are blocked).
    assert checked >= 28, f"only {checked} cases carried a sent Task-T"
    assert failures == []


@pytest.mark.parametrize(
    ("name_fragment", "label"),
    [
        ("26", "Outage duration"),
        ("30", "Site list"),
        ("24", "Carrier frequencies"),
        ("31", "Time windows"),
        ("38", "Site list"),
    ],
)
def test_a_corpus_blocked_case_is_caught_by_a_declarative_rule(
    name_fragment: str, label: str
) -> None:
    """Each of these is expected to be blocked; the reason is visible without a model."""
    case = _case(name_fragment)

    errors = _declarative_check_errors(
        _schema(), {}, "en-US", case.get("user_input")
    )

    labels = _labels(errors)
    assert label in labels, labels


def test_a_vague_rate_is_not_a_value() -> None:
    errors = _declarative_check_errors(
        _schema(),
        {},
        "en-US",
        "Create energy saving in A2ATestRegion and maximize the rate.",
    )

    assert _codes_and_labels(errors) == [("slot.not_provided", "Guaranteed rate")]


def test_a_vague_rate_next_to_a_concrete_one_passes() -> None:
    """The negative case above is about a missing value, not about the word itself."""
    errors = _declarative_check_errors(
        _schema(),
        {},
        "en-US",
        "Create energy saving in A2ATestRegion, maximize the rate but keep 5 Mbps from 00:00 to 06:00.",
    )

    assert errors == []


def test_an_invalid_rat_in_the_request_is_rejected() -> None:
    """Input-scoped on purpose: the prose forbids normalizing an invalid RAT to a valid one."""
    errors = _declarative_check_errors(
        _schema(),
        {},
        "en-US",
        "Use 3G in A2ATestRegion.",
    )

    assert _codes_and_labels(errors) == [("slot.constraint_violated", "Cell RAT")]


def test_a_gain_target_in_another_shape_is_rejected() -> None:
    errors = _declarative_check_errors(
        _schema(),
        {"task_target": "Energy consumption goal: 30%"},
        "en-US",
        "Set the gain target to 30%.",
    )

    assert _codes_and_labels(errors) == [
        ("slot.constraint_violated", "Energy saving gain target")
    ]


def test_the_canonical_gain_target_is_accepted() -> None:
    errors = _declarative_check_errors(
        _schema(),
        {"task_target": "energy saving gain target: 30%"},
        "en-US",
        "Set the gain target to 30%.",
    )

    assert errors == []


def test_the_chinese_overlay_carries_the_same_rules() -> None:
    """Both language resources are maintained by hand; a rule present in one must exist in the other."""
    zh = json.loads(
        SLOT_SCHEMA.parent.parent.joinpath("zh-CN", "slot.json").read_text(encoding="utf-8")
    )

    assert len(zh["x-a2at-checks"]) == len(_schema()["x-a2at-checks"])
    assert {check["error"] for check in zh["x-a2at-checks"]} == {
        check["error"] for check in _schema()["x-a2at-checks"]
    }
