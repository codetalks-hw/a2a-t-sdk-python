"""Per-run SDK ``.env`` construction shared by the client and server runtimes.

The A2A-T SDK reads its configuration from a file, never from process environment variables, so
both runtimes hand it a temporary copy of the sample ``.env`` instead of mutating the source file.
The builder lives in ``shared`` so neither runtime has to import the other: the server used to
delegate to :mod:`protocol.client_runtime`, and the client used to import the server assembly just
for its AgentCard constants (which built an unused ADK model at import time).

Offline mode pins the traced scripted provider; live mode keeps the SDK endpoint values of the
source file and fills any missing one from the ADK model configuration, so both SDK and ADK address
the same endpoint (design §5.1).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

from shared.config import MODEL_MODE_OFFLINE, SampleConfig
from shared.traced_llm import register_traced_providers, sdk_provider_name

#: Prefix of the temporary SDK ``.env`` directories.
DEFAULT_ENV_PREFIX: Final[str] = "a2at-ran-sdk-env-"


@dataclass(frozen=True)
class SdkEnvFile:
    """Per-run SDK ``.env`` built for one A2ATClient/A2ATServer.

    Attributes:
        path: Path of the generated ``.env`` file.
        directory: Temporary directory owning the file (removed by the client runtime).
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


def prepare_sdk_env(
    config: SampleConfig,
    env_path: Path | None = None,
    *,
    prefix: str = DEFAULT_ENV_PREFIX,
) -> SdkEnvFile:
    """Build a per-run SDK ``.env`` without mutating the caller's file.

    ``A2AT_LANGUAGE`` always follows the process-level configuration. Offline mode pins the
    scripted provider; live mode keeps the SDK endpoint values of the source file and fills any
    missing one from the ADK model configuration, so both SDK and ADK address the same endpoint.

    Args:
        config: resolved sample configuration.
        env_path: source ``.env``; defaults to ``$CWD/.env``.
        prefix: prefix of the temporary directory (the caller owns its cleanup).

    Returns:
        The generated file and the temporary directory owning it.
    """
    # The generated file names a traced provider, so the matching registration is part of the
    # contract of this helper (a caller may build the facade right after it, as the tests do).
    register_traced_providers()
    source_path = env_path or Path.cwd() / ".env"
    values = _env_values(source_path)
    values["A2AT_LANGUAGE"] = config.language
    if config.model_mode == MODEL_MODE_OFFLINE:
        values.update(
            {
                "A2AT_LLM_PROVIDER": sdk_provider_name(offline=True),
                "A2AT_LLM_MODEL": "scripted",
                "A2AT_LLM_API_KEY": "not-needed",
                "A2AT_LLM_BASE_URL": values.get("A2AT_LLM_BASE_URL") or config.adk_model_api_base,
            }
        )
    else:
        values.update(
            {
                # The traced provider is a drop-in replacement of the SDK OpenAI provider; the Run
                # Explorer must see every structured call, so the source value never wins.
                "A2AT_LLM_PROVIDER": sdk_provider_name(offline=False),
                "A2AT_LLM_MODEL": values.get("A2AT_LLM_MODEL") or _sdk_model_name(config.adk_model_name),
                "A2AT_LLM_API_KEY": values.get("A2AT_LLM_API_KEY") or config.adk_model_api_key,
                "A2AT_LLM_BASE_URL": values.get("A2AT_LLM_BASE_URL") or config.adk_model_api_base,
            }
        )
    temp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    target = temp_dir / ".env"
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return SdkEnvFile(path=target, directory=temp_dir)


__all__ = ["DEFAULT_ENV_PREFIX", "SdkEnvFile", "prepare_sdk_env"]
