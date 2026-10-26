"""ADK model construction.

Configuration stays a pure value object; building a model needs a mode decision and, in offline
mode, a deterministic model implementation. The offline implementation is supplied by the
composition root (the demo catalog), so the core never imports the demo package.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm

from shared.config import ADK_MODEL_NAME_ENV_KEY, SampleConfig, load_config

#: Builds the deterministic offline model (the demo supplies its fake model).
OfflineModelFactory = Callable[[], BaseLlm]


def build_adk_model(
    config: SampleConfig | None = None,
    *,
    offline_model_factory: OfflineModelFactory | None = None,
) -> BaseLlm:
    """Build the ADK model of the configured mode.

    Offline mode needs an injected deterministic model: the sample's fake model lives with the demo
    assets, and a production deployment supplies its own double or leaves offline mode unused.

    Args:
        config: resolved sample configuration; defaults to :func:`load_config`.
        offline_model_factory: builder of the deterministic model used in offline mode.

    Returns:
        The ADK model of the configured mode.

    Raises:
        ValueError: when offline mode has no factory or live mode has no model name.
    """
    resolved = config or load_config()
    if resolved.is_offline:
        if offline_model_factory is None:
            raise ValueError(
                "Offline model mode needs a deterministic model factory; "
                "pass offline_model_factory or configure live mode."
            )
        return offline_model_factory()

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
    offline_model_factory: OfflineModelFactory | None = None,
) -> BaseLlm:
    """Build the ADK model of the configured mode wrapped in the Run Explorer tracer.

    Production entry points use this builder so every ADK turn produces an ``llm`` span. The
    wrapped model still reports the same ``model`` id and ``capabilities``; an explicit ``model``
    override (tests) is wrapped as-is, and an already-traced model is returned unchanged.

    Args:
        config: resolved sample configuration; defaults to :func:`load_config`.
        model: optional model override (the deterministic fake model in tests).
        offline_model_factory: builder of the deterministic model used in offline mode.
    """
    from shared.adk_tracing import TracingLlm

    resolved = (
        model
        if model is not None
        else build_adk_model(config, offline_model_factory=offline_model_factory)
    )
    if isinstance(resolved, TracingLlm):
        return resolved
    return TracingLlm.wrap(resolved)
