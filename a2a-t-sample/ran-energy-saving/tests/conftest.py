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


#: Nuisance warnings of the third-party a2a/ADK stack and of the SDK's retired negotiation modules.
#:
#: The a2a-sdk protobuf helper still calls the deprecated ``FieldDescriptor.label()``; ADK marks its
#: A2A integration and the JSON-schema function declaration experimental; and importing
#: ``A2ATClient`` pulls in the retired ``a2a_t.negotiation.*`` state-machine demo modules. None of
#: them indicates a problem in the sample, so the test output stays pristine. The entries match on
#: the warning message and category only, so an unexpected new warning is still reported.
def pytest_configure(config: pytest.Config) -> None:
    """Register the targeted warning filters of the sample test suite."""
    config.addinivalue_line(
        "filterwarnings", r"ignore:label\(\) is deprecated.*:DeprecationWarning"
    )
    config.addinivalue_line("filterwarnings", r"ignore:\[EXPERIMENTAL\].*:UserWarning")
    config.addinivalue_line(
        "filterwarnings",
        r"ignore:The a2a_t\.negotiation\..* is deprecated.*:DeprecationWarning",
    )


@pytest.fixture
def english_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the process language to ``en-US`` for the scripted SDK provider.

    ``ScriptedLLMClient`` resolves the language from the process environment (then ``$CWD/.env``);
    the in-process integration tests pass the language to the sample code through ``SampleConfig``,
    so the environment has to agree or the canned responses would not match the schemas.
    """
    monkeypatch.setenv("A2AT_LANGUAGE", LANGUAGE_EN)


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
        adk_model_name="openai/mass/volcengine/deepseek-v4.1-flash",
        adk_model_api_base="http://127.0.0.1:31943/v1",
        adk_model_api_key="not-needed",
        discovery=DISCOVERY_REGISTRY,
        transport=TRANSPORT_SSE,
    )
