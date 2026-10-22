"""Unified configuration of the RAN energy-saving ADK sample.

All values come from the sample's ``.env`` (falling back to process environment variables and then
to defaults). ``build_adk_model`` returns a ``LiteLlm`` pointing at the configured
OpenAI-compatible endpoint in ``live`` mode, and the deterministic fake model in ``offline`` mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from google.adk.models.base_llm import BaseLlm

from shared.scenario import LANGUAGE_ZH, SUPPORTED_LANGUAGES

#: Model modes.
MODEL_MODE_LIVE = "live"
MODEL_MODE_OFFLINE = "offline"
SUPPORTED_MODEL_MODES = (MODEL_MODE_LIVE, MODEL_MODE_OFFLINE)
ModelMode = Literal["live", "offline"]

#: Discovery modes.
DISCOVERY_REGISTRY = "registry"
DISCOVERY_DIRECT = "direct"
SUPPORTED_DISCOVERY_MODES = (DISCOVERY_REGISTRY, DISCOVERY_DIRECT)

#: Transport modes.
TRANSPORT_SSE = "sse"
TRANSPORT_WEBHOOK = "webhook"
SUPPORTED_TRANSPORTS = (TRANSPORT_SSE, TRANSPORT_WEBHOOK)

#: ``.env`` keys.
MODEL_MODE_ENV_KEY = "A2AT_SAMPLE_MODEL_MODE"
LANGUAGE_ENV_KEY = "A2AT_LANGUAGE"
HOST_ENV_KEY = "A2AT_SAMPLE_HOST"
PORT_ENV_KEY = "A2AT_SAMPLE_PORT"
REGISTRY_HOST_ENV_KEY = "REGISTRY_CENTER_HOST"
REGISTRY_PORT_ENV_KEY = "REGISTRY_CENTER_PORT"
WEBHOOK_PORT_ENV_KEY = "A2AT_SAMPLE_WEBHOOK_PORT"
WEBHOOK_TOKEN_ENV_KEY = "A2AT_SAMPLE_WEBHOOK_TOKEN"
WEBHOOK_IDLE_TIMEOUT_ENV_KEY = "A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT"
ADK_MODEL_PROVIDER_ENV_KEY = "ADK_MODEL_PROVIDER"
ADK_MODEL_NAME_ENV_KEY = "ADK_MODEL_NAME"
ADK_MODEL_API_BASE_ENV_KEY = "ADK_MODEL_API_BASE"
ADK_MODEL_API_KEY_ENV_KEY = "ADK_MODEL_API_KEY"
DISCOVERY_ENV_KEY = "A2AT_SAMPLE_DISCOVERY"
TRANSPORT_ENV_KEY = "A2AT_SAMPLE_TRANSPORT"

#: Defaults matching ``env.example``.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_REGISTRY_HOST = "127.0.0.1"
DEFAULT_REGISTRY_PORT = 5001
DEFAULT_WEBHOOK_PORT = 9000
DEFAULT_WEBHOOK_TOKEN = "demo-webhook-token"
DEFAULT_WEBHOOK_IDLE_TIMEOUT = 30
DEFAULT_ADK_MODEL_PROVIDER = "litellm"
DEFAULT_ADK_MODEL_NAME = "openai/volcengine/deepseek-v4.1-flash"
DEFAULT_ADK_MODEL_API_BASE = "http://127.0.0.1:31943/v1"
DEFAULT_ADK_MODEL_API_KEY = "not-needed"


def _env_value(values: dict[str, str | None], key: str, default: str) -> str:
    """Resolve one key from the ``.env`` values, then the process environment, then a default."""
    for candidate in (values.get(key), os.environ.get(key)):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return default


def _env_int(values: dict[str, str | None], key: str, default: int) -> int:
    """Resolve one integer key, raising a readable error on invalid input."""
    raw_value = _env_value(values, key, str(default))
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer value for {key}: {raw_value}") from exc


@dataclass(frozen=True)
class SampleConfig:
    """Resolved sample configuration.

    Attributes:
        model_mode: ``live`` (LiteLlm) or ``offline`` (scripted LLM + fake ADK model).
        language: Process-level language (``en-US`` / ``zh-CN``).
        host: Sample bind host.
        port: Sample bind port.
        registry_url: Registry center base URL.
        webhook_port: Webhook receiver bind port (``0`` selects an ephemeral port).
        webhook_token: Push token checked by the webhook receiver.
        webhook_idle_timeout: Webhook idle backstop timeout in seconds.
        adk_model_provider: ADK model provider name (``litellm``).
        adk_model_name: LiteLlm model id.
        adk_model_api_base: OpenAI-compatible endpoint of the ADK model.
        adk_model_api_key: API key of the ADK model endpoint.
        discovery: Discovery mode (``registry`` / ``direct``).
        transport: Event transport (``sse`` / ``webhook``).
    """

    model_mode: ModelMode
    language: str
    host: str
    port: int
    registry_url: str
    webhook_port: int
    webhook_token: str
    webhook_idle_timeout: int
    adk_model_provider: str
    adk_model_name: str
    adk_model_api_base: str
    adk_model_api_key: str
    discovery: str = DISCOVERY_REGISTRY
    transport: str = TRANSPORT_SSE

    @property
    def is_offline(self) -> bool:
        """Return whether the sample runs with the scripted LLM and the fake ADK model."""
        return self.model_mode == MODEL_MODE_OFFLINE

    @property
    def model_label(self) -> str:
        """Return the model-mode label shown by the CLI and the Web UI."""
        return "offline (scripted)" if self.is_offline else f"live ({self.adk_model_name})"


def load_config(*, env_path: Path | None = None) -> SampleConfig:
    """Load the sample configuration from ``.env`` (defaults to ``$CWD/.env``).

    Raises:
        ValueError: when ``A2AT_SAMPLE_MODEL_MODE``, ``A2AT_LANGUAGE``, a port, or a discovery /
            transport key carries an unsupported value.
    """
    resolved = env_path or Path.cwd() / ".env"
    values: dict[str, str | None] = dict(dotenv_values(resolved)) if resolved.exists() else {}

    model_mode = _env_value(values, MODEL_MODE_ENV_KEY, MODEL_MODE_OFFLINE)
    if model_mode not in SUPPORTED_MODEL_MODES:
        raise ValueError(f"Unsupported {MODEL_MODE_ENV_KEY}: {model_mode}; expected one of {SUPPORTED_MODEL_MODES}")

    language = _env_value(values, LANGUAGE_ENV_KEY, LANGUAGE_ZH)
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Unsupported {LANGUAGE_ENV_KEY}: {language}; expected one of {SUPPORTED_LANGUAGES}")

    discovery = _env_value(values, DISCOVERY_ENV_KEY, DISCOVERY_REGISTRY)
    if discovery not in SUPPORTED_DISCOVERY_MODES:
        raise ValueError(f"Unsupported {DISCOVERY_ENV_KEY}: {discovery}; expected one of {SUPPORTED_DISCOVERY_MODES}")

    transport = _env_value(values, TRANSPORT_ENV_KEY, TRANSPORT_SSE)
    if transport not in SUPPORTED_TRANSPORTS:
        raise ValueError(f"Unsupported {TRANSPORT_ENV_KEY}: {transport}; expected one of {SUPPORTED_TRANSPORTS}")

    registry_host = _env_value(values, REGISTRY_HOST_ENV_KEY, DEFAULT_REGISTRY_HOST)
    registry_port = _env_int(values, REGISTRY_PORT_ENV_KEY, DEFAULT_REGISTRY_PORT)

    return SampleConfig(
        model_mode=cast(ModelMode, model_mode),
        language=language,
        host=_env_value(values, HOST_ENV_KEY, DEFAULT_HOST),
        port=_env_int(values, PORT_ENV_KEY, DEFAULT_PORT),
        registry_url=f"http://{registry_host}:{registry_port}",
        webhook_port=_env_int(values, WEBHOOK_PORT_ENV_KEY, DEFAULT_WEBHOOK_PORT),
        webhook_token=_env_value(values, WEBHOOK_TOKEN_ENV_KEY, DEFAULT_WEBHOOK_TOKEN),
        webhook_idle_timeout=_env_int(values, WEBHOOK_IDLE_TIMEOUT_ENV_KEY, DEFAULT_WEBHOOK_IDLE_TIMEOUT),
        adk_model_provider=_env_value(values, ADK_MODEL_PROVIDER_ENV_KEY, DEFAULT_ADK_MODEL_PROVIDER),
        adk_model_name=_env_value(values, ADK_MODEL_NAME_ENV_KEY, DEFAULT_ADK_MODEL_NAME),
        adk_model_api_base=_env_value(values, ADK_MODEL_API_BASE_ENV_KEY, DEFAULT_ADK_MODEL_API_BASE),
        adk_model_api_key=_env_value(values, ADK_MODEL_API_KEY_ENV_KEY, DEFAULT_ADK_MODEL_API_KEY),
        discovery=discovery,
        transport=transport,
    )


def build_adk_model(config: SampleConfig | None = None) -> BaseLlm:
    """Build the ADK model of the configured mode.

    Offline mode returns the deterministic fake model; live mode returns a ``LiteLlm`` pointing at
    the configured OpenAI-compatible endpoint.

    Raises:
        ValueError: when live mode has no model name.
    """
    resolved = config or load_config()
    if resolved.is_offline:
        from shared.adk_fake_model import build_fake_model

        return build_fake_model()

    if not resolved.adk_model_name:
        raise ValueError(f"{ADK_MODEL_NAME_ENV_KEY} must be non-empty in live mode")
    from google.adk.models.lite_llm import LiteLlm

    kwargs: dict[str, str] = {}
    if resolved.adk_model_api_base:
        kwargs["api_base"] = resolved.adk_model_api_base
    if resolved.adk_model_api_key:
        kwargs["api_key"] = resolved.adk_model_api_key
    return LiteLlm(model=resolved.adk_model_name, **kwargs)
