"""Focused tests of the client runtime assembly (no network)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from a2a.client import ClientConfig
from dotenv import dotenv_values
from protocol.client_runtime import (
    build_client_runtime,
    prepare_client_sdk_env,
)
from protocol.decision_provider import ManualDecisionProvider, ScriptedDecisionProvider
from protocol.discovery import DiscoveredAgent
from protocol.server_app import AGENT_CARD_NAME, AGENT_CARD_ORGANIZATION, build_agent_card
from shared.config import MODEL_MODE_LIVE, SampleConfig
from shared.scenario import SCENARIO_TARGET


class _FakeA2AClient:
    """a2a-sdk client double recording its close."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _card() -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_card=build_agent_card(host="127.0.0.1", port=8000),
        url="http://127.0.0.1:8000",
    )


async def test_manual_runtime_wires_scripted_env_and_streaming(
    offline_config: SampleConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import protocol.client_runtime as client_runtime

    captured: dict[str, object] = {}
    fake_client = _FakeA2AClient()

    async def fake_discover(**kwargs: object) -> DiscoveredAgent:
        captured["discover"] = kwargs
        return _card()

    async def fake_create(agent: object, client_config: ClientConfig | None = None) -> _FakeA2AClient:
        captured["agent"] = agent
        captured["client_config"] = client_config
        return fake_client

    monkeypatch.setattr(client_runtime, "discover_agent", fake_discover)
    monkeypatch.setattr(client_runtime, "create_client", fake_create)
    monkeypatch.setenv("A2AT_LANGUAGE", offline_config.language)

    runtime = await build_client_runtime(
        offline_config,
        "information",
        "manual",
        "sse",
        env_path=tmp_path / "missing.env",
    )

    assert isinstance(runtime.provider, ManualDecisionProvider)
    assert runtime.manual_provider is runtime.provider
    assert runtime.transport_name == "sse"
    values = dotenv_values(runtime.sdk_env.path)
    assert values["A2AT_LLM_PROVIDER"] == "scripted"
    assert values["A2AT_LLM_MODEL"] == "scripted"
    assert values["A2AT_LLM_API_KEY"] == "not-needed"
    assert values["A2AT_LANGUAGE"] == "en-US"
    client_config = captured["client_config"]
    assert isinstance(client_config, ClientConfig)
    assert client_config.streaming is True
    discovery = captured["discover"]
    assert isinstance(discovery, dict)
    assert discovery["organization"] == AGENT_CARD_ORGANIZATION
    assert discovery["name"] == AGENT_CARD_NAME
    assert discovery["direct_base_url"] == "http://127.0.0.1:8000"

    sdk_dir = runtime.sdk_env.directory
    await runtime.aclose()

    assert fake_client.closed is True
    assert not sdk_dir.exists()


async def test_scripted_mode_uses_the_scenario_decisions(
    offline_config: SampleConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import protocol.client_runtime as client_runtime

    async def fake_discover(**kwargs: object) -> DiscoveredAgent:
        del kwargs
        return _card()

    async def fake_create(agent: object, client_config: ClientConfig | None = None) -> _FakeA2AClient:
        del agent, client_config
        return _FakeA2AClient()

    monkeypatch.setattr(client_runtime, "discover_agent", fake_discover)
    monkeypatch.setattr(client_runtime, "create_client", fake_create)
    monkeypatch.setenv("A2AT_LANGUAGE", offline_config.language)

    runtime = await build_client_runtime(
        offline_config,
        SCENARIO_TARGET,
        "scripted",
        "sse",
        env_path=tmp_path / "missing.env",
    )

    assert isinstance(runtime.provider, ScriptedDecisionProvider)
    assert runtime.provider.remaining == 2
    await runtime.aclose()


def test_prepare_client_sdk_env_does_not_mutate_the_source(
    offline_config: SampleConfig,
    tmp_path: Path,
) -> None:
    source = tmp_path / ".env"
    original = "A2AT_LLM_PROVIDER=openai\nA2AT_LLM_MODEL=my-model\nA2AT_LLM_BASE_URL=http://example.test/v1\n"
    source.write_text(original, encoding="utf-8")
    live_config = replace(offline_config, model_mode=MODEL_MODE_LIVE)

    sdk_env = prepare_client_sdk_env(live_config, source)

    assert source.read_text(encoding="utf-8") == original
    values = dotenv_values(sdk_env.path)
    assert values["A2AT_LLM_PROVIDER"] == "openai"
    assert values["A2AT_LLM_MODEL"] == "my-model"
    assert values["A2AT_LLM_BASE_URL"] == "http://example.test/v1"
    assert values["A2AT_LANGUAGE"] == "en-US"
