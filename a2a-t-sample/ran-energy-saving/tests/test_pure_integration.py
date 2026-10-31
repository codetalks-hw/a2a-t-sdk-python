"""End-to-end tests of the pure A2A mode against a real in-process server."""

from __future__ import annotations

import pytest
from a2a_t.core.metadata import TASK_T_EXTENSION_URI
from demo.composition import build_demo_runtime
from demo.pure_policy import build_demo_pure_policy
from shared.config import DISCOVERY_DIRECT, TRANSPORT_SSE
from shared.run_constants import FINAL_STATE_TASK_COMPLETED, LANGUAGE_EN, PROTOCOL_PURE
from tests.integration_helpers import free_tcp_port, offline_direct_config, running_server


@pytest.mark.parametrize("scenario", ["night", "information", "target"])
async def test_a_pure_scenario_completes_end_to_end(scenario: str) -> None:
    port = free_tcp_port()
    config = offline_direct_config(port)

    with running_server(
        config,
        protocol_mode=PROTOCOL_PURE,
        pure_policy=build_demo_pure_policy(LANGUAGE_EN),
    ):
        runtime = await build_demo_runtime(
            config,
            scenario,
            "auto",
            TRANSPORT_SSE,
            DISCOVERY_DIRECT,
            protocol=PROTOCOL_PURE,
        )
        try:
            result = await runtime.run()
        finally:
            await runtime.aclose()

    assert result.final_state == FINAL_STATE_TASK_COMPLETED
    assert any(
        event.get("state") == "TASK_STATE_COMPLETED" for event in result.events
    )

    input_required = [
        event
        for event in result.events
        if event.get("state") == "TASK_STATE_INPUT_REQUIRED"
        and isinstance(event.get("metadata"), dict)
        and isinstance(event["metadata"].get("pure"), dict)
    ]
    if scenario == "night":
        assert input_required == []
    else:
        assert input_required

    for event in result.events:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        assert TASK_T_EXTENSION_URI not in metadata
        assert "templateUri" not in metadata
        assert "negotiationContext" not in metadata
