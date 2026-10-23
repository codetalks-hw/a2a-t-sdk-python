"""Static contract tests of the Run Explorer assets (no network, no browser).

The page is a no-build ES-module app, so the tests assert the parts a bundler would otherwise
guarantee: every module is served, the import graph is complete, payload rendering never uses
``innerHTML``, and the visual tokens/responsive rules of the spec are present.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from shared.config import SampleConfig
from web.app import STATIC_DIR, create_app

#: JavaScript modules of the explorer.
MODULES: tuple[str, ...] = (
    "api",
    "dom",
    "filters",
    "format",
    "inspector",
    "main",
    "shortcuts",
    "store",
    "timeline",
)

#: The public API operations the UI contract relies on.
API_ROUTES: tuple[str, ...] = (
    "/api/meta",
    "/api/runs",
    "/api/runs/${encodeURIComponent(runId)}",
    "/events",
    "/decisions",
    "/spans/",
    "/trace.json",
    "/transcript.json",
    "/transcript.md",
)

@pytest.fixture
async def static_client(
    offline_config: SampleConfig,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the ASGI app (no run is started)."""
    app = create_app(offline_config, runs_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _module_source(name: str) -> str:
    """Read one JavaScript module from the static directory."""
    return (STATIC_DIR / "js" / f"{name}.js").read_text(encoding="utf-8")


def _imports(source: str) -> set[str]:
    """Return the module names imported by one ES module source."""
    return set(re.findall(r'from\s+"\./([\w-]+)\.js"', source))


async def test_every_asset_is_served(static_client: httpx.AsyncClient) -> None:
    css = await static_client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--bg-canvas" in css.text

    for name in MODULES:
        response = await static_client.get(f"/static/js/{name}.js")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"]
        assert "export " in response.text, name


async def test_the_module_graph_is_complete(static_client: httpx.AsyncClient) -> None:
    page = (await static_client.get("/")).text
    assert "/static/js/main.js" in page

    reached: set[str] = {"main"}
    pending = ["main"]
    while pending:
        name = pending.pop()
        for imported in _imports(_module_source(name)):
            if imported not in reached:
                reached.add(imported)
                pending.append(imported)
    assert set(MODULES) <= reached
    for name in reached:
        assert (STATIC_DIR / "js" / f"{name}.js").is_file(), name
        assert (await static_client.get(f"/static/js/{name}.js")).status_code == 200


def test_payloads_are_never_rendered_with_innerhtml() -> None:
    for name in MODULES:
        source = _module_source(name)
        assert "innerHTML" not in source, name
        assert "outerHTML" not in source, name
        assert "insertAdjacentHTML" not in source, name
    # Views must build nodes and set text explicitly.
    for name in ("timeline", "inspector", "main"):
        assert "textContent" in _module_source(name), name


def test_api_module_covers_the_backend_contract() -> None:
    source = _module_source("api")
    assert "EventSource" in source
    assert "seen" in source and "seq" in source
    assert "onDuplicate" in source
    for route in API_ROUTES:
        assert route in source, route
    assert "getSpan" in source


def test_store_module_deduplicates_and_exposes_the_phase_model() -> None:
    source = _module_source("store")
    assert "if (this.frames.has(seq)) return false;" in source
    for phase in (
        "dispatch",
        "task_prompt",
        "task_validation",
        "negotiation",
        "resolution",
        "execution",
        "report",
        "done",
    ):
        assert phase in source, phase
    assert "phaseStates" in source
    assert "decisionLog" in source
    assert "anomalies" in source
    assert "n/a (scripted)" in _module_source("format")


def test_inspector_declares_all_tabs_and_lazy_lookup() -> None:
    source = _module_source("inspector")
    for tab in ("summary", "protocol", "llm-request", "llm-response", "tool", "raw"):
        assert f'key: "{tab}"' in source, tab
    assert "getSpan" not in source  # the API stays in api.js/main.js
    assert "fetchSpan" in source
    assert "systemInstruction" in source
    assert "jsonSchema" in source
    assert "functionCalls" in source
    assert "parseError" in source
    assert "args" in source and "result" in source


def test_timeline_renders_phases_rows_and_nesting() -> None:
    source = _module_source("timeline")
    assert "phase-card" in source
    assert "tl-row" in source
    assert "actor-badge" in source
    assert "parentSpanId" in source
    assert "waterfall" in source
    assert "round-chip" in source
    assert "formatTokens" in source
    assert "shortModel" in source


def test_shortcuts_exist_for_every_documented_key() -> None:
    source = _module_source("shortcuts")
    for key in ("j", "k", "Enter", "Escape", "/", "f", "?", "1", "6", "[", "]"):
        assert f'"{key}"' in source, key
    for handler in ("next", "previous", "open", "escape", "search", "follow", "tab", "help", "submitDecision"):
        assert handler in source, handler
    assert "Cmd/Ctrl + Enter" in source


def test_filters_cover_the_five_chips_and_follow_states() -> None:
    source = _module_source("filters")
    for chip in ("all", "protocol", "llm", "tools", "errors"):
        assert f'key: "{chip}"' in source, chip
    assert "class FollowController" in source
    assert "Paused ·" in source


async def test_index_shell_is_accessible_and_complete(static_client: httpx.AsyncClient) -> None:
    page = (await static_client.get("/")).text
    assert 'class="skip-link"' in page
    assert 'role="banner"' in page
    assert "<main" in page
    assert 'aria-label="Run controls"' in page
    assert 'aria-label="Step inspector"' in page
    assert 'role="tablist"' in page
    assert 'aria-live="polite"' in page
    assert "<dialog" in page
    assert "prefers-color-scheme" not in page  # theme lives in CSS
    assert page.count("data-field=") == 10  # both manual panels carry all five fields
    assert 'data-role="countdown"' in page
    assert 'data-role="error"' in page
    for phase in (
        "dispatch",
        "task_prompt",
        "task_validation",
        "negotiation",
        "resolution",
        "execution",
        "report",
        "done",
    ):
        assert f'data-phase="{phase}"' in page, phase


def test_css_implements_the_visual_spec() -> None:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for token, value in (
        ("--bg-canvas", "#f6f7f9"),
        ("--bg-surface", "#ffffff"),
        ("--text", "#1a1f26"),
        ("--accent", "#1d4ed8"),
        ("--actor-client", "#1d4ed8"),
        ("--actor-server", "#0b6b4a"),
        ("--actor-llm", "#6d28d9"),
        ("--actor-tool", "#92400e"),
        ("--actor-human", "#0e7490"),
    ):
        assert token in css
        assert value in css.lower(), token
    assert "@media (prefers-color-scheme: dark)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert "grid-template-columns: var(--rail-width) minmax(0, 1fr) var(--inspector-width)" in css
    for breakpoint in ("max-width: 1359px", "max-width: 1119px", "max-width: 899px", "max-width: 639px"):
        assert breakpoint in css, breakpoint
    for anatomy in (".tl-row", ".tl-node", ".actor-badge", ".round-chip", ".status-pill", ".inspector-tabs"):
        assert anatomy in css, anatomy
