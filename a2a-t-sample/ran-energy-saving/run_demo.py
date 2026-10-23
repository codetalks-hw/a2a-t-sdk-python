#!/usr/bin/env python3
"""Three-process launcher of the RAN energy-saving ADK sample.

The registry center and the A2A server each bind a port; started in the foreground they would
block the terminal. This launcher starts them as **background subprocesses** (new layout:
``registry.registry_main`` and ``protocol.server_app``), waits for their ports, then runs the client
in the foreground — or serves the Web UI with ``--ui`` — and finally stops the background processes
unless ``--keep-alive`` is given.

Both background processes stream their output to the terminal live and interleaved (through a pipe
in a normal run, through a file follower under ``--keep-alive``). ``PYTHONPATH`` points at the sample
root; the legacy ``src/`` tree is never imported.

Every run gets its own ``.env`` (a copy of ``a2a-t-sample/.env`` or the sample ``.env`` with the
run-level keys overridden) in a temporary run directory, so the shared configuration is never
mutated. Language and model mode are process-level: they are written into the run ``.env`` and into
the child environment (design D18).

Usage:
    python run_demo.py --scenario information
    python run_demo.py --scenario target --mode manual --ui
    python run_demo.py --scenario reject --transport webhook --model-mode offline
    python run_demo.py --ui --ui-port 8080 --language zh-CN
    python run_demo.py --keep-alive          # leave registry/server running; stop with --stop
    python run_demo.py --stop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import TextIO

CASE_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = CASE_DIR.parent
REPO_ROOT = SAMPLE_DIR.parent
RUN_DIR = Path(tempfile.gettempdir()) / "a2at-ran-energy-saving-adk"
PID_FILE = RUN_DIR / "pids.json"

#: Source ``.env`` candidates, in resolution order.
ENV_CANDIDATES = (CASE_DIR / ".env", SAMPLE_DIR / ".env", SAMPLE_DIR / "env.example")

REGISTRY_MODULE = "registry.registry_main"
SERVER_MODULE = "protocol.server_app"

DEFAULT_REGISTRY_PORT = 5001
DEFAULT_SERVER_PORT = 8000
DEFAULT_UI_PORT = 8080

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return whether a TCP port accepts a connection."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _wait_port(port: int, timeout: float, proc: subprocess.Popen[str] | None = None) -> bool:
    """Wait until a port accepts connections, failing fast when the child exits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if _port_open(port):
            return True
        time.sleep(0.3)
    return False


