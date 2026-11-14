"""P5: replay one exchange from artifacts alone.

The audit chain is the claim that matters for a production discussion: given what both sides
published, a third party can decide every link with a digest comparison -- no model, no log reading.
A plain-A2A run is not "failed" here; it is *unverifiable*, which is the honest verdict when the
accepted response is prose.
"""

from __future__ import annotations

from collections.abc import Mapping

from shared.approval import plan_digest
from shared.assurance import ASSURANCE_RECOMPUTED, Hop, report
from shared.audit import (
    LINK_APPROVAL,
    LINK_BASELINE,
    LINK_EXECUTION,
    LINK_MATCHED,
    LINK_MISMATCHED,
    LINK_NOT_APPLICABLE,
    LINK_REQUEST,
    LINK_RESTORED,
    LINK_UNVERIFIABLE,
    Artifacts,
    summarize,
    verify_chain,
)

PLAN: dict[str, object] = {
    "operation": "Create",
    "region": "A2ATestRegion",
    "window": "00:00-06:00",
    "rateMbps": 10.0,
}


def _statuses(links: tuple[object, ...]) -> dict[str, str]:
    return {getattr(link, "name"): getattr(link, "status") for link in links}


def _full_chain(
    *,
    requested_plan: Mapping[str, object] | None = PLAN,
    accepted_plan: Mapping[str, object] | None = PLAN,
    approved_digest: str | None = None,
    executed_plan: Mapping[str, object] | None = PLAN,
    receipt_digest: str | None = None,
    baseline_plan: Mapping[str, object] | None = None,
    baseline_digest: str | None = None,
    reversal_plan: Mapping[str, object] | None = None,
    restored_plan: Mapping[str, object] | None = None,
    negotiated: bool = False,
) -> Artifacts:
    """Build one chain whose links agree unless the caller overrides that side."""
    return Artifacts(
        requested_plan=requested_plan,
        accepted_plan=accepted_plan,
        approved_digest=approved_digest or plan_digest(PLAN),
        executed_plan=executed_plan,
        receipt_digest=receipt_digest or plan_digest(PLAN),
        baseline_plan=baseline_plan,
        baseline_digest=baseline_digest,
        reversal_plan=reversal_plan,
        restored_plan=restored_plan,
        negotiated=negotiated,
    )


def test_a_complete_a2a_t_chain_is_decided_on_every_link() -> None:
    links = verify_chain(_full_chain())

    assert _statuses(links) == {
        LINK_REQUEST: LINK_MATCHED,
        LINK_APPROVAL: LINK_MATCHED,
        LINK_EXECUTION: LINK_MATCHED,
        # A Create replaces nothing, so the baseline question does not exist for this run.
        LINK_BASELINE: LINK_NOT_APPLICABLE,
    }
    assert summarize(links) == {
        "links": 4,
        "applicable": 3,
        "matched": 3,
        "mismatched": 0,
        "unverifiable": 0,
        "notApplicable": 1,
        "decidedBy": "digest",
    }


def test_a_plain_run_is_unverifiable_after_the_request_link() -> None:
    """The honest baseline: a prose acceptance leaves nothing to compare against."""
    links = verify_chain(Artifacts(requested_plan=PLAN))

    assert _statuses(links) == {
        LINK_REQUEST: LINK_UNVERIFIABLE,
        LINK_APPROVAL: LINK_UNVERIFIABLE,
        LINK_EXECUTION: LINK_UNVERIFIABLE,
        LINK_BASELINE: LINK_NOT_APPLICABLE,
    }
    summary = summarize(links)
    assert summary["matched"] == 0
    assert summary["unverifiable"] == 3


def test_a_negotiated_acceptance_is_not_a_mismatch() -> None:
    """The accepted plan may differ from the request *by agreement*; that link does not apply."""
    negotiated_plan = {**PLAN, "rateMbps": 2.0}

    links = verify_chain(
        _full_chain(
            accepted_plan=negotiated_plan,
            approved_digest=plan_digest(negotiated_plan),
            executed_plan=negotiated_plan,
            receipt_digest=plan_digest(negotiated_plan),
            negotiated=True,
        )
    )

    assert _statuses(links)[LINK_REQUEST] == LINK_NOT_APPLICABLE
    assert summarize(links)["notApplicable"] == 2


