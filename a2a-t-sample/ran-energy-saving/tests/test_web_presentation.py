"""Contract tests of the audience-facing presentation page.

The page is the demo default. These tests pin what must never leak into it (transport, model or
trace wording), what it must expose (scenario switcher, request entry, three acts) and that the
Run Explorer stays available behind ``--ui-mode explorer``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from demo.web.app import PRESENTATION_DIR, create_app
from shared.config import SampleConfig

#: Modules the page loads directly or transitively.
PRESENTATION_MODULES = ("app.js", "model.js", "copy.js")


@pytest.fixture
async def client(
    offline_config: SampleConfig,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the default (presentation) app."""
    app = create_app(offline_config, runs_dir=tmp_path, finished_grace_seconds=0.0)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_root_serves_the_presentation_shell(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    page = response.text
    assert "/static/app.css" in page
    assert "/static/app.js" in page
    for marker in (
        'id="scenario-chips"',
        'id="request-text"',
        'id="start"',
        'data-node="oss"',
        'data-node="nms"',
        'data-node="cells"',
        'id="rail"',
        'id="turns"',
        'id="rail-status"',
        'data-act="execution"',
        'data-act="result"',
    ):
        assert marker in page, marker
    # One module script, no inline payload rendering.
    assert page.count("<script") == 1
    assert "innerHTML" not in page


async def test_the_page_never_mentions_transport_or_trace_details(
    client: httpx.AsyncClient,
) -> None:
    page = (await client.get("/")).text.lower()

    for word in ("transport", "model mode", "trace", "inspector", "stream", "webhook", "实时"):
        assert word not in page, word


async def test_presentation_assets_are_served(client: httpx.AsyncClient) -> None:
    css = await client.get("/static/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--accent" in css.text

    for name in PRESENTATION_MODULES:
        response = await client.get(f"/static/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"]
        assert response.text.strip(), name
    # The entry module imports its two dependencies; both are ES modules.
    assert 'from "./model.js"' in (await client.get("/static/app.js")).text
    assert 'from "./copy.js"' in (await client.get("/static/app.js")).text


async def test_meta_reports_the_presentation_mode_and_the_five_scenarios(
    client: httpx.AsyncClient,
) -> None:
    meta = (await client.get("/api/meta")).json()

    assert meta["uiMode"] == "presentation"
    assert meta["scenarios"] == ["information", "target", "feasibility", "reject", "abort"]
    assert set(meta["scenarioRequests"]) == set(meta["scenarios"])
    assert all(meta["scenarioRequests"][name] for name in meta["scenarios"])


async def test_unknown_ui_mode_is_rejected(offline_config: SampleConfig, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported ui mode"):
        create_app(offline_config, runs_dir=tmp_path, ui_mode="cinema")


async def test_explorer_mode_still_serves_the_run_explorer(
    offline_config: SampleConfig,
    tmp_path: Path,
) -> None:
    app = create_app(offline_config, runs_dir=tmp_path, ui_mode="explorer")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        page = (await http_client.get("/")).text

    assert "/static/js/main.js" in page


def _node_binary() -> str:
    """Return the Node executable, skipping the test when Node is absent."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    return node


def test_presentation_modules_pass_node_syntax_check() -> None:
    node = _node_binary()

    for name in PRESENTATION_MODULES:
        result = subprocess.run(
            [node, "--check", str(PRESENTATION_DIR / name)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_presentation_model_harness_runs_in_node() -> None:
    node = _node_binary()
    harness = Path(__file__).resolve().parent / "js" / "presentation.test.mjs"

    result = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "presentation model harness: ok" in result.stdout


def test_presentation_sources_never_build_payloads_with_innerhtml() -> None:
    for name in PRESENTATION_MODULES:
        source = (PRESENTATION_DIR / name).read_text(encoding="utf-8")
        assert "innerHTML" not in source, name
        assert "outerHTML" not in source, name
        assert "insertAdjacentHTML" not in source, name
