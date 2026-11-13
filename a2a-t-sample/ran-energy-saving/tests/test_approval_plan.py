"""Tests of the S6 plan projection: the client and the simulator must reach the same object."""

from __future__ import annotations

from demo.approval_plan import plan_from_task_t
from demo.nms_sim.approval import build_request, plan_for
from demo.nms_sim.intent import ParsedIntent
from shared.approval import plan_digest


def _intent(**overrides: object) -> ParsedIntent:
    fields: dict[str, object] = {
        "operation": "Create",
        "region": "A2ATestRegion",
        "window": "00:00-22:00",
        "rate_mbps": 2.0,
        "sites": (),
        "source": "",
    }
    fields.update(overrides)
    return ParsedIntent(**fields)  # ty: ignore[invalid-argument-type]


def _filled(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "operationType": "Create",
        "region": "A2ATestRegion",
        "guaranteedThroughputTarget": "{00:00-22:00,2Mbps}",
    }
    fields.update(overrides)
    return fields


def test_the_client_projection_reaches_the_same_digest_as_the_server_plan() -> None:
    """The whole S6 claim: both sides can compute the same object from what they hold."""
    assert plan_digest(plan_from_task_t(_filled())) == plan_digest(plan_for(_intent()))


def test_a_rate_change_changes_the_digest_on_both_sides() -> None:
    client = plan_digest(plan_from_task_t(_filled(guaranteedThroughputTarget="{00:00-22:00,5Mbps}")))
    server = plan_digest(plan_for(_intent(rate_mbps=5.0)))

    assert client == server
    assert client != plan_digest(plan_from_task_t(_filled()))


def test_the_site_list_does_not_take_part() -> None:
    """A site list is server inventory: binding to it would make the digest unverifiable."""
    with_sites = _filled(siteList='["LNR_KPI_7745", "NES_27A_321071"]')

    assert plan_digest(plan_from_task_t(with_sites)) == plan_digest(plan_from_task_t(_filled()))


def test_the_tamper_switch_names_a_plan_the_server_will_not_execute() -> None:
    request = build_request(_intent(), task_id="task-1", ttl_seconds=60, tamper=True)

    assert request.plan_digest != plan_digest(plan_for(_intent()))
    assert request.plan_digest == plan_digest({**plan_for(_intent()), "rateMbps": 3.0})


def test_without_the_tamper_switch_the_request_names_the_executed_plan() -> None:
    request = build_request(_intent(), task_id="task-1", ttl_seconds=60)

    assert request.plan_digest == plan_digest(plan_for(_intent()))