def test_an_approval_of_another_plan_is_a_mismatch() -> None:
    other = {**PLAN, "rateMbps": 18.0}

    links = verify_chain(_full_chain(approved_digest=plan_digest(other)))

    statuses = _statuses(links)
    assert statuses[LINK_APPROVAL] == LINK_MISMATCHED
    # The receipt still names the real plan, so it cannot match a wrong approval either: the chain
    # reports both, and that is the honest reading (the approval was for something else).
    assert statuses[LINK_EXECUTION] == LINK_MISMATCHED
    assert summarize(links)["mismatched"] == 2


def test_an_execution_that_differs_from_the_accepted_plan_is_a_mismatch() -> None:
    executed = {**PLAN, "rateMbps": 6.0}

    links = verify_chain(
        _full_chain(executed_plan=executed, receipt_digest=plan_digest(PLAN))
    )

    assert _statuses(links)[LINK_EXECUTION] == LINK_MISMATCHED


def test_a_receipt_for_another_plan_is_a_mismatch_even_without_the_executed_plan() -> None:
    links = verify_chain(
        _full_chain(receipt_digest=plan_digest({**PLAN, "region": "OtherRegion"}))
    )

    assert _statuses(links)[LINK_EXECUTION] == LINK_MISMATCHED


def test_a_receipt_that_agrees_with_the_approval_but_carries_no_plan_is_unverifiable() -> None:
    """Reaching the missing-plan branch needs the approval and receipt to agree first.

    With a differing receipt the earlier approval-vs-receipt check answers, so a test that only
    varies the receipt never exercises the branch this one is named after.
    """
    links = verify_chain(_full_chain(executed_plan=None, receipt_digest=plan_digest(PLAN)))

    execution = next(link for link in links if link.name == LINK_EXECUTION)
    assert execution.status == LINK_UNVERIFIABLE
    assert execution.detail


def test_a_change_order_is_checked_against_the_baseline_it_declared() -> None:
    previous = {**PLAN, "rateMbps": 30.0}
    changed = {**PLAN, "rateMbps": 10.0}
    good = verify_chain(
        _full_chain(
            requested_plan=changed,
            accepted_plan=changed,
            approved_digest=plan_digest(changed),
            executed_plan=changed,
            receipt_digest=plan_digest(changed),
            baseline_plan=previous,
            baseline_digest=plan_digest(previous),
        )
    )
    stale = verify_chain(
        _full_chain(
            requested_plan=changed,
            accepted_plan=changed,
            approved_digest=plan_digest(changed),
            executed_plan=changed,
            receipt_digest=plan_digest(changed),
            baseline_plan=previous,
            baseline_digest=plan_digest({**previous, "rateMbps": 5.0}),
        )
    )

    assert _statuses(good)[LINK_BASELINE] == LINK_MATCHED
    assert _statuses(stale)[LINK_BASELINE] == LINK_MISMATCHED


def test_an_unrelated_server_side_value_does_not_break_the_chain() -> None:
    """The chain covers the declared plan fields only: inventory cannot enter the verdict."""
    noisy: Mapping[str, object] = {
        **PLAN,
        "sites": ["LNR_KPI_7745"],
        "savingRate": 0.3,
    }

    links = verify_chain(_full_chain(requested_plan=noisy, accepted_plan=noisy))

    assert _statuses(links)[LINK_REQUEST] == LINK_MATCHED


def test_a_readback_without_the_executed_plan_is_unverifiable_not_agreement() -> None:
    """A matching digest only ties the readback to the approval; the values were never compared."""
    links = verify_chain(_full_chain(executed_plan=None, receipt_digest=plan_digest(PLAN)))

    execution = next(link for link in links if link.name == LINK_EXECUTION)
    assert execution.status == LINK_UNVERIFIABLE
    assert "no executed plan" in execution.detail


