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
import logging
import shutil
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
from shared.agent_card import AGENT_CARD_NAME, AGENT_CARD_ORGANIZATION
from shared.config import (
    SUPPORTED_TRANSPORTS,
    TRANSPORT_SSE,
    TRANSPORT_WEBHOOK,
    SampleConfig,
)
from shared.model_factory import build_traced_adk_model
from shared.models import NegotiationProposeView
from shared.sdk_env import SdkEnvFile, prepare_sdk_env
from shared.trace import set_global_sink, trace_scope
from shared.traced_llm import register_traced_providers
from shared.webhook_receiver import WebhookReceiver

from protocol.client_flow import (
    ClientFlowResult,
    ClientTransport,
    DecisionMadeCallback,
    SseClientTransport,
    WebhookClientTransport,
    build_webhook_receiver,
    run_client_flow,
)
from protocol.decision_provider import (
    AdkDecisionProvider,
    ConfirmingDecisionProvider,
    ManualDecisionProvider,
    NegotiationDecisionProvider,
    ScriptedDecisionProvider,
)
from protocol.discovery import DiscoveredAgent, discover_agent
from protocol.run_spec import RunCatalog, RunSpec

_LOGGER = logging.getLogger("ran_energy_saving.client_runtime")

#: Client decision modes.
MODE_AUTO: Final[str] = "auto"
MODE_MANUAL: Final[str] = "manual"
MODE_SCRIPTED: Final[str] = "scripted"
MODE_CONFIRM: Final[str] = "confirm"
SUPPORTED_MODES: Final[tuple[str, ...]] = (MODE_AUTO, MODE_MANUAL, MODE_SCRIPTED, MODE_CONFIRM)

#: Discovery key of the direct server base URL.
DIRECT_SERVER_URL_ENV_KEY: Final[str] = "A2AT_SAMPLE_DIRECT_SERVER_URL"

#: Prefix of the temporary SDK ``.env`` directories.
_SDK_ENV_PREFIX: Final[str] = "a2at-ran-client-env-"


class ClientRuntimeError(RuntimeError):
    """Raised when the client runtime cannot be assembled."""


def _env_values(env_path: Path | None) -> dict[str, str]:
    """Read the source ``.env`` values as plain strings (empty when the file is missing)."""
    if env_path is None or not env_path.exists():
        return {}
    return {
        str(key): str(value)
        for key, value in dotenv_values(env_path).items()
        if key is not None and value is not None
    }


def prepare_client_sdk_env(
    config: SampleConfig,
    env_path: Path | None = None,
) -> SdkEnvFile:
    """Build a per-run SDK ``.env`` without mutating the caller's file.

    The builder lives in :mod:`shared.sdk_env` so the server runtime can use it without importing
    this module; this wrapper keeps the historical client entry point and its directory prefix.

    Args:
        config: resolved sample configuration.
        env_path: source ``.env``; defaults to ``$CWD/.env``.

    Returns:
        The generated file and the temporary directory owning it.
    """
    return prepare_sdk_env(config, env_path, prefix=_SDK_ENV_PREFIX)


def _direct_base_url(config: SampleConfig, env_path: Path | None) -> str:
    """Return the direct-discovery server URL (configured value or the sample bind address)."""
    configured = _env_values(env_path).get(DIRECT_SERVER_URL_ENV_KEY, "").strip()
    return configured or f"http://{config.host}:{config.port}"


def _server_auth_headers(config: SampleConfig) -> dict[str, str]:
    """Return the bearer header of the client-to-server A2A calls, empty when open."""
    token = config.server_auth_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _decision_provider(
    config: SampleConfig,
    run_spec: RunSpec,
    mode: str,
    *,
    model: Any | None = None,
    client_agent_factory: Callable[[Any], Any] | None = None,
) -> NegotiationDecisionProvider:
    """Build the decision provider of one client mode.

    The offline model is supplied by the composition root; a scripted provider replays the run
    spec's decisions. In offline ``auto`` mode the model serves the same scripted decisions, so the
    ADK runner stays in the loop and still produces counter/reject/abort decisions.

    Raises:
        ClientRuntimeError: when the mode is not supported.
    """
    if mode == MODE_MANUAL:
        return ManualDecisionProvider()
    if mode == MODE_SCRIPTED:
        return ScriptedDecisionProvider(run_spec.scripted_decisions)
    if mode in (MODE_AUTO, MODE_CONFIRM):
        # The traced wrapper keeps the ADK decision turns visible in the Run Explorer. The original
        # request is carried into every decision prompt: target/feasibility proposals describe the
        # conflict but not the concrete throughput target, so the model cannot resolve them alone.
        provider = AdkDecisionProvider(
            model=build_traced_adk_model(config, model=model),
            offline=config.is_offline,
            user_request=run_spec.user_request,
            session_db_path=config.client_db_path,
            agent_factory=client_agent_factory,
        )
        if mode == MODE_CONFIRM:
            return ConfirmingDecisionProvider(provider)
        return provider
    raise ClientRuntimeError(f"Unsupported client mode: {mode}; expected one of {SUPPORTED_MODES}")


