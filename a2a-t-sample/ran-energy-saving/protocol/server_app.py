"""Application assembly of the RAN energy-saving A2A-T server.

The module builds the AgentCard (declaring the Task-T and Negotiation-T extensions), the A2A
request handler, the Starlette app and the registry registration. The server always runs the
negotiation gate in front of the deterministic OSS executor, so all negotiation happens before a
Task is created and the server uses no ADK agent.

Offline mode registers the traced scripted SDK provider and points the SDK at a temporary ``.env``
carrying ``A2AT_LLM_PROVIDER=scripted_traced`` / ``A2AT_LLM_MODEL=scripted`` /
``A2AT_LLM_API_KEY=not-needed``; the SDK reads its configuration from a file, not from process
environment variables, so the temporary copy is the non-destructive way to honor the sample's
offline switch. Live mode uses ``A2AT_LLM_PROVIDER=openai_traced``.

Run Explorer: when ``A2AT_SAMPLE_TRACE_URL`` (Web UI) or ``A2AT_SAMPLE_TRACE_DIR`` (CLI file) is
set, the runtime attaches the matching trace emitter as the process-wide span sink, so the server
side of a cross-process run reaches the client timeline (design §4).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import BasePushNotificationSender, InMemoryPushNotificationConfigStore
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_store import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentProvider,
    AgentSkill,
)
from a2a.utils.constants import TransportProtocol
from a2a_t.core.metadata import NEGOTIATION_T_EXTENSION_URI, TASK_T_EXTENSION_URI
from a2a_t.server.a2at_server import A2ATServer
from google.protobuf.json_format import MessageToDict
from shared.agent_card import (
    AGENT_CARD_DESCRIPTION,
    AGENT_CARD_EXAMPLE,
    AGENT_CARD_NAME,
    AGENT_CARD_ORGANIZATION,
    AGENT_CARD_PROVIDER_URL,
    AGENT_CARD_SKILL_ID,
)
from shared.config import SampleConfig, load_config
from shared.registry_client import register_agentcard
from shared.scenario import MAX_ROUNDS
from shared.scripted_llm import register_scripted_provider
from shared.sdk_env import prepare_sdk_env
from shared.trace import FileTraceEmitter, HttpTraceEmitter, set_global_sink
from shared.traced_llm import register_traced_providers
from starlette.applications import Starlette

from protocol.negotiation_gate import NegotiationGate
from protocol.server_executor import NegotiationAgentExecutor, build_negotiation_executor
from protocol.sqlite_task_store import SqliteTaskStore

#: Temporary SDK ``.env`` directory prefix of the server runtime.
SERVER_ENV_PREFIX: Final[str] = "a2at-ran-energy-saving-env-"

_LOGGER = logging.getLogger("ran_energy_saving.server_app")


@dataclass(frozen=True)
class ServerRuntime:
    """Assembled server runtime exposed to ``main`` and tests.

    Attributes:
        config: resolved sample configuration.
        agent_card: card served at ``/.well-known/agent-card.json`` and registered.
        prompt_server: A2A-T SDK server facade.
        gate: pre-task negotiation gate.
        executor: outer executor used by the request handler.
        task_store: Task store backing the A2A Task lifecycle (SQLite when configured).
        app: Starlette application.
        trace_emitter: Run Explorer emitter attached to this process, when configured.
    """

    config: SampleConfig
    agent_card: AgentCard
    prompt_server: A2ATServer
    gate: NegotiationGate
    executor: NegotiationAgentExecutor
    task_store: TaskStore
    app: Starlette
    trace_emitter: HttpTraceEmitter | FileTraceEmitter | None = None


def build_trace_emitter() -> HttpTraceEmitter | FileTraceEmitter | None:
    """Build the server trace emitter configured by the process environment.

    ``A2AT_SAMPLE_TRACE_URL`` selects the cross-process HTTP transport to the Web UI (with
    ``A2AT_SAMPLE_TRACE_TOKEN``); otherwise ``A2AT_SAMPLE_TRACE_DIR`` selects the CLI JSONL file
    ``<dir>/<runId>/server-trace.jsonl``. Without either key the server emits no trace.
    """
    base_url = os.environ.get("A2AT_SAMPLE_TRACE_URL", "").strip()
    if base_url:
        return HttpTraceEmitter(base_url, os.environ.get("A2AT_SAMPLE_TRACE_TOKEN", "").strip())
    directory = os.environ.get("A2AT_SAMPLE_TRACE_DIR", "").strip()
    if directory:
        return FileTraceEmitter(Path(directory))
    return None


def build_agent_card(*, host: str, port: int) -> AgentCard:
    """Build the AgentCard with the Task-T and Negotiation-T extension declarations.

    Args:
        host: bind host advertised to clients.
        port: bind port advertised to clients.
    """
    card = AgentCard(
        name=AGENT_CARD_NAME,
        description=AGENT_CARD_DESCRIPTION,
        version="1.0.0",
        provider=AgentProvider(organization=AGENT_CARD_ORGANIZATION, url=AGENT_CARD_PROVIDER_URL),
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
            extensions=[
                AgentExtension(
                    uri=TASK_T_EXTENSION_URI,
                    description="Extension of structured prompt TASK-T requests.",
                    required=False,
                ),
                AgentExtension(
                    uri=NEGOTIATION_T_EXTENSION_URI,
                    description="Extension of structured prompt Negotiation-T requests.",
                    required=False,
                ),
            ],
        ),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id=AGENT_CARD_SKILL_ID,
                name="RAN energy saving",
                description=(
                    "Creates an energy-saving task based on the energy-saving intent, and "
                    "periodically reports energy-saving intent reports."
                ),
                tags=["energy saving"],
                examples=[AGENT_CARD_EXAMPLE],
            )
        ],
    )
    card.supported_interfaces.add(
        url=f"http://{host}:{port}",
        protocol_binding=TransportProtocol.HTTP_JSON.value,
        protocol_version="1.0.0",
    )
    return card


def build_registration_payload(agent_card: AgentCard) -> dict[str, Any]:
    """Build the registry-center registration payload for one AgentCard."""
    return {"agentCards": [MessageToDict(agent_card)]}


def prepare_sdk_env_path(config: SampleConfig, env_path: Path | None = None) -> Path:
    """Return the ``.env`` file the A2A-T SDK should read.

    The caller-provided file (defaults to ``$CWD/.env``) is never modified: both modes write a
    temporary copy. In live mode the copy keeps the SDK endpoint values of the source file and
    normalizes empty or missing ``A2AT_LLM_*`` keys from :class:`SampleConfig`, so an empty
    ``A2AT_LLM_API_KEY`` becomes the local-proxy placeholder instead of failing the SDK's eager
    configuration validation. In offline mode the copy pins the scripted provider keys.

    Args:
        config: resolved sample configuration.
        env_path: optional source ``.env`` path.

    Raises:
        FileNotFoundError: in live mode when the resolved file does not exist.
    """
    base_path = env_path or Path.cwd() / ".env"
    if not config.is_offline and not base_path.exists():
        raise FileNotFoundError(f"Live mode requires an SDK .env file at {base_path}")
    return prepare_sdk_env(config, base_path, prefix=SERVER_ENV_PREFIX).path


def build_rest_app(
    *,
    agent_card: AgentCard,
    executor: NegotiationAgentExecutor,
    task_store: TaskStore | None = None,
    trace_emitter: HttpTraceEmitter | FileTraceEmitter | None = None,
) -> Starlette:
    """Build the Starlette app with the AgentCard routes and the A2A REST routes.

    The push-notification machinery is assembled unconditionally: it idles in SSE mode and is used
    when a client registers a webhook. The sender's HTTP client is closed by the app lifespan; the
    optional Run Explorer emitter is flushed and stopped there too.

    Args:
        agent_card: card served at the well-known A2A route.
        executor: negotiation-aware AgentExecutor.
        task_store: A2A Task store; defaults to an in-memory store (tests/direct runs).
        trace_emitter: optional Run Explorer span emitter.
    """
    resolved_task_store: TaskStore = task_store if task_store is not None else InMemoryTaskStore()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_httpx_client = httpx.AsyncClient(trust_env=False)
    push_sender = BasePushNotificationSender(push_httpx_client, push_config_store)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        del app
        yield
        await push_httpx_client.aclose()
        if trace_emitter is not None:
            trace_emitter.close()

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=resolved_task_store,
        agent_card=agent_card,
        push_config_store=push_config_store,
        push_sender=push_sender,
    )
    routes = [
        *create_agent_card_routes(agent_card),
        *create_rest_routes(request_handler),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.request_handler = request_handler
    app.state.task_store = resolved_task_store
    return app


def build_server_runtime(
    *,
    config: SampleConfig | None = None,
    env_path: Path | None = None,
    logger: logging.Logger | None = None,
) -> ServerRuntime:
    """Assemble the full server runtime.

    Args:
        config: resolved sample configuration; defaults to :func:`shared.config.load_config`.
        env_path: optional ``.env`` source path.
        logger: optional logger shared by the gate and the runtime.

    Returns:
        The assembled runtime, including the Starlette app.
    """
    resolved_logger = logger or _LOGGER
    resolved_config = config or load_config(env_path=env_path)
    register_traced_providers()
    if resolved_config.is_offline:
        register_scripted_provider()
    trace_emitter = build_trace_emitter()
    if trace_emitter is not None:
        set_global_sink(trace_emitter)
    sdk_env_path = prepare_sdk_env_path(resolved_config, env_path)
    prompt_server = A2ATServer(env_path=sdk_env_path, logger=resolved_logger)
    gate = NegotiationGate(prompt_server, max_rounds=MAX_ROUNDS, logger=resolved_logger)
    executor = build_negotiation_executor(gate=gate, config=resolved_config)
    task_store: TaskStore = (
        SqliteTaskStore(resolved_config.task_db_path)
        if resolved_config.task_db_path
        else InMemoryTaskStore()
    )
    agent_card = build_agent_card(host=resolved_config.host, port=resolved_config.port)
    app = build_rest_app(
        agent_card=agent_card,
        executor=executor,
        task_store=task_store,
        trace_emitter=trace_emitter,
    )
    app.state.config = resolved_config
    app.state.prompt_server = prompt_server
    app.state.gate = gate
    app.state.trace_emitter = trace_emitter
    return ServerRuntime(
        config=resolved_config,
        agent_card=agent_card,
        prompt_server=prompt_server,
        gate=gate,
        executor=executor,
        task_store=task_store,
        app=app,
        trace_emitter=trace_emitter,
    )


def build_server_app(
    *,
    config: SampleConfig | None = None,
    env_path: Path | None = None,
    logger: logging.Logger | None = None,
) -> Starlette:
    """Build only the Starlette app (see :func:`build_server_runtime` for the full runtime)."""
    return build_server_runtime(config=config, env_path=env_path, logger=logger).app


async def register_server_agent_card(
    agent_card: AgentCard,
    *,
    config: SampleConfig,
    env_path: Path | None = None,
    tolerate_failure: bool = False,
    log_sink: Any | None = print,
) -> dict[str, Any]:
    """Register the AgentCard with the registry center.

    Args:
        agent_card: card to register.
        config: sample configuration carrying the registry URL.
        env_path: optional ``.env`` path used by the registry client.
        tolerate_failure: when ``True``, a failed registration is logged and returned; otherwise it
            raises.
        log_sink: optional log sink.

    Raises:
        RuntimeError: when registration fails and ``tolerate_failure`` is ``False``.
    """
    result = await register_agentcard(
        registration_payload=build_registration_payload(agent_card),
        env_path=env_path,
        registry_center_url=config.registry_url,
        log_sink=log_sink,
    )
    if result.get("status") != "success":
        message = str(result.get("message", "unknown error"))
        if tolerate_failure:
            _LOGGER.warning("agent-card registration failed (tolerated): %s", message)
            return result
        raise RuntimeError(f"Agent card registration failed: {message}")
    _LOGGER.info("agent-card registration succeeded: %s", config.registry_url)
    return result


def main() -> None:
    """Entry point: assemble the runtime, register the card, and start uvicorn."""
    logging.basicConfig(level=logging.INFO)
    env_path = Path.cwd() / ".env"
    resolved_env_path = env_path if env_path.exists() else None
    runtime = build_server_runtime(env_path=resolved_env_path)
    asyncio.run(
        register_server_agent_card(
            runtime.agent_card,
            config=runtime.config,
            env_path=resolved_env_path,
            log_sink=print,
        )
    )
    _LOGGER.info(
        "starting RAN energy-saving server on %s:%s (model_mode=%s, model=%s, language=%s, task_db=%s)",
        runtime.config.host,
        runtime.config.port,
        runtime.config.model_mode,
        runtime.config.model_label,
        runtime.config.language,
        runtime.config.task_db_path or "in-memory",
    )
    uvicorn.run(runtime.app, host=runtime.config.host, port=runtime.config.port)


if __name__ == "__main__":
    main()
