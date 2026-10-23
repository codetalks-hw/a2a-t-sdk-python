"""Tests for the launcher defaults of ``run_demo.py``."""

from __future__ import annotations

import pytest
import run_demo


def test_ui_port_default_reads_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``A2AT_SAMPLE_UI_PORT`` is the default of ``--ui-port``; absent means 8080."""
    monkeypatch.delenv("A2AT_SAMPLE_UI_PORT", raising=False)
    assert run_demo._ui_port_default({}) == run_demo.DEFAULT_UI_PORT
    assert run_demo._ui_port_default({"A2AT_SAMPLE_UI_PORT": "18080"}) == 18080


def test_parser_uses_the_configured_ui_port_default() -> None:
    """An explicit ``--ui-port`` overrides the configured default."""
    parser = run_demo._build_parser(default_ui_port=18080)

    assert parser.parse_args([]).ui_port == 18080
    assert parser.parse_args(["--ui-port", "9999"]).ui_port == 9999


def test_server_trace_env_targets_the_ui_endpoint_or_the_cli_file() -> None:
    """``--ui`` posts to the UI with a local token; the CLI writes the per-run JSONL file."""
    ui_env = run_demo._server_trace_env(ui=True, ui_port=18080)

    assert ui_env["A2AT_SAMPLE_TRACE_URL"] == "http://127.0.0.1:18080"
    assert ui_env["A2AT_SAMPLE_TRACE_TOKEN"]

    cli_env = run_demo._server_trace_env(ui=False, ui_port=18080)

    assert "A2AT_SAMPLE_TRACE_URL" not in cli_env
    assert cli_env["A2AT_SAMPLE_TRACE_DIR"].replace("\\", "/").endswith("ran-energy-saving/runs")


def test_trace_summary_reports_llm_and_tool_counts() -> None:
    """The CLI summary prints LLM/tool counts and never fabricates scripted tokens."""
    scripted: list[dict[str, object]] = [
        {"kind": "llm", "tokens": {"prompt": None, "completion": None, "total": None}},
        {"kind": "tool"},
        {"kind": "message_sent"},
    ]
    assert run_demo._trace_summary(scripted) == "llm=1 tool=1 tokens=n/a (scripted)"

    live: list[dict[str, object]] = [
        {"kind": "llm", "tokens": {"prompt": 10, "completion": 5, "total": 15}}
    ]
    assert run_demo._trace_summary(live) == "llm=1 tool=0 tokens=prompt=10 completion=5 total=15"
