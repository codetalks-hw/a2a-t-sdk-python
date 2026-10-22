"""Client runtime assembly of the RAN energy-saving sample.

:func:`build_client_runtime` wires one run end to end:

* it builds a **per-run** A2A-T SDK ``.env`` from the sample ``.env`` (the SDK reads its
  configuration from a file, never from process environment variables) and never mutates the source
  file;
* it registers the scripted SDK provider before constructing ``A2ATClient`` in offline mode;
* it discovers the server with :func:`protocol.discovery.discover_agent` (registry by default, or
  the direct AgentCard);
* it creates the a2a-sdk client with streaming enabled for SSE and disabled for webhook, owns the
  embedded webhook receiver and its queue, and selects the configured decision provider;
* :meth:`ClientRuntime.run` executes :func:`protocol.client_flow.run_client_flow` with the optional
  live observers used by the Web UI and returns its :class:`~protocol.client_flow.ClientFlowResult`;
* :meth:`ClientRuntime.aclose` stops the receiver and closes every HTTP client and temporary file.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from a2a.client import ClientConfig, create_client
from a2a.utils.constants import TransportProtocol
from a2a_t.client.a2at_client import A2ATClient
from a2a_t.core.metadata import NegotiationContext
from dotenv import dotenv_values
from shared.config import (
    MODEL_MODE_OFFLINE,
    SUPPORTED_TRANSPORTS,
    TRANSPORT_SSE,
    TRANSPORT_WEBHOOK,
    SampleConfig,
    build_adk_model,
)
from shared.models import NegotiationProposeView
from shared.scenario import Scenario, get_scenario, resolve_user_request
from shared.scripted_llm import register_scripted_provider
from shared.webhook_receiver import WebhookReceiver

from protocol.client_flow import (
    ClientFlowResult,
    ClientTransport,
    SseClientTransport,
    WebhookClientTransport,
    build_webhook_receiver,
    run_client_flow,
)
from protocol.decision_provider import (
    AdkDecisionProvider,
    ManualDecisionProvider,
    NegotiationDecisionProvider,
    ScriptedDecisionProvider,
)
from protocol.discovery import DiscoveredAgent, discover_agent
from protocol.server_app import AGENT_CARD_NAME, AGENT_CARD_ORGANIZATION

#: Client decision modes.
MODE_AUTO: Final[str] = "auto"
MODE_MANUAL: Final[str] = "manual"
MODE_SCRIPTED: Final[str] = "scripted"
SUPPORTED_MODES: Final[tuple[str, ...]] = (MODE_AUTO, MODE_MANUAL, MODE_SCRIPTED)

#: Discovery key of the direct server base URL.
DIRECT_SERVER_URL_ENV_KEY: Final[str] = "A2AT_SAMPLE_DIRECT_SERVER_URL"

#: Default HTTP timeout of the client requests.
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0

#: Prefix of the temporary SDK ``.env`` directories.
_SDK_ENV_PREFIX: Final[str] = "a2at-ran-client-env-"


class ClientRuntimeError(RuntimeError):
    """Raised when the client runtime cannot be assembled."""


@dataclass(frozen=True)
class SdkEnvFile:
    """Per-run SDK ``.env`` built for one A2ATClient.

    Attributes:
        path: Path of the generated ``.env`` file.
        directory: Temporary directory owning the file (removed by the runtime).
    """

    path: Path
    directory: Path


def _env_values(env_path: Path | None) -> dict[str, str]:
    """Read the source ``.env`` values as plain strings (empty when the file is missing)."""
    if env_path is None or not env_path.exists():
        return {}
    return {
        str(key): str(value)
        for key, value in dotenv_values(env_path).items()
        if key is not None and value is not None
    }


def _sdk_model_name(adk_model_name: str) -> str:
    """Return the SDK model id matching one LiteLlm model id (drops the litellm provider prefix)."""
    return adk_model_name.removeprefix("openai/") if adk_model_name else ""


def prepare_client_sdk_env(
    config: SampleConfig,
    env_path: Path | None = None,
) -> SdkEnvFile:
    """Build a per-run SDK ``.env`` without mutating the caller's file.

    ``A2AT_LANGUAGE`` always follows the process-level configuration. Offline mode pins the
    scripted provider; live mode keeps the SDK endpoint values of the source file and fills any
    missing one from the ADK model configuration, so both SDK and ADK address the same endpoint.

    Args:
        config: resolved sample configuration.
        env_path: source ``.env``; defaults to ``$CWD/.env``.

    Returns:
        The generated file and the temporary directory owning it.
    """
    source_path = env_path or Path.cwd() / ".env"
    values = _env_values(source_path)
    values["A2AT_LANGUAGE"] = config.language
    if config.model_mode == MODEL_MODE_OFFLINE:
        values.update(
            {
                "A2AT_LLM_PROVIDER": "scripted",
                "A2AT_LLM_MODEL": "scripted",
                "A2AT_LLM_API_KEY": "not-needed",
                "A2AT_LLM_BASE_URL": values.get("A2AT_LLM_BASE_URL") or config.adk_model_api_base,
            }
        )
    else:
        values.update(
            {
                "A2AT_LLM_PROVIDER": values.get("A2AT_LLM_PROVIDER") or "openai",
                "A2AT_LLM_MODEL": values.get("A2AT_LLM_MODEL") or _sdk_model_name(config.adk_model_name),
                "A2AT_LLM_API_KEY": values.get("A2AT_LLM_API_KEY") or config.adk_model_api_key,
                "A2AT_LLM_BASE_URL": values.get("A2AT_LLM_BASE_URL") or config.adk_model_api_base,
            }
        )
    temp_dir = Path(tempfile.mkdtemp(prefix=_SDK_ENV_PREFIX))
    target = temp_dir / ".env"
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return SdkEnvFile(path=target, directory=temp_dir)


def _direct_base_url(config: SampleConfig, env_path: Path | None) -> str:
    """Return the direct-discovery server URL (configured value or the sample bind address)."""
    configured = _env_values(env_path).get(DIRECT_SERVER_URL_ENV_KEY, "").strip()
    return configured or f"http://{config.host}:{config.port}"


def _decision_provider(config: SampleConfig, scenario: Scenario, mode: str) -> NegotiationDecisionProvider:
    """Build the decision provider of one client mode.

    Raises:
        ClientRuntimeError: when the mode is not supported.
    """
    if mode == MODE_MANUAL:
        return ManualDecisionProvider()
    if mode == MODE_SCRIPTED:
        return ScriptedDecisionProvider.from_scenario(scenario)
    if mode == MODE_AUTO:
        return AdkDecisionProvider(model=build_adk_model(config), offline=config.is_offline)
    raise ClientRuntimeError(f"Unsupported client mode: {mode}; expected one of {SUPPORTED_MODES}")


@dataclass
class ClientRuntime:
    """Assembled client runtime of one run.

    Attributes:
        config: resolved sample configuration.
        scenario: scenario being demonstrated.
        mode: decision mode (``auto`` / ``manual`` / ``scripted``).
        transport_name: event transport (``sse`` / ``webhook``).
        prompt_client: A2A-T SDK prompt facade.
        a2a_client: a2a-sdk client used by the transports.
        transport: normalized-event transport.
        provider: negotiation decision provider.
        discovered: discovered server endpoint.
        sdk_env: generated SDK ``.env`` owned by this runtime.
        manual_provider: manual provider when ``mode == "manual"``, else ``None``.
    """

    config: SampleConfig
    scenario: Scenario
    mode: str
    transport_name: str
    prompt_client: A2ATClient
    a2a_client: Any
    transport: ClientTransport
    provider: NegotiationDecisionProvider
    discovered: DiscoveredAgent
    sdk_env: SdkEnvFile
    manual_provider: ManualDecisionProvider | None
    _httpx_client: httpx.AsyncClient
    _webhook_receiver: WebhookReceiver | None = None
    _closed: bool = False

    async def run(
        self,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        on_decision_requested: Callable[[NegotiationProposeView, NegotiationContext], None] | None = None,
    ) -> ClientFlowResult:
        """Run the full client flow of the configured scenario.

        Args:
            event_sink: Optional live observer of the normalized events (Web UI timeline).
            on_decision_requested: Optional observer invoked before asking the provider.

        Returns:
            The collected events, decisions and terminal state.
        """
        if self._closed:
            raise ClientRuntimeError("The client runtime is already closed.")
        return await run_client_flow(
            prompt_client=self.prompt_client,
            transport=self.transport,
            user_request=resolve_user_request(self.scenario, self.config.language),
            provider=self.provider,
            language=self.config.language,
            event_sink=event_sink,
            on_decision_requested=on_decision_requested,
        )

    async def aclose(self) -> None:
        """Stop the webhook receiver, close the HTTP clients and remove the temporary SDK env."""
        if self._closed:
            return
        self._closed = True
        if self._webhook_receiver is not None:
            await self._webhook_receiver.stop()
        close_client = getattr(self.a2a_client, "close", None)
        if callable(close_client):
            await close_client()
        await self._httpx_client.aclose()
        shutil.rmtree(self.sdk_env.directory, ignore_errors=True)


async def build_client_runtime(
    config: SampleConfig,
    scenario: str,
    mode: str = MODE_AUTO,
    transport: str = TRANSPORT_SSE,
    *,
    env_path: Path | None = None,
    log_sink: Callable[[str], None] | None = None,
) -> ClientRuntime:
    """Assemble the client runtime of one run.

    Args:
        config: resolved sample configuration (language and model mode are process-level).
        scenario: one of the five scenario names.
        mode: decision mode (``auto`` / ``manual`` / ``scripted``).
        transport: event transport (``sse`` / ``webhook``).
        env_path: source ``.env`` used for discovery and for the per-run SDK ``.env``.
        log_sink: optional log sink of the transports and the webhook receiver.

    Returns:
        The assembled runtime; the caller owns it and must call :meth:`ClientRuntime.aclose`.

    Raises:
        ClientRuntimeError: on an unsupported mode/transport or when the runtime cannot assemble.
        KeyError: when the scenario name is unknown.
        DiscoveryError: when no usable server endpoint is found.
    """
    if mode not in SUPPORTED_MODES:
        raise ClientRuntimeError(f"Unsupported client mode: {mode}; expected one of {SUPPORTED_MODES}")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ClientRuntimeError(f"Unsupported transport: {transport}; expected one of {SUPPORTED_TRANSPORTS}")

    scenario_spec = get_scenario(scenario)
    if config.is_offline:
        register_scripted_provider()
    sdk_env = prepare_client_sdk_env(config, env_path)
    prompt_client = A2ATClient(env_path=sdk_env.path, logger=None)

    discovered = await discover_agent(
        discovery=config.discovery,
        organization=AGENT_CARD_ORGANIZATION,
        name=AGENT_CARD_NAME,
        registry_center_url=config.registry_url,
        direct_base_url=_direct_base_url(config, env_path),
        env_path=env_path,
    )
    if log_sink is not None:
        log_sink(f"[client] agent-discovered: url={discovered.url} discovery={config.discovery}")

    httpx_client = httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS, trust_env=False)
    client_config = ClientConfig(
        streaming=transport == TRANSPORT_SSE,
        httpx_client=httpx_client,
        supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
        use_client_preference=True,
    )
    try:
        a2a_client = await create_client(discovered.agent_card, client_config=client_config)
        if transport == TRANSPORT_WEBHOOK:
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            receiver = build_webhook_receiver(
                token=config.webhook_token,
                queue=queue,
                port=config.webhook_port,
                log_sink=log_sink,
            )
            await receiver.start()
            if log_sink is not None:
                log_sink(f"[client] webhook-mode: push_url={receiver.url}")
            flow_transport: ClientTransport = WebhookClientTransport(
                a2a_client,
                queue=queue,
                push_url=receiver.url,
                push_token=config.webhook_token,
                idle_timeout=float(config.webhook_idle_timeout),
                log_sink=log_sink,
            )
        else:
            receiver = None
            flow_transport = SseClientTransport(a2a_client, log_sink=log_sink)
    except Exception:
        await httpx_client.aclose()
        shutil.rmtree(sdk_env.directory, ignore_errors=True)
        raise

    provider = _decision_provider(config, scenario_spec, mode)
    return ClientRuntime(
        config=config,
        scenario=scenario_spec,
        mode=mode,
        transport_name=transport,
        prompt_client=prompt_client,
        a2a_client=a2a_client,
        transport=flow_transport,
        provider=provider,
        discovered=discovered,
        sdk_env=sdk_env,
        manual_provider=provider if isinstance(provider, ManualDecisionProvider) else None,
        _httpx_client=httpx_client,
        _webhook_receiver=receiver,
    )
