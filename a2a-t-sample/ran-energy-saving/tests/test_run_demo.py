"""Tests for the launcher defaults and PID reuse decisions of ``run_demo.py``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import demo.run_demo as run_demo
import pytest
from dotenv import dotenv_values


def test_write_run_env_points_both_processes_at_the_task_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The server writes and the UI reads the same absolute SQLite task store."""
    monkeypatch.setattr(run_demo, "RUN_DIR", tmp_path)
    env_path = run_demo._write_run_env(
        source=None,
        language="en-US",
        model_mode="offline",
        discovery="direct",
        transport="sse",
        webhook_port=0,
        server_host="127.0.0.1",
        server_port=8000,
        registry_host="127.0.0.1",
        registry_port=5001,
        ui_port=8080,
        gateway_port=8001,
        gateway_register=True,
    )

    values = dotenv_values(env_path)
    task_db = Path(str(values["A2AT_SAMPLE_TASK_DB"]))
    client_db = Path(str(values["A2AT_SAMPLE_CLIENT_DB"]))

    assert task_db.is_absolute()
    assert task_db == run_demo.TASK_DB_PATH
    assert task_db.name == "task-status.sqlite3"
    assert client_db.is_absolute()
    assert client_db == run_demo.CLIENT_DB_PATH
    assert client_db.name == "client-state.sqlite3"
    gateway_db = Path(str(values["A2AT_SAMPLE_GATEWAY_TASK_DB"]))
    assert gateway_db.is_absolute()
    assert gateway_db == run_demo.GATEWAY_TASK_DB_PATH
    assert gateway_db.name == "gateway-task-status.sqlite3"
    assert values["A2AT_SAMPLE_GATEWAY_PORT"] == "8001"
    assert values["A2AT_SAMPLE_GATEWAY_REGISTER"] == "1"


def test_ui_port_default_reads_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``A2AT_SAMPLE_UI_PORT`` is the default of ``--ui-port``; absent means 8080."""
    monkeypatch.delenv("A2AT_SAMPLE_UI_PORT", raising=False)
    assert run_demo._ui_port_default({}) == run_demo.DEFAULT_UI_PORT
    assert run_demo._ui_port_default({"A2AT_SAMPLE_UI_PORT": "18080"}) == 18080


def test_ui_host_default_reads_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``A2AT_SAMPLE_UI_HOST`` is the default of ``--ui-host``; absent means loopback."""
    monkeypatch.delenv("A2AT_SAMPLE_UI_HOST", raising=False)
    assert run_demo._ui_host_default({}) == run_demo.DEFAULT_UI_HOST
    assert run_demo._ui_host_default({"A2AT_SAMPLE_UI_HOST": "0.0.0.0"}) == "0.0.0.0"


def test_parser_uses_the_configured_ui_port_default() -> None:
    """An explicit ``--ui-port`` overrides the configured default."""
    parser = run_demo._build_parser(default_ui_port=18080)

    assert parser.parse_args([]).ui_port == 18080
    assert parser.parse_args(["--ui-port", "9999"]).ui_port == 9999


def test_parser_uses_the_configured_ui_host_default() -> None:
    """An explicit ``--ui-host`` overrides the configured default."""
    parser = run_demo._build_parser(default_ui_host="0.0.0.0")

    assert parser.parse_args([]).ui_host == "0.0.0.0"
    assert parser.parse_args(["--ui-host", "127.0.0.1"]).ui_host == "127.0.0.1"


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

    run_demo._save_state(
        [11, 12],
        model_mode="offline",
        language="en-US",
        trace_token="tok",
        trace_url="http://127.0.0.1:8080",
        ui_port=8080,
    )

    state = run_demo._load_state()
    assert state == {
        "pids": [11, 12],
        "model_mode": "offline",
        "language": "en-US",
        "trace_token": "tok",
        "trace_url": "http://127.0.0.1:8080",
        "ui_port": 8080,
        "gateway_port": None,
    }
    assert run_demo._load_pids() == [11, 12]


def test_state_file_is_written_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The PID file carries the local trace token, so it must be ``0600`` like the SDK ``.env``."""
    pid_file = tmp_path / "pids.json"
    monkeypatch.setattr(run_demo, "PID_FILE", pid_file)
    chmod_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        run_demo.os, "chmod", lambda path, mode: chmod_calls.append((str(path), mode))
    )

    run_demo._save_state([1], model_mode="offline", language="en-US", trace_token="tok")

    assert chmod_calls == [(str(pid_file), 0o600)]


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
        "trace_url": "http://127.0.0.1:8080",
        "ui_port": 8080,
    }

    def decide(self, **overrides: Any) -> str:
        """Call the reuse decision with a compatible UI-mode request by default."""
        params: dict[str, Any] = {
            "port_open": True,
            "state": self.STATE,
            "model_mode": "offline",
            "language": "en-US",
            "ui": True,
            "trace_url": "http://127.0.0.1:8080",
            "ui_port": 8080,
        }
        params.update(overrides)
        return run_demo._server_reuse_decision(**params)

    def test_nothing_listening_starts(self) -> None:
        assert self.decide(port_open=False) == run_demo.REUSE_START

    def test_matching_metadata_reuses_the_process(self) -> None:
        assert self.decide() == run_demo.REUSE_REUSE

    def test_model_mode_mismatch_restarts(self) -> None:
        """The offline-UI/live-server bug: the recorded live server must not be reused."""
        assert (
            self.decide(state={**self.STATE, "model_mode": "live"}) == run_demo.REUSE_RESTART
        )

    def test_language_mismatch_restarts(self) -> None:
        assert self.decide(state={**self.STATE, "language": "zh-CN"}) == run_demo.REUSE_RESTART

    def test_trace_url_mismatch_restarts(self) -> None:
        """A server pointed at another UI port would post its spans to the wrong process."""
        assert (
            self.decide(trace_url="http://127.0.0.1:9090", ui_port=8080)
            == run_demo.REUSE_RESTART
        )

    def test_ui_port_mismatch_restarts(self) -> None:
        assert self.decide(ui_port=9090) == run_demo.REUSE_RESTART

    def test_legacy_record_without_a_trace_url_restarts(self) -> None:
        legacy = {key: value for key, value in self.STATE.items() if key != "trace_url"}
        assert self.decide(state=legacy) == run_demo.REUSE_RESTART

    def test_ui_run_restarts_a_cli_keep_alive_server(self) -> None:
        """A CLI server records spans to a file; the UI would get no server trace."""
        assert self.decide(state={**self.STATE, "trace_token": ""}) == run_demo.REUSE_RESTART

    def test_cli_run_restarts_a_ui_keep_alive_server(self) -> None:
        """A UI server posts spans to its UI; the CLI runs cannot consume them."""
        assert (
            self.decide(ui=False, trace_url="", ui_port=None) == run_demo.REUSE_RESTART
        )

    def test_legacy_record_without_metadata_restarts(self) -> None:
        assert self.decide(state={"pids": [11]}) == run_demo.REUSE_RESTART

    def test_foreign_listener_is_refused(self) -> None:
        """A port with no PID record is not ours: refuse instead of assuming a mode."""
        assert self.decide(state={}) == run_demo.REUSE_REFUSE


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
