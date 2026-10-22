"""Pytest bootstrap for the RAN energy-saving ADK sample.

The sample root is added to ``sys.path`` so ``shared``, ``agents``, ``registry``, and ``protocol``
import as top-level packages without an installation step.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SAMPLE_ROOT = Path(__file__).resolve().parents[1]
if str(SAMPLE_ROOT) not in sys.path:
    sys.path.insert(0, str(SAMPLE_ROOT))

from shared.config import (  # noqa: E402
    DISCOVERY_REGISTRY,
    MODEL_MODE_OFFLINE,
    TRANSPORT_SSE,
    SampleConfig,
)
from shared.scenario import LANGUAGE_EN  # noqa: E402


@pytest.fixture
def offline_config() -> SampleConfig:
    """Return a fully offline sample configuration (scripted LLM + fake ADK model)."""
    return SampleConfig(
        model_mode=MODEL_MODE_OFFLINE,
        language=LANGUAGE_EN,
        host="127.0.0.1",
        port=8000,
        registry_url="http://127.0.0.1:5001",
        webhook_port=9000,
        webhook_token="demo-webhook-token",
        webhook_idle_timeout=30,
        adk_model_provider="litellm",
        adk_model_name="openai/volcengine/deepseek-v4.1-flash",
        adk_model_api_base="http://127.0.0.1:31943/v1",
        adk_model_api_key="not-needed",
        discovery=DISCOVERY_REGISTRY,
        transport=TRANSPORT_SSE,
    )
