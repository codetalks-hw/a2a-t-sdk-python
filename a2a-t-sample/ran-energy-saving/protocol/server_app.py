"""Application assembly of the RAN energy-saving A2A-T server.

The module builds the AgentCard (declaring the Task-T and Negotiation-T extensions), the A2A
request handler, the Starlette app and the registry registration. The server always runs the
negotiation gate in front of the ADK agent, so all negotiation happens before a Task is created.

Offline mode registers the scripted SDK provider and points the SDK at a temporary ``.env`` carrying
``A2AT_LLM_PROVIDER=scripted`` / ``A2AT_LLM_MODEL=scripted`` / ``A2AT_LLM_API_KEY=not-needed``; the
SDK reads its configuration from a file, not from process environment variables, so the temporary
copy is the non-destructive way to honor the sample's offline switch.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
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
from dotenv import dotenv_values
from google.adk.models.base_llm import BaseLlm
from google.protobuf.json_format import MessageToDict
from shared.config import SampleConfig, load_config
from shared.registry_client import register_agentcard
from shared.scenario import MAX_ROUNDS
from shared.scripted_llm import register_scripted_provider
from starlette.applications import Starlette

from protocol.negotiation_gate import NegotiationGate
from protocol.server_executor import NegotiationAgentExecutor, build_negotiation_executor

#: AgentCard name registered in the registry.
AGENT_CARD_NAME: Final[str] = "RAN Domain Agent"

#: AgentCard description.
AGENT_CARD_DESCRIPTION: Final[str] = "RAN Domain Agent"

#: Provider organization of the sample card.
AGENT_CARD_ORGANIZATION: Final[str] = "Huawei"

#: Provider URL of the sample card.
AGENT_CARD_PROVIDER_URL: Final[str] = "https://www.huawei.com"

#: Skill id of the card.
AGENT_CARD_SKILL_ID: Final[str] = "energy-saving"

#: Sample negotiation example advertised by the card.
AGENT_CARD_EXAMPLE: Final[str] = (
    "Create a RAN energy-saving task for XX Region to maximize energy savings. The "
    "time-segment-based guaranteed throughput target is {00:00~07:00,2Mbps}, "
    "{07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}."
)

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
        app: Starlette application.
    """

    config: SampleConfig
    agent_card: AgentCard
    prompt_server: A2ATServer
    gate: NegotiationGate
    executor: NegotiationAgentExecutor
    app: Starlette


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
    if not config.is_offline:
        if not base_path.exists():
            raise FileNotFoundError(f"Live mode requires an SDK .env file at {base_path}")
        # Deferred import: ``protocol.client_runtime`` imports this module at load time.
        from protocol.client_runtime import prepare_client_sdk_env

        return prepare_client_sdk_env(config, base_path).path

    values: dict[str, str] = {}
    if base_path.exists():
        values = {
            str(key): str(value)
            for key, value in dotenv_values(base_path).items()
            if key is not None and value is not None
        }
    values.update(
        {
            "A2AT_LLM_PROVIDER": "scripted",
            "A2AT_LLM_MODEL": "scripted",
            "A2AT_LLM_API_KEY": "not-needed",
            "A2AT_LANGUAGE": config.language,
        }
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="a2at-ran-energy-saving-env-"))
    target = temp_dir / ".env"
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return target


def build_rest_app(*, agent_card: AgentCard, executor: NegotiationAgentExecutor) -> Starlette:
    """Build the Starlette app with the AgentCard routes and the A2A REST routes.

    The push-notification machinery is assembled unconditionally: it idles in SSE mode and is used
    when a client registers a webhook. The sender's HTTP client is closed by the app lifespan.
    """
    task_store = InMemoryTaskStore()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_httpx_client = httpx.AsyncClient(trust_env=False)
    push_sender = BasePushNotificationSender(push_httpx_client, push_config_store)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        del app
        yield
        await push_httpx_client.aclose()

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
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
    return app


def build_server_runtime(
    *,
    config: SampleConfig | None = None,
    env_path: Path | None = None,
    model: BaseLlm | None = None,
    logger: logging.Logger | None = None,
) -> ServerRuntime:
    """Assemble the full server runtime.

    Args:
        config: resolved sample configuration; defaults to :func:`shared.config.load_config`.
        env_path: optional ``.env`` source path.
        model: optional ADK model override (tests inject the fake model).
        logger: optional logger shared by the gate and the runtime.

    Returns:
        The assembled runtime, including the Starlette app.
    """
    resolved_logger = logger or _LOGGER
    resolved_config = config or load_config(env_path=env_path)
    if resolved_config.is_offline:
        register_scripted_provider()
    sdk_env_path = prepare_sdk_env_path(resolved_config, env_path)
    prompt_server = A2ATServer(env_path=sdk_env_path, logger=resolved_logger)
    gate = NegotiationGate(prompt_server, max_rounds=MAX_ROUNDS, logger=resolved_logger)
    executor = build_negotiation_executor(gate=gate, config=resolved_config, model=model)
    agent_card = build_agent_card(host=resolved_config.host, port=resolved_config.port)
    app = build_rest_app(agent_card=agent_card, executor=executor)
    app.state.config = resolved_config
    app.state.prompt_server = prompt_server
    app.state.gate = gate
    return ServerRuntime(
        config=resolved_config,
        agent_card=agent_card,
        prompt_server=prompt_server,
        gate=gate,
        executor=executor,
        app=app,
    )


def build_server_app(
    *,
    config: SampleConfig | None = None,
    env_path: Path | None = None,
    model: BaseLlm | None = None,
    logger: logging.Logger | None = None,
) -> Starlette:
    """Build only the Starlette app (see :func:`build_server_runtime` for the full runtime)."""
    return build_server_runtime(config=config, env_path=env_path, model=model, logger=logger).app


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
        "starting RAN energy-saving server on %s:%s (model_mode=%s)",
        runtime.config.host,
        runtime.config.port,
        runtime.config.model_label,
    )
    uvicorn.run(runtime.app, host=runtime.config.host, port=runtime.config.port)


if __name__ == "__main__":
    main()
