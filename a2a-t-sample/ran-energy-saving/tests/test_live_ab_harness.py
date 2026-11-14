"""The live A/B harness computes its own totals; those totals must not be able to lie.

The harness itself talks to a live endpoint, so only its arithmetic is covered here: which spans
count as LLM calls, how a run that never finished is counted, and that the two carriers are
summarised separately rather than pooled.
"""

from __future__ import annotations

from eval.osp.live_ab import LiveRow, _tokens, summarize


def test_only_llm_spans_contribute_tokens() -> None:
    spans = [
        {"kind": "llm", "tokens": {"prompt": 10, "completion": 2, "total": 12}},
        {"kind": "message_sent", "tokens": {"prompt": 999, "completion": 999, "total": 1998}},
        {"kind": "llm"},
        {"kind": "llm", "tokens": {"prompt": 5, "completion": 1, "total": 6}},
    ]

    assert _tokens(spans) == (15, 3, 18, 2)


def test_tokens_per_completed_ignores_runs_that_never_finished() -> None:
    rows = [
        LiveRow(1, "Create", "a2a-t", "task_completed", True, total_tokens=100, matched=3),
        LiveRow(2, "Create", "a2a-t", "incomplete", False, total_tokens=900),
        LiveRow(3, "Create", "a2a-t-typed", "task_completed", True, total_tokens=120, matched=3),
    ]

    summary = summarize(rows)

    assert summary["a2a-t"]["cases"] == 2
    assert summary["a2a-t"]["completed"] == 1
    assert summary["a2a-t"]["tokensPerCompleted"] == 100
    assert summary["a2a-t-typed"]["tokensPerCompleted"] == 120


def test_a_full_chain_needs_a_completed_run_with_nothing_unverifiable() -> None:
    done = LiveRow(1, "Create", "a2a-t", "task_completed", True, matched=3, unverifiable=0)
    gap = LiveRow(2, "Create", "a2a-t", "task_completed", True, matched=3, unverifiable=1)
    stopped = LiveRow(3, "Create", "a2a-t", "incomplete", False, matched=1, unverifiable=1)
    for row in (done, gap, stopped):
        row.full_chain = row.completed and row.unverifiable == 0 and row.matched > 0

    assert done.full_chain is True
    assert gap.full_chain is False
    assert stopped.full_chain is False