@dataclass
class ClientRuntime:
    """Assembled client runtime of one run.

    Attributes:
        config: resolved sample configuration.
        run_spec: the named run being executed (catalog entry).
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
    run_spec: RunSpec
    mode: str
    transport_name: str
    prompt_client: A2ATClient
    a2a_client: Any
    transport: ClientTransport
    provider: NegotiationDecisionProvider
    discovered: DiscoveredAgent
    sdk_env: SdkEnvFile
    manual_provider: Any | None
    _httpx_client: httpx.AsyncClient
    _webhook_receiver: WebhookReceiver | None = None
    _closed: bool = False
    trace_sink: Callable[[dict[str, object]], None] | None = None

    def set_recommendation_sink(
        self,
        sink: Callable[[Any, Any, Any], None] | None,
    ) -> None:
        """Install the confirmation recommendation sink when the provider supports one.

        Args:
            sink: callback receiving ``(decision, propose, session)`` before the human wait.
        """
        setter = getattr(self.provider, "set_recommendation_sink", None)
        if callable(setter):
            setter(sink)

    async def run(
        self,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        on_decision_requested: Callable[[NegotiationProposeView, NegotiationContext], None] | None = None,
        on_decision_made: DecisionMadeCallback | None = None,
        trace_sink: Callable[[dict[str, object]], None] | None = None,
        run_id: str | None = None,
        user_request: str | None = None,
        conversation_context: str | None = None,
    ) -> ClientFlowResult:
        """Run the full client flow of the configured scenario.

        The optional trace sink is attached to the process-wide producer hook for the duration of
        the run, and the run id is put into the trace context so the client spans, the LLM/tool
        spans and the ``sampleRunId`` message metadata all agree.

        Args:
            event_sink: Optional live observer of the normalized events (Web UI timeline).
            on_decision_requested: Optional observer invoked before asking the provider.
            on_decision_made: Optional observer invoked immediately after the provider returns,
                before the response is sent.
            trace_sink: Optional observer of synthetic Run Explorer spans (LLM/tool producers
                included); defaults to the sink the runtime was built with.
            run_id: Optional Run Explorer run id (the Web UI and the CLI pass theirs).
            user_request: Optional natural-language request entered in the Web UI; ``None`` uses
                the scenario's canned request.
            conversation_context: Optional recent-turn block injected into the decision prompt
                and Task-T generation so a follow-up can refer to an earlier run.

        Returns:
            The collected events, decisions and terminal state.
        """
        if self._closed:
            raise ClientRuntimeError("The client runtime is already closed.")
        sink = trace_sink if trace_sink is not None else self.trace_sink
        # A run-level sink is restored to the construction-time sink after the run; the
        # construction-time sink stays attached until ``aclose``.
        temporary_sink = trace_sink is not None and trace_sink is not self.trace_sink
        if sink is not None:
            set_global_sink(sink)
        try:
            with trace_scope(run_id=run_id, role="client"):
                request_text = (
                    user_request.strip()
                    if isinstance(user_request, str) and user_request.strip()
                    else self.run_spec.user_request
                )
                set_context = getattr(self.provider, "set_conversation_context", None)
                if callable(set_context):
                    set_context(conversation_context)
                return await run_client_flow(
                    prompt_client=self.prompt_client,
                    transport=self.transport,
                    user_request=request_text,
                    provider=self.provider,
                    language=self.config.language,
                    policy=self.run_spec.policy,
                    event_sink=event_sink,
                    on_decision_requested=on_decision_requested,
                    on_decision_made=on_decision_made,
                    trace_sink=sink,
                    call_timeout_seconds=self.config.http_timeout_seconds,
                    conversation_context=conversation_context,
                )
        finally:
            if temporary_sink:
                set_global_sink(self.trace_sink)

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
        if self.trace_sink is not None:
            set_global_sink(None)
        shutil.rmtree(self.sdk_env.directory, ignore_errors=True)


async def build_client_runtime(
    config: SampleConfig,
    scenario: str,
    mode: str = MODE_AUTO,
    transport: str = TRANSPORT_SSE,
    *,
    catalog: RunCatalog,
    env_path: Path | None = None,
    trace_sink: Callable[[dict[str, object]], None] | None = None,
    offline_model_factory: Callable[[RunSpec], Any] | None = None,
    install_offline_provider: Callable[[], None] | None = None,
    client_agent_factory: Callable[[Any], Any] | None = None,
) -> ClientRuntime:
    """Assemble the client runtime of one run.

    Args:
        config: resolved sample configuration (language and model mode are process-level).
        scenario: run name resolved through ``catalog``.
        mode: decision mode (``auto`` / ``manual`` / ``scripted``).
        transport: event transport (``sse`` / ``webhook``).
        catalog: run catalog supplied by the composition root.
        env_path: source ``.env`` used for discovery and for the per-run SDK ``.env``.
        trace_sink: optional Run Explorer sink attached **before** the SDK and ADK clients are
            built, so their LLM/tool producers are traced from the first call.
        offline_model_factory: builder of the deterministic ADK model; required in offline mode.
        install_offline_provider: optional installer of the deterministic SDK LLM routing.
        client_agent_factory: optional builder of the ADK decision agent from its model.

    Returns:
        The assembled runtime; the caller owns it and must call :meth:`ClientRuntime.aclose`.

    Raises:
        ClientRuntimeError: on an unsupported mode/transport, a missing offline factory, or when
            the runtime cannot assemble.
        KeyError: when the run name is unknown to the catalog.
        DiscoveryError: when no usable server endpoint is found.
    """
    if mode not in SUPPORTED_MODES:
        raise ClientRuntimeError(f"Unsupported client mode: {mode}; expected one of {SUPPORTED_MODES}")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ClientRuntimeError(f"Unsupported transport: {transport}; expected one of {SUPPORTED_TRANSPORTS}")

    run_spec = catalog.spec(scenario, language=config.language)
    register_traced_providers()
    if config.is_offline:
        if offline_model_factory is None:
            raise ClientRuntimeError(
                "Offline mode needs a deterministic model factory; configure live mode or pass "
                "offline_model_factory from the composition root."
            )
        if install_offline_provider is not None:
            install_offline_provider()
    if trace_sink is not None:
        set_global_sink(trace_sink)
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
    _LOGGER.info("agent discovered: url=%s discovery=%s", discovered.url, config.discovery)

    # One budget for both the transport and the per-call context (the SDK applies the context
    # timeout to every request, overriding the httpx client default).
    httpx_client = httpx.AsyncClient(
        timeout=config.http_timeout_seconds,
        trust_env=False,
        headers=_server_auth_headers(config),
    )
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
            )
            await receiver.start()
            _LOGGER.info("webhook mode: push_url=%s", receiver.url)
            flow_transport: ClientTransport = WebhookClientTransport(
                a2a_client,
                queue=queue,
                push_url=receiver.url,
                push_token=config.webhook_token,
                idle_timeout=float(config.webhook_idle_timeout),
            )
        else:
            receiver = None
            flow_transport = SseClientTransport(a2a_client)
    except Exception:
        await httpx_client.aclose()
        shutil.rmtree(sdk_env.directory, ignore_errors=True)
        raise

    offline_model = offline_model_factory(run_spec) if offline_model_factory is not None else None
    provider = _decision_provider(
        config,
        run_spec,
        mode,
        model=offline_model,
        client_agent_factory=client_agent_factory,
    )
    return ClientRuntime(
        config=config,
        run_spec=run_spec,
        mode=mode,
        transport_name=transport,
        prompt_client=prompt_client,
        a2a_client=a2a_client,
        transport=flow_transport,
        provider=provider,
        discovered=discovered,
        sdk_env=sdk_env,
        manual_provider=(
            provider
            if isinstance(provider, (ManualDecisionProvider, ConfirmingDecisionProvider))
            else None
        ),
        _httpx_client=httpx_client,
        _webhook_receiver=receiver,
        trace_sink=trace_sink,
    )