def test_a_declared_baseline_without_its_plan_is_not_reported_as_absent() -> None:
    """A baseline WAS carried; the report must say we cannot recompute it, not that it is missing."""
    links = verify_chain(_full_chain(baseline_plan=None, baseline_digest=plan_digest(PLAN)))
    baseline = next(link for link in links if link.name == LINK_BASELINE)

    assert baseline.status == LINK_UNVERIFIABLE
    assert "no baseline plan was carried" in baseline.detail
    assert baseline.detail != "no change order on this run"


def test_a_baseline_plan_without_a_declared_digest_is_unverifiable() -> None:
    links = verify_chain(_full_chain(baseline_plan=PLAN, baseline_digest=None))
    baseline = next(link for link in links if link.name == LINK_BASELINE)

    assert baseline.status == LINK_UNVERIFIABLE
    assert "no digest was declared" in baseline.detail


def test_a_link_serialises_itself() -> None:
    link = verify_chain(_full_chain())[0]

    assert link.to_dict() == {
        "name": LINK_REQUEST,
        "status": LINK_MATCHED,
        "detail": "",
    }


def test_a_run_without_a_reversal_contract_keeps_exactly_four_links() -> None:
    links = verify_chain(_full_chain())

    assert len(links) == 4
    assert LINK_RESTORED not in _statuses(links)


def test_a_reversal_contract_that_was_not_executed_is_not_applicable() -> None:
    reversal = {**PLAN, "operation": "Restore", "rateMbps": 100.0}

    links = verify_chain(_full_chain(reversal_plan=reversal))
    restored = next(link for link in links if link.name == LINK_RESTORED)

    assert restored.status == LINK_NOT_APPLICABLE
    assert restored.detail == "the reversal contract was not executed"
    assert len(links) == 5


def test_an_executed_reversal_is_compared_with_the_contract() -> None:
    reversal = {**PLAN, "operation": "Restore", "rateMbps": 100.0}

    links = verify_chain(_full_chain(reversal_plan=reversal, restored_plan=dict(reversal)))

    assert _statuses(links)[LINK_RESTORED] == LINK_MATCHED
    assert summarize(links)["matched"] == 4


def test_a_restoration_that_differs_from_the_contract_is_a_mismatch() -> None:
    reversal = {**PLAN, "operation": "Restore", "rateMbps": 100.0}
    restored = {**reversal, "rateMbps": 77.0}

    links = verify_chain(_full_chain(reversal_plan=reversal, restored_plan=restored))

    assert _statuses(links)[LINK_RESTORED] == LINK_MISMATCHED


def test_the_summary_gains_the_assurance_report_only_when_asked() -> None:
    links = verify_chain(_full_chain())
    graded = report([Hop("ui", plan_digest(PLAN), ASSURANCE_RECOMPUTED)])

    plain = summarize(links)
    with_assurance = summarize(links, assurance=graded)

    assert "assurance" not in plain
    assert with_assurance["assurance"] == graded.to_dict()
    assert {key: value for key, value in with_assurance.items() if key != "assurance"} == plain


def test_a_create_has_no_baseline_question_to_answer() -> None:
    """A Create replaces nothing, so the link does not exist rather than going unanswered."""
    links = verify_chain(_full_chain(baseline_plan=None, baseline_digest=None))

    baseline = next(link for link in links if link.name == LINK_BASELINE)
    assert baseline.status == LINK_NOT_APPLICABLE
    assert "not a change order" in baseline.detail


def test_a_change_order_without_a_baseline_is_unverifiable() -> None:
    changed = {**PLAN, "operation": "Modify"}

    links = verify_chain(
        _full_chain(requested_plan=changed, accepted_plan=changed, baseline_plan=None)
    )

    baseline = next(link for link in links if link.name == LINK_BASELINE)
    assert baseline.status == LINK_UNVERIFIABLE
    assert "no change order on this run" in baseline.detail
