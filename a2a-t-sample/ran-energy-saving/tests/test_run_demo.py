"""Tests for the launcher defaults of ``run_demo.py``."""

from __future__ import annotations

import run_demo


def test_ui_port_default_reads_the_env_key() -> None:
    """``A2AT_SAMPLE_UI_PORT`` is the default of ``--ui-port``; absent means 8080."""
    assert run_demo._ui_port_default({}) == run_demo.DEFAULT_UI_PORT
    assert run_demo._ui_port_default({"A2AT_SAMPLE_UI_PORT": "18080"}) == 18080


def test_parser_uses_the_configured_ui_port_default() -> None:
    """An explicit ``--ui-port`` overrides the configured default."""
    parser = run_demo._build_parser(default_ui_port=18080)

    assert parser.parse_args([]).ui_port == 18080
    assert parser.parse_args(["--ui-port", "9999"]).ui_port == 9999
