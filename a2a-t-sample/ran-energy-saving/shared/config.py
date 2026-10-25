"""Unified configuration of the RAN energy-saving ADK sample.

All values come from the sample's ``.env`` (falling back to process environment variables and then
to defaults). ``build_adk_model`` returns a ``LiteLlm`` pointing at the configured
OpenAI-compatible endpoint in ``live`` mode, and the deterministic fake model in ``offline`` mode.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from dotenv import dotenv_values

from shared.scenario import LANGUAGE_ZH, SUPPORTED_LANGUAGES, SUPPORTED_SCENARIO_NAMES

if TYPE_CHECKING:
    from google.adk.models.base_llm import BaseLlm

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
HTTP_TIMEOUT_ENV_KEY = "A2AT_SAMPLE_HTTP_TIMEOUT_SECONDS"
LLM_TIMEOUT_ENV_KEY = "A2AT_SAMPLE_LLM_TIMEOUT_SECONDS"
LLM_MAX_ATTEMPTS_ENV_KEY = "A2AT_SAMPLE_LLM_MAX_ATTEMPTS"
TASK_DB_ENV_KEY = "A2AT_SAMPLE_TASK_DB"
CLIENT_DB_ENV_KEY = "A2AT_SAMPLE_CLIENT_DB"
CLIENT_MEMORY_TURNS_ENV_KEY = "A2AT_SAMPLE_CLIENT_MEMORY_TURNS"
GATEWAY_HOST_ENV_KEY = "A2AT_SAMPLE_GATEWAY_HOST"
GATEWAY_PORT_ENV_KEY = "A2AT_SAMPLE_GATEWAY_PORT"
GATEWAY_REGISTER_ENV_KEY = "A2AT_SAMPLE_GATEWAY_REGISTER"
GATEWAY_SCENARIO_ENV_KEY = "A2AT_SAMPLE_GATEWAY_SCENARIO"
PLANNER_MODEL_ENV_KEY = "A2AT_SAMPLE_PLANNER_MODEL"
PLANNER_API_BASE_ENV_KEY = "A2AT_SAMPLE_PLANNER_API_BASE"
PLANNER_API_KEY_ENV_KEY = "A2AT_SAMPLE_PLANNER_API_KEY"
PLANNER_TIMEOUT_ENV_KEY = "A2AT_SAMPLE_PLANNER_TIMEOUT_SECONDS"
PLANNER_MAX_STEPS_ENV_KEY = "A2AT_SAMPLE_PLANNER_MAX_STEPS"

#: Defaults matching ``env.example``.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_REGISTRY_HOST = "127.0.0.1"
DEFAULT_REGISTRY_PORT = 5001
DEFAULT_WEBHOOK_PORT = 9000
DEFAULT_WEBHOOK_TOKEN = "demo-webhook-token"

#: Client HTTP budget of one A2A request (``ClientCallContext.timeout`` and the httpx client).
#: A live-mode server gate may perform several LLM calls before sending any event, so the budget
#: must exceed the server-side worst case (LLM timeout × calls; the sample disables transport
#: retries). 300s covers two 120s gate calls plus ADK agent turns.
DEFAULT_HTTP_TIMEOUT_SECONDS = 300.0

#: Backstop idle timeout of a webhook phase; it must not end a phase before the client budget, so
#: it follows the HTTP budget by default.
DEFAULT_WEBHOOK_IDLE_TIMEOUT = int(DEFAULT_HTTP_TIMEOUT_SECONDS)

#: Live LLM budget of one call (SDK structured calls and ADK LiteLlm turns). A local live smoke
#: observed a 58s validation and a >90s accept validation; 120s leaves headroom for slower
#: endpoints while the 300s client budget still covers the gate plus ADK agent turns.
DEFAULT_LLM_TIMEOUT_SECONDS = 120.0

#: Live SDK LLM attempt limit per retryable step (the SDK accepts 1..10).
DEFAULT_LLM_MAX_ATTEMPTS = 1

#: Inclusive bounds of ``A2AT_SAMPLE_LLM_MAX_ATTEMPTS`` (mirrors the SDK's clamping bounds).
LLM_MAX_ATTEMPTS_LOWER_BOUND = 1
LLM_MAX_ATTEMPTS_UPPER_BOUND = 10

#: Recent conversation turns injected into the next client run.
DEFAULT_CLIENT_MEMORY_TURNS = 6

#: A2A gateway defaults (the client agent's inbound A2A surface).
DEFAULT_GATEWAY_PORT = 8001
DEFAULT_GATEWAY_SCENARIO = "information"

#: LangGraph planner defaults (the server's post-accept execution agent).
DEFAULT_PLANNER_MAX_STEPS = 12
DEFAULT_ADK_MODEL_PROVIDER = "litellm"
DEFAULT_ADK_MODEL_NAME = "openai/mass/volcengine/deepseek-v4.1-flash"
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


def _env_float(values: dict[str, str | None], key: str, default: float) -> float:
    """Resolve one float key, raising a readable error on invalid input."""
    raw_value = _env_value(values, key, str(default))
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value for {key}: {raw_value}") from exc


def _positive_seconds(values: dict[str, str | None], key: str, default: float) -> float:
    """Resolve one strictly positive, finite duration in seconds."""
    value = _env_float(values, key, default)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid duration for {key}: {value:g}; expected a positive number of seconds")
    return value


def _non_negative_int(values: dict[str, str | None], key: str, default: int) -> int:
    """Resolve one non-negative integer key, raising a readable error on invalid input."""
    value = _env_int(values, key, default)
    if value < 0:
        raise ValueError(f"Invalid value for {key}: {value}; expected a non-negative integer")
    return value


def _positive_int(values: dict[str, str | None], key: str, default: int) -> int:
    """Resolve one strictly positive integer key."""
    value = _env_int(values, key, default)
    if value < 1:
        raise ValueError(f"Invalid value for {key}: {value}; expected a positive integer")
    return value


def _llm_max_attempts(values: dict[str, str | None]) -> int:
    """Resolve the live LLM attempt limit, validated against the inclusive SDK bounds."""
    value = _env_int(values, LLM_MAX_ATTEMPTS_ENV_KEY, DEFAULT_LLM_MAX_ATTEMPTS)
    if not LLM_MAX_ATTEMPTS_LOWER_BOUND <= value <= LLM_MAX_ATTEMPTS_UPPER_BOUND:
        raise ValueError(
            f"Invalid value for {LLM_MAX_ATTEMPTS_ENV_KEY}: {value}; expected "
            f"{LLM_MAX_ATTEMPTS_LOWER_BOUND}..{LLM_MAX_ATTEMPTS_UPPER_BOUND}"
        )
    return value


def _env_bool(values: dict[str, str | None], key: str, default: bool) -> bool:
    """Resolve one boolean key (``1/true/yes/on`` and ``0/false/no/off``)."""
    raw_value = _env_value(values, key, "true" if default else "false").lower()
    if raw_value in ("1", "true", "yes", "on"):
        return True
    if raw_value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Invalid boolean value for {key}: {raw_value}")


def _optional_absolute_path(values: dict[str, str | None], key: str) -> str | None:
    """Resolve an optional absolute path key, rejecting relative values."""
    raw_value = _env_value(values, key, "")
    if not raw_value:
        return None
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Invalid value for {key}: {raw_value}; expected an absolute path")
    return str(path)


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
        http_timeout_seconds: Client HTTP budget of one A2A request, in seconds.
        llm_timeout_seconds: Live LLM budget of one call (SDK structured call or ADK turn), in seconds.
        llm_max_attempts: Live SDK LLM attempt limit per retryable step.
        task_db_path: Absolute SQLite path of the persisted A2A Task store; ``None`` keeps the
            in-memory store (tests and ephemeral runs).
        client_db_path: Absolute SQLite path of the client conversation/run store; ``None`` keeps
            the client state in memory only.
        client_memory_turns: Recent conversation turns injected into the next run's prompts.
        gateway_host: bind host of the optional inbound A2A gateway.
        gateway_port: bind port of the optional inbound A2A gateway.
        gateway_register: whether the gateway registers its AgentCard in the registry center.
        gateway_scenario: scenario used for an inbound gateway request.
        planner_model: LangChain model id of the server's LangGraph planner.
        planner_api_base: OpenAI-compatible endpoint of the planner.
        planner_api_key: API key of the planner endpoint.
        planner_timeout_seconds: per-call planner budget.
        planner_max_steps: LangGraph recursion limit of one execution.
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
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_max_attempts: int = DEFAULT_LLM_MAX_ATTEMPTS
    task_db_path: str | None = None
    client_db_path: str | None = None
    client_memory_turns: int = DEFAULT_CLIENT_MEMORY_TURNS
    gateway_host: str = DEFAULT_HOST
    gateway_port: int = DEFAULT_GATEWAY_PORT
    gateway_register: bool = False
    gateway_scenario: str = DEFAULT_GATEWAY_SCENARIO
    planner_model: str = ""
    planner_api_base: str = ""
    planner_api_key: str = ""
    planner_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    planner_max_steps: int = DEFAULT_PLANNER_MAX_STEPS

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
        ValueError: when ``A2AT_SAMPLE_MODEL_MODE``, ``A2AT_LANGUAGE``, a port, a timeout, the LLM
            attempt limit, a discovery / transport key, or ``A2AT_SAMPLE_TASK_DB`` carries an
            unsupported value.
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

    gateway_scenario = _env_value(
        values, GATEWAY_SCENARIO_ENV_KEY, DEFAULT_GATEWAY_SCENARIO
    )
    if gateway_scenario not in SUPPORTED_SCENARIO_NAMES:
        raise ValueError(
            f"Unsupported {GATEWAY_SCENARIO_ENV_KEY}: {gateway_scenario}; "
            f"expected one of {SUPPORTED_SCENARIO_NAMES}"
        )

    adk_model_name = _env_value(values, ADK_MODEL_NAME_ENV_KEY, DEFAULT_ADK_MODEL_NAME)
    adk_model_api_base = _env_value(
        values, ADK_MODEL_API_BASE_ENV_KEY, DEFAULT_ADK_MODEL_API_BASE
    )
    adk_model_api_key = _env_value(
        values, ADK_MODEL_API_KEY_ENV_KEY, DEFAULT_ADK_MODEL_API_KEY
    )
    # The ADK model id is a LiteLLM id ("openai/..."); ChatOpenAI takes the bare model id. The
    # planner keys are independent so a deployment can point the server at a different model.
    planner_model_default = adk_model_name.removeprefix("openai/")

    return SampleConfig(
        model_mode=model_mode,
        language=language,
        host=_env_value(values, HOST_ENV_KEY, DEFAULT_HOST),
        port=_env_int(values, PORT_ENV_KEY, DEFAULT_PORT),
        registry_url=f"http://{registry_host}:{registry_port}",
        webhook_port=_env_int(values, WEBHOOK_PORT_ENV_KEY, DEFAULT_WEBHOOK_PORT),
        webhook_token=_env_value(values, WEBHOOK_TOKEN_ENV_KEY, DEFAULT_WEBHOOK_TOKEN),
        webhook_idle_timeout=_env_int(values, WEBHOOK_IDLE_TIMEOUT_ENV_KEY, DEFAULT_WEBHOOK_IDLE_TIMEOUT),
        adk_model_provider=_env_value(values, ADK_MODEL_PROVIDER_ENV_KEY, DEFAULT_ADK_MODEL_PROVIDER),
        adk_model_name=adk_model_name,
        adk_model_api_base=adk_model_api_base,
        adk_model_api_key=adk_model_api_key,
        discovery=discovery,
        transport=transport,
        http_timeout_seconds=_positive_seconds(
            values, HTTP_TIMEOUT_ENV_KEY, DEFAULT_HTTP_TIMEOUT_SECONDS
        ),
        llm_timeout_seconds=_positive_seconds(values, LLM_TIMEOUT_ENV_KEY, DEFAULT_LLM_TIMEOUT_SECONDS),
        llm_max_attempts=_llm_max_attempts(values),
        task_db_path=_optional_absolute_path(values, TASK_DB_ENV_KEY),
        client_db_path=_optional_absolute_path(values, CLIENT_DB_ENV_KEY),
        client_memory_turns=_non_negative_int(
            values, CLIENT_MEMORY_TURNS_ENV_KEY, DEFAULT_CLIENT_MEMORY_TURNS
        ),
        gateway_host=_env_value(values, GATEWAY_HOST_ENV_KEY, DEFAULT_HOST),
        gateway_port=_env_int(values, GATEWAY_PORT_ENV_KEY, DEFAULT_GATEWAY_PORT),
        gateway_register=_env_bool(values, GATEWAY_REGISTER_ENV_KEY, False),
        gateway_scenario=gateway_scenario,
        planner_model=_env_value(values, PLANNER_MODEL_ENV_KEY, planner_model_default),
        planner_api_base=_env_value(values, PLANNER_API_BASE_ENV_KEY, adk_model_api_base),
        planner_api_key=_env_value(values, PLANNER_API_KEY_ENV_KEY, adk_model_api_key),
        planner_timeout_seconds=_positive_seconds(
            values, PLANNER_TIMEOUT_ENV_KEY, DEFAULT_LLM_TIMEOUT_SECONDS
        ),
        planner_max_steps=_positive_int(
            values, PLANNER_MAX_STEPS_ENV_KEY, DEFAULT_PLANNER_MAX_STEPS
        ),
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

    kwargs: dict[str, Any] = {
        # LiteLlm forwards these to litellm. Keep every ADK turn inside the same per-call budget
        # as the SDK structured calls, and disable LiteLLM's own retries so the client budget
        # stays predictable (one call cannot silently become three attempts).
        "timeout": resolved.llm_timeout_seconds,
        "num_retries": 0,
    }
    if resolved.adk_model_api_base:
        kwargs["api_base"] = resolved.adk_model_api_base
    if resolved.adk_model_api_key:
        kwargs["api_key"] = resolved.adk_model_api_key
    return LiteLlm(model=resolved.adk_model_name, **kwargs)


def build_traced_adk_model(
    config: SampleConfig | None = None,
    *,
    model: BaseLlm | None = None,
) -> BaseLlm:
    """Build the ADK model of the configured mode wrapped in the Run Explorer tracer.

    Production entry points use this builder so every ADK turn produces an ``llm`` span. The
    wrapped model still reports the same ``model`` id and ``capabilities``; an explicit ``model``
    override (tests) is wrapped as-is, and an already-traced model is returned unchanged.

    Args:
        config: resolved sample configuration; defaults to :func:`load_config`.
        model: optional model override (the deterministic fake model in tests).
    """
    from shared.adk_tracing import TracingLlm

    resolved = model if model is not None else build_adk_model(config)
    if isinstance(resolved, TracingLlm):
        return resolved
    return TracingLlm.wrap(resolved)
