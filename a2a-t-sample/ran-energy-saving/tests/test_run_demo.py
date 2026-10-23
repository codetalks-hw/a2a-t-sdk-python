"""Tests for the launcher defaults and PID reuse decisions of ``run_demo.py``."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_server_trace_env_reuses_a_supplied_token() -> None:
    """A reused keep-alive server keeps its recorded token, so the UI accepts its spans."""
    reused = run_demo._server_trace_env(ui=True, ui_port=18080, trace_token="recorded-token")

    assert reused["A2AT_SAMPLE_TRACE_TOKEN"] == "recorded-token"
    fresh = run_demo._server_trace_env(ui=True, ui_port=18080)
    assert fresh["A2AT_SAMPLE_TRACE_TOKEN"] != "recorded-token"
    assert len(fresh["A2AT_SAMPLE_TRACE_TOKEN"]) == 32


def test_state_round_trip_records_the_run_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The PID file carries the mode/language/token a later launch needs to decide reuse."""
    pid_file = tmp_path / "pids.json"
    monkeypatch.setattr(run_demo, "PID_FILE", pid_file)

    run_demo._save_state([11, 12], model_mode="offline", language="en-US", trace_token="tok")

    state = run_demo._load_state()
    assert state == {
        "pids": [11, 12],
        "model_mode": "offline",
        "language": "en-US",
        "trace_token": "tok",
    }
    assert run_demo._load_pids() == [11, 12]


def test_state_loader_accepts_the_legacy_pid_only_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An older ``{"pids": [...]}`` file still stops, but carries no reusable metadata."""
    pid_file = tmp_path / "pids.json"
    pid_file.write_text(json.dumps({"pids": [7, "8", "nope"]}), encoding="utf-8")
    monkeypatch.setattr(run_demo, "PID_FILE", pid_file)

    assert run_demo._load_state() == {"pids": [7, "8", "nope"]}
    assert run_demo._load_pids() == [7, 8]


def test_state_loader_ignores_a_corrupt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.json"
    pid_file.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(run_demo, "PID_FILE", pid_file)

    assert run_demo._load_state() == {}
    assert run_demo._load_pids() == []


class TestServerReuseDecision:
    """The launcher may only reuse a listening server with the requested configuration."""

    #: Metadata of a compatible, UI-mode keep-alive record.
    STATE: dict[str, object] = {
        "pids": [11, 12],
        "model_mode": "offline",
        "language": "en-US",
        "trace_token": "tok",
    }

    def test_nothing_listening_starts(self) -> None:
        assert (
            run_demo._server_reuse_decision(
                port_open=False,
                state=self.STATE,
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_START
        )

    def test_matching_metadata_reuses_the_process(self) -> None:
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state=self.STATE,
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_REUSE
        )

    def test_model_mode_mismatch_restarts(self) -> None:
        """The offline-UI/live-server bug: the recorded live server must not be reused."""
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state={**self.STATE, "model_mode": "live"},
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_RESTART
        )

    def test_language_mismatch_restarts(self) -> None:
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state={**self.STATE, "language": "zh-CN"},
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_RESTART
        )

    def test_ui_run_restarts_a_cli_keep_alive_server(self) -> None:
        """A CLI server records spans to a file; the UI would get no server trace."""
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state={**self.STATE, "trace_token": ""},
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_RESTART
        )

    def test_cli_run_restarts_a_ui_keep_alive_server(self) -> None:
        """A UI server posts spans to its UI; the CLI runs cannot consume them."""
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state=self.STATE,
                model_mode="offline",
                language="en-US",
                ui=False,
            )
            == run_demo.REUSE_RESTART
        )

    def test_legacy_record_without_metadata_restarts(self) -> None:
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state={"pids": [11]},
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_RESTART
        )

    def test_foreign_listener_is_refused(self) -> None:
        """A port with no PID record is not ours: refuse instead of assuming a mode."""
        assert (
            run_demo._server_reuse_decision(
                port_open=True,
                state={},
                model_mode="offline",
                language="en-US",
                ui=True,
            )
            == run_demo.REUSE_REFUSE
        )


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