def _source_env_path() -> Path | None:
    """Return the first existing source ``.env`` (or ``env.example``)."""
    for candidate in ENV_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _read_env(path: Path) -> dict[str, str]:
    """Read one dotenv file as plain string values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _env_int(values: dict[str, str], key: str, default: int) -> int:
    """Resolve one integer value with a readable failure."""
    raw = values.get(key) or os.environ.get(key) or str(default)
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"[run_demo] invalid integer for {key}: {raw}") from exc


def _ui_port_default(values: dict[str, str]) -> int:
    """Return the ``--ui-port`` default: ``A2AT_SAMPLE_UI_PORT``, else ``8080``."""
    return _env_int(values, "A2AT_SAMPLE_UI_PORT", DEFAULT_UI_PORT)


def _child_env(language: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment of the child processes (sample root on ``PYTHONPATH``)."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(CASE_DIR) + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["A2AT_LANGUAGE"] = language
    if extra:
        env.update(extra)
    return env


def _server_trace_env(*, ui: bool, ui_port: int) -> dict[str, str]:
    """Return the Run Explorer environment of the server process.

    ``--ui`` points the server at the Web UI trace endpoint with a per-run local token; the CLI
    points it at ``runs/<runId>/server-trace.jsonl`` under the sample root.
    """
    if ui:
        return {
            "A2AT_SAMPLE_TRACE_URL": f"http://127.0.0.1:{ui_port}",
            "A2AT_SAMPLE_TRACE_TOKEN": secrets.token_hex(16),
        }
    return {"A2AT_SAMPLE_TRACE_DIR": str(CASE_DIR / "runs")}


def _trace_summary(spans: list[dict[str, object]]) -> str:
    """Summarize the LLM/tool spans of one CLI client run (counts and token usage)."""
    llm = [span for span in spans if span.get("kind") == "llm"]
    tools = [span for span in spans if span.get("kind") == "tool"]
    prompt = 0
    completion = 0
    total = 0
    measured = False
    for span in llm:
        tokens = span.get("tokens")
        if not isinstance(tokens, dict):
            continue
        prompt_value = tokens.get("prompt")
        completion_value = tokens.get("completion")
        total_value = tokens.get("total")
        if isinstance(prompt_value, int):
            prompt += prompt_value
        if isinstance(completion_value, int):
            completion += completion_value
        if isinstance(total_value, int):
            total += total_value
            measured = True
    usage = (
        f"prompt={prompt} completion={completion} total={total}"
        if measured
        else "n/a (scripted)"
    )
    return f"llm={len(llm)} tool={len(tools)} tokens={usage}"


def _write_run_env(
    *,
    source: Path | None,
    language: str,
    model_mode: str,
    discovery: str,
    transport: str,
    webhook_port: int,
    server_host: str,
    server_port: int,
    registry_host: str,
    registry_port: int,
    ui_port: int,
) -> Path:
    """Write the per-run ``.env`` and return its path."""
    values = _read_env(source) if source is not None else {}
    values.update(
        {
            "A2AT_LANGUAGE": language,
            "A2AT_SAMPLE_MODEL_MODE": model_mode,
            "A2AT_SAMPLE_DISCOVERY": discovery,
            "A2AT_SAMPLE_TRANSPORT": transport,
            "A2AT_SAMPLE_WEBHOOK_PORT": str(webhook_port),
            "A2AT_SAMPLE_HOST": server_host,
            "A2AT_SAMPLE_PORT": str(server_port),
            "A2AT_SAMPLE_UI_PORT": str(ui_port),
            "A2AT_SAMPLE_DIRECT_SERVER_URL": f"http://{server_host}:{server_port}",
            "REGISTRY_CENTER_HOST": registry_host,
            "REGISTRY_CENTER_PORT": str(registry_port),
        }
    )
    text = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
    env_path = RUN_DIR / ".env"
    env_path.write_text(text, encoding="utf-8", newline="\n")
    try:  # best effort: keep the copied config (may carry an API key) owner-only
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return env_path


def _stop_pids(pids: list[int]) -> int:
    """Terminate the recorded background processes."""
    stopped = 0
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, 15)
            stopped += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return stopped


def _load_pids() -> list[int]:
    """Load the persisted background PIDs."""
    if not PID_FILE.exists():
        return []
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    try:
        return [int(pid) for pid in data.get("pids", [])]
    except (TypeError, ValueError):
        return []


def _save_pids(pids: list[int]) -> None:
    """Persist the managed PIDs atomically so a concurrent ``--stop`` never reads a partial file."""
    tmp_path = PID_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"pids": pids}), encoding="utf-8")
    os.replace(tmp_path, PID_FILE)


def _show_tail(path: Path) -> None:
    """Print the tail of one child log."""
    if path.exists():
        print(f"--- {path} (tail) ---")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
            print(line)


def _follow_log(path: Path, stop: threading.Event, *, from_end: bool = False) -> None:
    """Echo lines appended to ``path`` to the terminal until ``stop`` is set."""
    while not stop.is_set() and not path.exists():
        time.sleep(0.1)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if from_end:
                handle.seek(0, os.SEEK_END)
            pending = ""
            while not stop.is_set():
                line = handle.readline()
                if not line:
                    time.sleep(0.15)
                    continue
                if line.endswith("\n"):
                    sys.stdout.write(pending + line)
                    sys.stdout.flush()
                    pending = ""
                else:
                    pending += line
            if pending:
                sys.stdout.write(pending)
                sys.stdout.flush()
    except OSError:
        pass


def _pump_pipe(stream: TextIO, log_handle: TextIO) -> None:
    """Echo a child's merged output pipe to the terminal and the log file in real time."""
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            sys.stdout.write(line)
            sys.stdout.flush()
            log_handle.write(line)
            log_handle.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _preflight(python: str, source_env: Path | None) -> bool:
    """Check the source configuration and the runtime dependencies."""
    if source_env is None:
        print("[run_demo] no source .env found; copy a2a-t-sample/env.example to a2a-t-sample/.env")
        return False
    check = subprocess.run(
        [
            python,
            "-c",
            "import a2a, a2a_t, google.adk, sse_starlette, starlette, uvicorn",
        ],
        cwd=str(SAMPLE_DIR),
        env=_child_env("en-US"),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        print(f"[run_demo] '{python}' is missing the sample dependencies.")
        print(f"           Run:  cd {SAMPLE_DIR}  &&  uv pip install -r requirements.txt")
        return False
    return True


async def _run_client_foreground(
    *,
    env_path: Path,
    scenario: str,
    mode: str,
    transport: str,
    language: str,
) -> int:
    """Run the client flow in-process and write its transcript."""
    from protocol.client_runtime import build_client_runtime
    from protocol.transcript import NegotiationTranscript
    from shared.config import load_config
    from shared.models import NegotiationProposeView

    config = load_config(env_path=env_path)
    transcript = NegotiationTranscript(
        scenario=scenario,
        language=language,
        model_mode=config.model_mode,
        mode=mode,
        transport=transport,
    )

    def event_sink(event: dict[str, object]) -> None:
        entry = transcript.record_event(event)
        performative = entry.get("performative") or entry.get("kind")
        print(f"[client] event kind={entry.get('kind')} performative={performative} summary={entry.get('summary')}")

    proposals: list[NegotiationProposeView] = []

    def on_decision_requested(propose: NegotiationProposeView, session: object) -> None:
        proposals.append(propose)
        print(f"[client] decision-requested round={getattr(session, 'round', '?')}")

    def on_decision_made(
        decision: object,
        propose: NegotiationProposeView,
        session: object,
    ) -> None:
        from shared.models import NegotiationDecision
        from shared.trace import infer_decision_phase

        if not isinstance(decision, NegotiationDecision):
            return
        transcript.record_decision(
            decision,
            propose=propose,
            phase=infer_decision_phase(decision.action),
        )
        print(f"[client] decision-made action={decision.action} round={getattr(session, 'round', '?')}")

    run_id = uuid.uuid4().hex
    trace_spans: list[dict[str, object]] = []

    def trace_sink(span: dict[str, object]) -> None:
        trace_spans.append(span)
        transcript.record_trace(span)

    runtime = None
    try:
        runtime = await build_client_runtime(
            config,
            scenario,
            mode,
            transport,
            env_path=env_path,
            log_sink=print,
        )
        result = await runtime.run(
            event_sink=event_sink,
            on_decision_requested=on_decision_requested,
            on_decision_made=on_decision_made,
            trace_sink=trace_sink,
            run_id=run_id,
        )
        for index, decision in enumerate(result.decisions):
            if index < len(transcript.decisions):
                continue
            propose = proposals[index] if index < len(proposals) else None
            transcript.record_decision(decision, propose=propose)
        transcript.record_final(result)
        print(f"[client] final_state={result.final_state} task_id={result.task_id or '-'}")
        print(f"[client] trace: {_trace_summary(trace_spans)}")
        print(f"[client] run id: {run_id}")
        print(f"[client] server trace: {CASE_DIR / 'runs' / run_id / 'server-trace.jsonl'}")
        if result.report:
            print(f"[client] report={json.dumps(result.report, ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001 - the launcher reports the failure and exits non-zero
        transcript.record_error(f"{type(exc).__name__}: {exc}")
        print(f"[client] failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if runtime is not None:
            await runtime.aclose()
        paths = transcript.write()
        print(f"[client] transcript: {paths.json_path}")
        print(f"[client] transcript: {paths.markdown_path}")
    return 0


def _run_ui(
    *,
    env_path: Path,
    ui_port: int,
    scenario: str,
    mode: str,
    transport: str,
    host: str = "127.0.0.1",
) -> int:
    """Serve the Web UI of the client process until interrupted."""
    import uvicorn
    from shared.config import load_config
    from web.app import create_app

    config = load_config(env_path=env_path)
    app = create_app(
        config,
        env_path=env_path,
        default_scenario=scenario,
        default_mode=mode,
        default_transport=transport,
    )
    trace_enabled = bool(os.environ.get("A2AT_SAMPLE_TRACE_TOKEN", "").strip())
    print(
        f"[run_demo] Web UI on http://{host}:{ui_port} "
        f"(language={config.language}, model_mode={config.model_label}, "
        f"defaults: scenario={scenario}, mode={mode}, transport={transport}, "
        f"server trace ingestion: {'enabled' if trace_enabled else 'disabled (no local token)'})"
    )
    try:
        uvicorn.run(app, host=host, port=ui_port, log_level="info")
    except KeyboardInterrupt:
        print("\n[run_demo] Web UI stopped")
        return 130
    return 0


def _build_parser(default_ui_port: int = DEFAULT_UI_PORT) -> argparse.ArgumentParser:
    """Build the CLI parser of the new launcher.

    Args:
        default_ui_port: default of ``--ui-port``, resolved from ``A2AT_SAMPLE_UI_PORT`` by the
            caller (``8080`` when the key is absent).
    """
    from protocol.client_runtime import SUPPORTED_MODES
    from shared.config import SUPPORTED_DISCOVERY_MODES, SUPPORTED_MODEL_MODES, SUPPORTED_TRANSPORTS
    from shared.scenario import SUPPORTED_LANGUAGES, SUPPORTED_SCENARIO_NAMES

    cli_modes = [mode for mode in SUPPORTED_MODES if mode != "scripted"]
    parser = argparse.ArgumentParser(description="Non-blocking launcher of the RAN energy-saving ADK sample.")
    parser.add_argument(
        "--scenario",
        default="information",
        choices=list(SUPPORTED_SCENARIO_NAMES),
        help="demonstration scenario (default: information)",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=cli_modes,
        help="client decision mode: ADK agent (auto) or Web UI decisions (manual; requires --ui)",
    )
    parser.add_argument(
        "--transport",
        default="sse",
        choices=list(SUPPORTED_TRANSPORTS),
        help="event transport (default: sse)",
    )
    parser.add_argument(
        "--discovery",
        default="registry",
        choices=list(SUPPORTED_DISCOVERY_MODES),
        help="server discovery: registry center (default) or direct AgentCard",
    )
    parser.add_argument(
        "--language",
        default="en-US",
        choices=list(SUPPORTED_LANGUAGES),
        help="process-level language of registry, server and client (default: en-US)",
    )
    parser.add_argument(
        "--model-mode",
        default="offline",
        choices=list(SUPPORTED_MODEL_MODES),
        help="offline (scripted + fake ADK model) or live (OpenAI-compatible endpoint)",
    )
    parser.add_argument("--ui", action="store_true", help="serve the Web UI instead of the CLI client")
    parser.add_argument(
        "--ui-port",
        type=int,
        default=default_ui_port,
        help=f"bind port of the Web UI (default: {default_ui_port}; A2AT_SAMPLE_UI_PORT)",
    )
    parser.add_argument("--keep-alive", action="store_true", help="leave registry/server running afterwards")
    parser.add_argument("--keep-logs", action="store_true", help="keep the temporary run directory (logs/.env)")
    parser.add_argument("--stop", action="store_true", help="stop a previous --keep-alive run and exit")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each port")
    return parser


def main() -> int:
    """Assemble the run directory, start the background processes and run the client."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(line_buffering=True)
        except ValueError:
            pass

    source_env = _source_env_path()
    source_values = _read_env(source_env) if source_env is not None else {}
    parser = _build_parser(default_ui_port=_ui_port_default(source_values))
    args = parser.parse_args()

    if args.stop:
        pids = _load_pids()
        if not pids:
            print("[run_demo] nothing to stop (no recorded background processes)")
            return 0
        stopped = _stop_pids(pids)
        PID_FILE.unlink(missing_ok=True)
        print(f"[run_demo] stopped {stopped}/{len(pids)} recorded background processes")
        return 0

    if args.mode == "manual" and not args.ui:
        parser.error("--mode manual requires --ui (the decision form lives in the Web UI)")

    python = sys.executable
    if not _preflight(python, source_env):
        return 1

    os.environ["A2AT_LANGUAGE"] = args.language
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_pids()
    registry_host = source_values.get("REGISTRY_CENTER_HOST") or "127.0.0.1"
    registry_port = _env_int(source_values, "REGISTRY_CENTER_PORT", DEFAULT_REGISTRY_PORT)
    server_host = source_values.get("A2AT_SAMPLE_HOST") or "127.0.0.1"
    server_port = _env_int(source_values, "A2AT_SAMPLE_PORT", DEFAULT_SERVER_PORT)
    webhook_port = _env_int(source_values, "A2AT_SAMPLE_WEBHOOK_PORT", 9000)

    env_path = _write_run_env(
        source=source_env,
        language=args.language,
        model_mode=args.model_mode,
        discovery=args.discovery,
        transport=args.transport,
        webhook_port=webhook_port,
        server_host=server_host,
        server_port=server_port,
        registry_host=registry_host,
        registry_port=registry_port,
        ui_port=args.ui_port,
    )

    # Drop a stale PID file only when its processes are gone (both ports closed).
    if existing and not _port_open(registry_port) and not _port_open(server_port):
        PID_FILE.unlink(missing_ok=True)
        existing = []

    managed: list[int] = list(existing)
    started_pids: list[int] = []
    log_handles: list[TextIO] = []
    followers: list[threading.Thread] = []
    pumps: list[threading.Thread] = []
    stop_events: list[threading.Event] = []

    def attach_follower(log_path: Path, *, from_end: bool = False) -> None:
        """Tail ``log_path`` to the terminal until the run's stop event is set."""
        stop_event = threading.Event()
        stop_events.append(stop_event)
        follower = threading.Thread(
            target=_follow_log,
            args=(log_path, stop_event),
            kwargs={"from_end": from_end},
            daemon=True,
        )
        follower.start()
        followers.append(follower)

    def start(
        name: str,
        module: str,
        port: int,
        extra_env: dict[str, str] | None = None,
    ) -> str | None:
        """Return ``started``/``reused``, or ``None`` on failure."""
        if _port_open(port):
            if not existing:
                print(f"[run_demo] port {port} is already in use by another process; free it first")
                return None
            print(f"[run_demo] {name} already listening on {port} (reusing recorded process)")
            attach_follower(RUN_DIR / f"{name}.log", from_end=True)
            return "reused"
        print(f"[run_demo] starting {name} on {port} ...")
        log_path = RUN_DIR / f"{name}.log"
        log_handle = log_path.open("w", encoding="utf-8")
        log_handles.append(log_handle)
        pump: threading.Thread | None = None
        if args.keep_alive:
            proc = subprocess.Popen(
                [python, "-m", module],
                cwd=str(RUN_DIR),
                env=_child_env(args.language, extra_env),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            attach_follower(log_path)
        else:
            proc = subprocess.Popen(
                [python, "-m", module],
                cwd=str(RUN_DIR),
                env=_child_env(args.language, extra_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert proc.stdout is not None
            pump = threading.Thread(target=_pump_pipe, args=(proc.stdout, log_handle), daemon=True)
            pump.start()
            pumps.append(pump)
        if not _wait_port(port, args.timeout, proc):
            print(f"[run_demo] {name} failed to start")
            if pump is not None:
                pump.join(timeout=1.0)
            _show_tail(log_path)
            _stop_pids(list(dict.fromkeys(started_pids + [proc.pid])))
            return None
        started_pids.append(proc.pid)
        managed.append(proc.pid)
        return "started"

    return_code = 0
    persisted = False
    try:
        if start("registry", REGISTRY_MODULE, registry_port) is None:
            return 1
        server_trace_env = _server_trace_env(ui=args.ui, ui_port=args.ui_port)
        if args.ui:
            # The UI process checks the same token the server sends; the URL is only read by the
            # server process, but keeping both keys together documents the paired configuration.
            os.environ["A2AT_SAMPLE_TRACE_TOKEN"] = server_trace_env["A2AT_SAMPLE_TRACE_TOKEN"]
        if start("server", SERVER_MODULE, server_port, server_trace_env) is None:
            return 1
        _save_pids(list(dict.fromkeys(managed)))
        persisted = True

        if args.ui:
            print(
                f"[run_demo] starting Web UI (scenario selectable, mode manual/auto, "
                f"transport={args.transport}, language={args.language}, model_mode={args.model_mode}) ..."
            )
            return_code = _run_ui(
                env_path=env_path,
                ui_port=args.ui_port,
                scenario=args.scenario,
                mode=args.mode,
                transport=args.transport,
            )
        else:
            print(
                f"[run_demo] running client (scenario={args.scenario}, mode={args.mode}, "
                f"transport={args.transport}, discovery={args.discovery}, language={args.language}, "
                f"model_mode={args.model_mode}) ..."
            )
            return_code = asyncio.run(
                _run_client_foreground(
                    env_path=env_path,
                    scenario=args.scenario,
                    mode=args.mode,
                    transport=args.transport,
                    language=args.language,
                )
            )
        return return_code
    except KeyboardInterrupt:
        print("\n[run_demo] interrupted")
        return 130
    finally:
        if args.keep_alive:
            if persisted:
                print(f"\n[run_demo] --keep-alive set; stop later with: {python} {Path(__file__).name} --stop")
        else:
            stopped = _stop_pids(managed)
            PID_FILE.unlink(missing_ok=True)
            print(f"\n[run_demo] stopped {stopped} background process(es)")
        for pump in pumps:
            pump.join(timeout=2.0)
        if followers:
            time.sleep(0.3)
        for stop_event in stop_events:
            stop_event.set()
        for follower in followers:
            follower.join(timeout=1.0)
        for handle in log_handles:
            handle.close()
        if not args.keep_alive:
            if args.keep_logs:
                print(f"[run_demo] logs kept at {RUN_DIR}")
            else:
                shutil.rmtree(RUN_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
