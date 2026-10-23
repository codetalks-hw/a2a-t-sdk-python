"""Tests for the unified sample configuration and model selection."""

from __future__ import annotations

from pathlib import Path

import pytest
from shared.adk_fake_model import FakeLlm
from shared.config import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_LLM_MAX_ATTEMPTS,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_WEBHOOK_IDLE_TIMEOUT,
    HTTP_TIMEOUT_ENV_KEY,
    LLM_MAX_ATTEMPTS_ENV_KEY,
    LLM_TIMEOUT_ENV_KEY,
    MODEL_MODE_LIVE,
    MODEL_MODE_OFFLINE,
    SampleConfig,
    build_adk_model,
    load_config,
)

_TIMEOUT_KEYS = (HTTP_TIMEOUT_ENV_KEY, LLM_TIMEOUT_ENV_KEY, LLM_MAX_ATTEMPTS_ENV_KEY)


@pytest.fixture(autouse=True)
def _clear_timeout_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the process environment from leaking timeout values into the file-based tests."""
    for key in _TIMEOUT_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_offline_config_loads_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "A2AT_SAMPLE_MODEL_MODE=offline",
                "A2AT_LANGUAGE=en-US",
                "A2AT_SAMPLE_HOST=0.0.0.0",
                "A2AT_SAMPLE_PORT=8100",
                "REGISTRY_CENTER_HOST=registry.local",
                "REGISTRY_CENTER_PORT=5002",
                "A2AT_SAMPLE_WEBHOOK_PORT=0",
                "A2AT_SAMPLE_WEBHOOK_TOKEN=token-123",
                "A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT=15",
                "ADK_MODEL_NAME=openai/test-model",
                "ADK_MODEL_API_BASE=http://127.0.0.1:1234/v1",
                "ADK_MODEL_API_KEY=secret",
                "A2AT_SAMPLE_DISCOVERY=direct",
                "A2AT_SAMPLE_TRANSPORT=webhook",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(env_path=env_file)

    assert config.model_mode == MODEL_MODE_OFFLINE
    assert config.is_offline
    assert config.model_label == "offline (scripted)"
    assert config.language == "en-US"
    assert config.host == "0.0.0.0"
    assert config.port == 8100
    assert config.registry_url == "http://registry.local:5002"
    assert config.webhook_port == 0
    assert config.webhook_token == "token-123"
    assert config.webhook_idle_timeout == 15
    assert config.discovery == "direct"
    assert config.transport == "webhook"


def test_missing_env_file_uses_offline_defaults(tmp_path: Path) -> None:
    config = load_config(env_path=tmp_path / "missing.env")

    assert config.model_mode == MODEL_MODE_OFFLINE
    assert config.registry_url == "http://127.0.0.1:5001"
    assert config.http_timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS == 180.0
    assert config.llm_timeout_seconds == DEFAULT_LLM_TIMEOUT_SECONDS == 30.0
    assert config.llm_max_attempts == DEFAULT_LLM_MAX_ATTEMPTS == 1
    assert config.webhook_idle_timeout == DEFAULT_WEBHOOK_IDLE_TIMEOUT == 180
    assert DEFAULT_WEBHOOK_IDLE_TIMEOUT == int(DEFAULT_HTTP_TIMEOUT_SECONDS)


def test_timeout_budgets_are_read_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"{HTTP_TIMEOUT_ENV_KEY}=240",
                f"{LLM_TIMEOUT_ENV_KEY}=12.5",
                f"{LLM_MAX_ATTEMPTS_ENV_KEY}=3",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(env_path=env_file)

    assert config.http_timeout_seconds == 240.0
    assert config.llm_timeout_seconds == 12.5
    assert config.llm_max_attempts == 3


@pytest.mark.parametrize("key", [HTTP_TIMEOUT_ENV_KEY, LLM_TIMEOUT_ENV_KEY])
@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "nan", "inf"])
def test_timeout_budgets_reject_non_positive_values(
    tmp_path: Path, key: str, value: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}={value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=key):
        load_config(env_path=env_file)


@pytest.mark.parametrize("value", ["not-a-number", "0", "11", "-1"])
def test_llm_max_attempts_rejects_out_of_range_values(tmp_path: Path, value: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{LLM_MAX_ATTEMPTS_ENV_KEY}={value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=LLM_MAX_ATTEMPTS_ENV_KEY):
        load_config(env_path=env_file)


def test_invalid_model_mode_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A2AT_SAMPLE_MODEL_MODE=network\n", encoding="utf-8")

    with pytest.raises(ValueError, match="A2AT_SAMPLE_MODEL_MODE"):
        load_config(env_path=env_file)


def test_build_adk_model_returns_fake_model_offline(offline_config: SampleConfig) -> None:
    model = build_adk_model(offline_config)

    assert isinstance(model, FakeLlm)


def test_build_adk_model_rejects_empty_live_model_name(offline_config: SampleConfig) -> None:
    from dataclasses import replace

    live_config = replace(offline_config, model_mode=MODEL_MODE_LIVE, adk_model_name="")

    with pytest.raises(ValueError, match="ADK_MODEL_NAME"):
        build_adk_model(live_config)
