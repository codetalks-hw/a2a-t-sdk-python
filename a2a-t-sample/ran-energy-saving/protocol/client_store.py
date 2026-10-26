"""SQLite persistence of the client agent's conversations, runs and negotiations.

The client and the RAN server are two systems with two databases:

* the server owns the A2A ``Task`` history (:mod:`protocol.sqlite_task_store`);
* the client owns the conversation, the run lifecycle and the pre-task negotiation audit
  (this module).

The client database is deliberately small and queryable: full transcripts and trace spans stay in
the ``runs/<runId>/`` files, while this store keeps the rows the Web UI, the A2A gateway and a
production console need to list, resume-by-inspection and audit a run.

The implementation uses the standard library only (WAL + busy timeout + one connection per
operation), matching :class:`protocol.sqlite_task_store.SqliteTaskStore`. Persistence failures are
logged and never break a client run.
"""

from __future__ import annotations

import asyncio
import builtins
import ctypes
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from shared.sqlite import SchemaMigration, apply_migrations, connect, readonly_connection

_LOGGER = logging.getLogger("ran_energy_saving.client_store")


#: Longest user request/title shown in list responses.
SUMMARY_LIMIT: Final[int] = 200

#: Run statuses owned by the client.
RUN_STATUS_RUNNING: Final[str] = "running"
RUN_STATUS_FINISHED: Final[str] = "finished"
RUN_STATUS_FAILED: Final[str] = "failed"
RUN_STATUS_INTERRUPTED: Final[str] = "interrupted"
RUN_STATUSES: Final[tuple[str, ...]] = (
    RUN_STATUS_RUNNING,
    RUN_STATUS_FINISHED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INTERRUPTED,
)

#: Turn roles.
TURN_ROLE_USER: Final[str] = "user"
TURN_ROLE_ASSISTANT: Final[str] = "assistant"
TURN_ROLE_SYSTEM: Final[str] = "system"

#: Negotiation event directions.
NEGOTIATION_DIRECTION_INBOUND: Final[str] = "inbound"
NEGOTIATION_DIRECTION_OUTBOUND: Final[str] = "outbound"

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id  TEXT PRIMARY KEY,
  external_id      TEXT NOT NULL DEFAULT '',
  user_id          TEXT NOT NULL DEFAULT '',
  title            TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  metadata_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
  turn_id          TEXT PRIMARY KEY,
  conversation_id  TEXT NOT NULL,
  seq              INTEGER NOT NULL,
  role             TEXT NOT NULL,
  content          TEXT NOT NULL,
  run_id           TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, seq);

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  conversation_id   TEXT,
  turn_id           TEXT,
  scenario          TEXT NOT NULL,
  mode              TEXT NOT NULL,
  transport         TEXT NOT NULL,
  discovery         TEXT NOT NULL,
  user_request      TEXT NOT NULL,
  status            TEXT NOT NULL,
  final_state       TEXT,
  server_context_id TEXT,
  server_task_id    TEXT,
  error             TEXT,
  owner_pid         INTEGER NOT NULL DEFAULT 0,
  idempotency_key   TEXT NOT NULL DEFAULT '',
  transcript_json   TEXT,
  transcript_markdown TEXT,
  trace_json        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_conversation ON runs(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency
  ON runs(conversation_id, idempotency_key) WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS negotiation_events (
  event_id          TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  seq               INTEGER NOT NULL,
  negotiation_id    TEXT,
  kind              TEXT,
  round             INTEGER,
  performative      TEXT,
  direction         TEXT NOT NULL,
  actor             TEXT NOT NULL,
  content_json      TEXT NOT NULL DEFAULT '{}',
  decision_action   TEXT,
  rationale         TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_negotiation_run ON negotiation_events(run_id, seq);

CREATE TABLE IF NOT EXISTS task_snapshots (
  run_id       TEXT PRIMARY KEY,
  task_id      TEXT,
  context_id   TEXT,
  state        TEXT,
  updated_at   TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _create_schema(connection: sqlite3.Connection) -> None:
    """Create the version-1 client schema."""
    connection.executescript(_SCHEMA)


#: Ordered schema migrations; index ``i`` upgrades version ``i`` to ``i + 1``.
_MIGRATIONS: Final[tuple[SchemaMigration, ...]] = (_create_schema,)


#: Fields ``update_run`` may change (keeps the SQL dynamic but closed).
_RUN_UPDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "conversation_id",
        "turn_id",
        "status",
        "final_state",
        "server_context_id",
        "server_task_id",
        "error",
        "transcript_json",
        "transcript_markdown",
        "trace_json",
        "finished_at",
    }
)


def now_iso() -> str:
    """Return the current UTC time in ISO 8601 form."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    """Return whether one process is still running (used to sweep stale client runs)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
        if not process_handle:
            return False
        ctypes.windll.kernel32.CloseHandle(process_handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _truncate(value: str, limit: int = SUMMARY_LIMIT) -> str:
    """Truncate one display string."""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dump_json(value: Mapping[str, object] | None) -> str:
    """Serialize one JSON object deterministically."""
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True)


def _load_json(value: object) -> dict[str, object]:
    """Parse one JSON object column, returning an empty object for malformed values."""
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


@dataclass(frozen=True)
class ConversationRecord:
    """One client conversation."""

    conversation_id: str
    external_id: str = ""
    user_id: str = ""
    title: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnRecord:
    """One conversation turn."""

    turn_id: str
    conversation_id: str
    seq: int
    role: str
    content: str
    run_id: str | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass(frozen=True)
class ClientRunRecord:
    """One client run."""

    run_id: str
    scenario: str
    mode: str
    transport: str
    discovery: str
    user_request: str
    status: str = RUN_STATUS_RUNNING
    conversation_id: str | None = None
    turn_id: str | None = None
    final_state: str | None = None
    server_context_id: str | None = None
    server_task_id: str | None = None
    error: str | None = None
    owner_pid: int = 0
    idempotency_key: str = ""
    transcript_json: str | None = None
    transcript_markdown: str | None = None
    trace_json: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    finished_at: str | None = None


@dataclass(frozen=True)
class NegotiationEventRecord:
    """One inbound proposal or outbound decision of a run."""

    event_id: str
    run_id: str
    seq: int
    direction: str
    actor: str
    negotiation_id: str | None = None
    kind: str | None = None
    round: int | None = None
    performative: str | None = None
    content: dict[str, object] = field(default_factory=dict)
    decision_action: str | None = None
    rationale: str | None = None
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        seq: int,
        direction: str,
        actor: str,
        negotiation_id: str | None = None,
        kind: str | None = None,
        round: int | None = None,
        performative: str | None = None,
        content: Mapping[str, object] | None = None,
        decision_action: str | None = None,
        rationale: str | None = None,
    ) -> NegotiationEventRecord:
        """Build one event with a fresh id and timestamp."""
        return cls(
            event_id=uuid.uuid4().hex,
            run_id=run_id,
            seq=seq,
            direction=direction,
            actor=actor,
            negotiation_id=negotiation_id,
            kind=kind,
            round=round,
            performative=performative,
            content=dict(content or {}),
            decision_action=decision_action,
            rationale=rationale,
        )


@dataclass(frozen=True)
class TaskSnapshotRecord:
    """Last observed A2A Task state of one client run."""

    run_id: str
    task_id: str | None = None
    context_id: str | None = None
    state: str | None = None
    updated_at: str = field(default_factory=now_iso)
    payload: dict[str, object] = field(default_factory=dict)


class ClientStore(Protocol):
    """Persistence surface used by the client service."""

    async def ensure_conversation(
        self,
        *,
        conversation_id: str,
        external_id: str = "",
        user_id: str = "",
        title: str = "",
    ) -> ConversationRecord | None: ...

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...

    async def append_turn(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        run_id: str | None = None,
    ) -> TurnRecord | None: ...

    async def list_turns(
        self,
        conversation_id: str,
        limit: int = 100,
    ) -> builtins.list[TurnRecord]: ...

    async def list_recent_turns(
        self,
        conversation_id: str,
        limit: int = 6,
    ) -> builtins.list[TurnRecord]: ...

    async def create_run(self, record: ClientRunRecord) -> None: ...

    async def update_run(self, run_id: str, **fields: object) -> None: ...

    async def get_run(self, run_id: str) -> ClientRunRecord | None: ...

    async def find_run_by_idempotency(
        self,
        conversation_id: str,
        idempotency_key: str,
    ) -> ClientRunRecord | None: ...

    async def list_runs(
        self,
        limit: int = 50,
        conversation_id: str | None = None,
    ) -> builtins.list[ClientRunRecord]: ...

    async def record_negotiation_event(self, record: NegotiationEventRecord) -> None: ...

    async def list_negotiation_events(
        self,
        run_id: str,
    ) -> builtins.list[NegotiationEventRecord]: ...

    async def save_task_snapshot(self, record: TaskSnapshotRecord) -> None: ...

    async def get_task_snapshot(self, run_id: str) -> TaskSnapshotRecord | None: ...

    async def mark_running_interrupted(self) -> int: ...


class SqliteClientStore:
    """SQLite implementation of :class:`ClientStore`."""

    def __init__(self, path: str | Path) -> None:
        """Open (or create) the client database at ``path``.

        Args:
            path: Absolute SQLite file path.
        """
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        """Return the backing SQLite file path."""
        return self._path

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        """Yield one connection, commit on success, and always close it."""
        with connect(self._path) as connection:
            yield connection

    def _initialize(self) -> None:
        """Apply the schema migrations (WAL is enabled by the shared connection helper)."""
        with self._connection() as connection:
            apply_migrations(connection, _MIGRATIONS)

    async def ensure_conversation(
        self,
        *,
        conversation_id: str,
        external_id: str = "",
        user_id: str = "",
        title: str = "",
    ) -> ConversationRecord | None:
        """Create or update one conversation and return its current row."""
        try:
            await asyncio.to_thread(
                self._ensure_conversation,
                conversation_id,
                external_id,
                user_id,
                title,
            )
            return await self.get_conversation(conversation_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("conversation persistence failed for %s: %s", conversation_id, exc)
            return None

    async def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Read one conversation."""
        try:
            return await asyncio.to_thread(self._get_conversation, conversation_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("conversation read failed for %s: %s", conversation_id, exc)
            return None

    async def append_turn(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        run_id: str | None = None,
    ) -> TurnRecord | None:
        """Append one turn and return it (``None`` on a persistence error)."""
        try:
            return await asyncio.to_thread(
                self._append_turn,
                conversation_id,
                role,
                content,
                run_id,
            )
        except sqlite3.Error as exc:
            _LOGGER.warning("turn persistence failed for %s: %s", conversation_id, exc)
            return None

    async def list_turns(
        self,
        conversation_id: str,
        limit: int = 100,
    ) -> builtins.list[TurnRecord]:
        """Return the conversation turns in chronological order."""
        try:
            return await asyncio.to_thread(self._list_turns, conversation_id, limit, False)
        except sqlite3.Error as exc:
            _LOGGER.warning("turn list failed for %s: %s", conversation_id, exc)
            return []

    async def list_recent_turns(
        self,
        conversation_id: str,
        limit: int = 6,
    ) -> builtins.list[TurnRecord]:
        """Return the newest ``limit`` turns in chronological order."""
        try:
            rows = await asyncio.to_thread(self._list_turns, conversation_id, limit, True)
        except sqlite3.Error as exc:
            _LOGGER.warning("recent turn list failed for %s: %s", conversation_id, exc)
            return []
        return list(reversed(rows))

    async def create_run(self, record: ClientRunRecord) -> None:
        """Persist one new run."""
        try:
            await asyncio.to_thread(self._create_run, record)
        except sqlite3.Error as exc:
            _LOGGER.warning("run persistence failed for %s: %s", record.run_id, exc)

    async def update_run(self, run_id: str, **fields: object) -> None:
        """Update the allowed mutable fields of one run."""
        unknown = sorted(set(fields) - _RUN_UPDATE_FIELDS)
        if unknown:
            raise ValueError(f"Unsupported run fields: {unknown}")
        try:
            await asyncio.to_thread(self._update_run, run_id, fields)
        except sqlite3.Error as exc:
            _LOGGER.warning("run update failed for %s: %s", run_id, exc)

    async def get_run(self, run_id: str) -> ClientRunRecord | None:
        """Read one run."""
        try:
            return await asyncio.to_thread(self._get_run, run_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("run read failed for %s: %s", run_id, exc)
            return None

    async def find_run_by_idempotency(
        self,
        conversation_id: str,
        idempotency_key: str,
    ) -> ClientRunRecord | None:
        """Return the run created by one idempotency key, when it exists."""
        if not idempotency_key:
            return None
        try:
            return await asyncio.to_thread(
                self._find_run_by_idempotency, conversation_id, idempotency_key
            )
        except sqlite3.Error as exc:
            _LOGGER.warning("idempotency lookup failed: %s", exc)
            return None

    async def list_runs(
        self,
        limit: int = 50,
        conversation_id: str | None = None,
    ) -> builtins.list[ClientRunRecord]:
        """Return the newest runs, optionally scoped to one conversation."""
        try:
            return await asyncio.to_thread(self._list_runs, limit, conversation_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("run list failed: %s", exc)
            return []

    async def record_negotiation_event(self, record: NegotiationEventRecord) -> None:
        """Persist one inbound proposal or outbound decision."""
        try:
            await asyncio.to_thread(self._record_negotiation_event, record)
        except sqlite3.Error as exc:
            _LOGGER.warning("negotiation persistence failed for %s: %s", record.run_id, exc)

    async def list_negotiation_events(
        self,
        run_id: str,
    ) -> builtins.list[NegotiationEventRecord]:
        """Return one run's negotiation events in order."""
        try:
            return await asyncio.to_thread(self._list_negotiation_events, run_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("negotiation list failed for %s: %s", run_id, exc)
            return []

    async def save_task_snapshot(self, record: TaskSnapshotRecord) -> None:
        """Upsert the last observed Task state of one run."""
        try:
            await asyncio.to_thread(self._save_task_snapshot, record)
        except sqlite3.Error as exc:
            _LOGGER.warning("task snapshot failed for %s: %s", record.run_id, exc)

    async def get_task_snapshot(self, run_id: str) -> TaskSnapshotRecord | None:
        """Read the last observed Task state of one run."""
        try:
            return await asyncio.to_thread(self._get_task_snapshot, run_id)
        except sqlite3.Error as exc:
            _LOGGER.warning("task snapshot read failed for %s: %s", run_id, exc)
            return None

    async def mark_running_interrupted(self) -> int:
        """Mark every run left ``running`` by a previous process as ``interrupted``."""
        try:
            return await asyncio.to_thread(self._mark_running_interrupted)
        except sqlite3.Error as exc:
            _LOGGER.warning("interrupted-run sweep failed: %s", exc)
            return 0

    # -- sqlite worker methods -------------------------------------------------

    def _ensure_conversation(
        self,
        conversation_id: str,
        external_id: str,
        user_id: str,
        title: str,
    ) -> None:
        stamp = now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO conversations
                    (conversation_id, external_id, user_id, title, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, '{}')
                ON CONFLICT(conversation_id) DO UPDATE SET
                    external_id=CASE WHEN excluded.external_id <> '' THEN excluded.external_id
                                     ELSE conversations.external_id END,
                    user_id=CASE WHEN excluded.user_id <> '' THEN excluded.user_id
                                 ELSE conversations.user_id END,
                    title=CASE WHEN excluded.title <> '' THEN excluded.title
                               ELSE conversations.title END,
                    updated_at=excluded.updated_at
                """,
                (
                    conversation_id,
                    external_id,
                    user_id,
                    _truncate(title, 120),
                    stamp,
                    stamp,
                ),
            )

    def _get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            external_id=str(row["external_id"]),
            user_id=str(row["user_id"]),
            title=str(row["title"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    def _append_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        run_id: str | None,
    ) -> TurnRecord:
        turn_id = uuid.uuid4().hex
        stamp = now_iso()
        with self._connection() as connection:
            # The sequence read and the insert must form one write transaction; without the
            # immediate lock two concurrent appends can both observe the same MAX(seq).
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            seq = int(row[0]) if row is not None else 1
            connection.execute(
                """
                INSERT INTO turns (turn_id, conversation_id, seq, role, content, run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, conversation_id, seq, role, content, run_id, stamp),
            )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                (stamp, conversation_id),
            )
        return TurnRecord(
            turn_id=turn_id,
            conversation_id=conversation_id,
            seq=seq,
            role=role,
            content=content,
            run_id=run_id,
            created_at=stamp,
        )

    def _list_turns(
        self,
        conversation_id: str,
        limit: int,
        newest_first: bool,
    ) -> builtins.list[TurnRecord]:
        resolved_limit = max(1, min(limit, 500))
        order = "DESC" if newest_first else "ASC"
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM turns WHERE conversation_id=? ORDER BY seq {order} LIMIT ?",
                (conversation_id, resolved_limit),
            ).fetchall()
        return [
            TurnRecord(
                turn_id=str(row["turn_id"]),
                conversation_id=str(row["conversation_id"]),
                seq=int(row["seq"]),
                role=str(row["role"]),
                content=str(row["content"]),
                run_id=str(row["run_id"]) if row["run_id"] else None,
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _create_run(self, record: ClientRunRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, conversation_id, turn_id, scenario, mode, transport, discovery,
                     user_request, status, final_state, server_context_id, server_task_id, error,
                     owner_pid, idempotency_key, transcript_json, transcript_markdown, trace_json,
                     created_at, updated_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.conversation_id,
                    record.turn_id,
                    record.scenario,
                    record.mode,
                    record.transport,
                    record.discovery,
                    record.user_request,
                    record.status,
                    record.final_state,
                    record.server_context_id,
                    record.server_task_id,
                    record.error,
                    record.owner_pid,
                    record.idempotency_key,
                    record.transcript_json,
                    record.transcript_markdown,
                    record.trace_json,
                    record.created_at,
                    record.updated_at,
                    record.finished_at,
                ),
            )

    def _update_run(self, run_id: str, fields: Mapping[str, object]) -> None:
        if not fields:
            return
        columns = sorted(fields)
        assignments = ", ".join(f"{column}=?" for column in columns)
        values: builtins.list[object] = [fields[column] for column in columns]
        values.extend([now_iso(), run_id])
        with self._connection() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments}, updated_at=? WHERE run_id=?",
                values,
            )

    def _row_to_run(self, row: sqlite3.Row) -> ClientRunRecord:
        return ClientRunRecord(
            run_id=str(row["run_id"]),
            conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            scenario=str(row["scenario"]),
            mode=str(row["mode"]),
            transport=str(row["transport"]),
            discovery=str(row["discovery"]),
            user_request=str(row["user_request"]),
            status=str(row["status"]),
            final_state=str(row["final_state"]) if row["final_state"] else None,
            server_context_id=(
                str(row["server_context_id"]) if row["server_context_id"] else None
            ),
            server_task_id=str(row["server_task_id"]) if row["server_task_id"] else None,
            error=str(row["error"]) if row["error"] else None,
            owner_pid=int(row["owner_pid"] or 0),
            idempotency_key=str(row["idempotency_key"]),
            transcript_json=str(row["transcript_json"]) if row["transcript_json"] else None,
            transcript_markdown=(
                str(row["transcript_markdown"]) if row["transcript_markdown"] else None
            ),
            trace_json=str(row["trace_json"]) if row["trace_json"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
        )

    def _get_run(self, run_id: str) -> ClientRunRecord | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row is not None else None

    def _find_run_by_idempotency(
        self,
        conversation_id: str,
        idempotency_key: str,
    ) -> ClientRunRecord | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM runs WHERE conversation_id=? AND idempotency_key=?",
                (conversation_id, idempotency_key),
            ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def _list_runs(
        self,
        limit: int,
        conversation_id: str | None,
    ) -> builtins.list[ClientRunRecord]:
        resolved_limit = max(1, min(limit, 500))
        query = "SELECT * FROM runs"
        params: builtins.list[object] = []
        if conversation_id:
            query += " WHERE conversation_id=?"
            params.append(conversation_id)
        query += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        params.append(resolved_limit)
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def _record_negotiation_event(self, record: NegotiationEventRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO negotiation_events
                    (event_id, run_id, seq, negotiation_id, kind, round, performative, direction,
                     actor, content_json, decision_action, rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.event_id,
                    record.run_id,
                    record.seq,
                    record.negotiation_id,
                    record.kind,
                    record.round,
                    record.performative,
                    record.direction,
                    record.actor,
                    _dump_json(record.content),
                    record.decision_action,
                    record.rationale,
                    record.created_at,
                ),
            )

    def _list_negotiation_events(self, run_id: str) -> builtins.list[NegotiationEventRecord]:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM negotiation_events WHERE run_id=? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return [
            NegotiationEventRecord(
                event_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                seq=int(row["seq"]),
                negotiation_id=(
                    str(row["negotiation_id"]) if row["negotiation_id"] else None
                ),
                kind=str(row["kind"]) if row["kind"] else None,
                round=int(row["round"]) if row["round"] is not None else None,
                performative=str(row["performative"]) if row["performative"] else None,
                direction=str(row["direction"]),
                actor=str(row["actor"]),
                content=_load_json(row["content_json"]),
                decision_action=(
                    str(row["decision_action"]) if row["decision_action"] else None
                ),
                rationale=str(row["rationale"]) if row["rationale"] else None,
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _save_task_snapshot(self, record: TaskSnapshotRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO task_snapshots
                    (run_id, task_id, context_id, state, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    context_id=excluded.context_id,
                    state=excluded.state,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    record.run_id,
                    record.task_id,
                    record.context_id,
                    record.state,
                    record.updated_at,
                    _dump_json(record.payload),
                ),
            )

    def _get_task_snapshot(self, run_id: str) -> TaskSnapshotRecord | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM task_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return TaskSnapshotRecord(
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]) if row["task_id"] else None,
            context_id=str(row["context_id"]) if row["context_id"] else None,
            state=str(row["state"]) if row["state"] else None,
            updated_at=str(row["updated_at"]),
            payload=_load_json(row["payload_json"]),
        )

    def _mark_running_interrupted(self) -> int:
        """Mark runs whose owning process is gone as ``interrupted``.

        A live second client process (for example the Web UI while the A2A gateway is running) owns
        its own runs; checking the recorded PID keeps its active runs untouched.
        """
        stamp = now_iso()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT run_id, owner_pid FROM runs WHERE status=?",
                (RUN_STATUS_RUNNING,),
            ).fetchall()
            stale = [
                str(run_id)
                for run_id, owner_pid in rows
                if not _pid_alive(int(owner_pid or 0))
            ]
            if not stale:
                return 0
            placeholders = ",".join("?" for _ in stale)
            cursor = connection.execute(
                f"UPDATE runs SET status=?, updated_at=?, finished_at=? "
                f"WHERE run_id IN ({placeholders})",
                (RUN_STATUS_INTERRUPTED, stamp, stamp, *stale),
            )
            return int(cursor.rowcount)


# ----------------------------------------------------------------------
# Read-only helpers used by the Web UI and external consoles
# ----------------------------------------------------------------------


def _readonly_connection(path: str | Path) -> sqlite3.Connection | None:
    """Open one read-only connection, ``None`` when the database is missing."""
    return readonly_connection(path)


def read_client_run_summaries(path: str | Path, limit: int = 50) -> builtins.list[dict[str, object]]:
    """Return UI rows for the newest client runs (never creates the database)."""
    connection = _readonly_connection(path)
    if connection is None:
        return []
    resolved_limit = max(1, min(limit, 500))
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT r.run_id, r.conversation_id, r.scenario, r.mode, r.status, r.final_state,
                   r.user_request, r.error, r.created_at, r.updated_at, r.finished_at,
                   (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = r.conversation_id)
                       AS turn_count
            FROM runs r
            ORDER BY r.created_at DESC, r.run_id DESC
            LIMIT ?
            """,
            (resolved_limit,),
        ).fetchall()
        return [
            {
                "runId": str(row["run_id"]),
                "conversationId": row["conversation_id"],
                "scenario": str(row["scenario"]),
                "mode": str(row["mode"]),
                "status": str(row["status"]),
                "finalState": row["final_state"],
                "userRequest": _truncate(str(row["user_request"])),
                "error": row["error"],
                "createdAt": str(row["created_at"]),
                "updatedAt": str(row["updated_at"]),
                "finishedAt": row["finished_at"],
                "turnCount": int(row["turn_count"] or 0),
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        _LOGGER.warning("client run summary read failed: %s", exc)
        return []
    finally:
        connection.close()


def read_client_run_detail(path: str | Path, run_id: str) -> dict[str, object] | None:
    """Return one run with its turns, negotiation events and Task snapshot."""
    connection = _readonly_connection(path)
    if connection is None:
        return None
    try:
        connection.row_factory = sqlite3.Row
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return None
        turns = connection.execute(
            "SELECT * FROM turns WHERE conversation_id=? ORDER BY seq ASC",
            (run["conversation_id"],),
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM negotiation_events WHERE run_id=? ORDER BY seq ASC",
            (run_id,),
        ).fetchall()
        snapshot = connection.execute(
            "SELECT * FROM task_snapshots WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return {
            "runId": str(run["run_id"]),
            "conversationId": run["conversation_id"],
            "turnId": run["turn_id"],
            "scenario": str(run["scenario"]),
            "mode": str(run["mode"]),
            "transport": str(run["transport"]),
            "discovery": str(run["discovery"]),
            "userRequest": str(run["user_request"]),
            "status": str(run["status"]),
            "finalState": run["final_state"],
            "serverContextId": run["server_context_id"],
            "serverTaskId": run["server_task_id"],
            "error": run["error"],
            "idempotencyKey": str(run["idempotency_key"]),
            "transcriptJson": run["transcript_json"],
            "transcriptMarkdown": run["transcript_markdown"],
            "traceJson": run["trace_json"],
            "createdAt": str(run["created_at"]),
            "updatedAt": str(run["updated_at"]),
            "finishedAt": run["finished_at"],
            "turns": [
                {
                    "turnId": str(row["turn_id"]),
                    "seq": int(row["seq"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "runId": row["run_id"],
                    "createdAt": str(row["created_at"]),
                }
                for row in turns
            ],
            "negotiationEvents": [
                {
                    "eventId": str(row["event_id"]),
                    "seq": int(row["seq"]),
                    "negotiationId": row["negotiation_id"],
                    "kind": row["kind"],
                    "round": int(row["round"]) if row["round"] is not None else None,
                    "performative": row["performative"],
                    "direction": str(row["direction"]),
                    "actor": str(row["actor"]),
                    "content": _load_json(row["content_json"]),
                    "decisionAction": row["decision_action"],
                    "rationale": row["rationale"],
                    "createdAt": str(row["created_at"]),
                }
                for row in events
            ],
            "taskSnapshot": (
                {
                    "taskId": snapshot["task_id"],
                    "contextId": snapshot["context_id"],
                    "state": snapshot["state"],
                    "updatedAt": str(snapshot["updated_at"]),
                    "payload": _load_json(snapshot["payload_json"]),
                }
                if snapshot is not None
                else None
            ),
        }
    except sqlite3.Error as exc:
        _LOGGER.warning("client run detail read failed: %s", exc)
        return None
    finally:
        connection.close()


def read_conversation_summaries(
    path: str | Path,
    limit: int = 50,
) -> builtins.list[dict[str, object]]:
    """Return UI rows for the newest conversations."""
    connection = _readonly_connection(path)
    if connection is None:
        return []
    resolved_limit = max(1, min(limit, 500))
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT c.conversation_id, c.external_id, c.user_id, c.title, c.created_at,
                   c.updated_at,
                   (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.conversation_id)
                       AS turn_count,
                   (SELECT COUNT(*) FROM runs r WHERE r.conversation_id = c.conversation_id)
                       AS run_count
            FROM conversations c
            ORDER BY c.updated_at DESC, c.conversation_id DESC
            LIMIT ?
            """,
            (resolved_limit,),
        ).fetchall()
        return [
            {
                "conversationId": str(row["conversation_id"]),
                "externalId": str(row["external_id"]),
                "userId": str(row["user_id"]),
                "title": str(row["title"]),
                "createdAt": str(row["created_at"]),
                "updatedAt": str(row["updated_at"]),
                "turnCount": int(row["turn_count"] or 0),
                "runCount": int(row["run_count"] or 0),
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        _LOGGER.warning("conversation summary read failed: %s", exc)
        return []
    finally:
        connection.close()


def read_conversation_detail(
    path: str | Path,
    conversation_id: str,
) -> dict[str, object] | None:
    """Return one conversation with its turns and runs."""
    connection = _readonly_connection(path)
    if connection is None:
        return None
    try:
        connection.row_factory = sqlite3.Row
        conversation = connection.execute(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if conversation is None:
            return None
        turns = connection.execute(
            "SELECT * FROM turns WHERE conversation_id=? ORDER BY seq ASC",
            (conversation_id,),
        ).fetchall()
        runs = connection.execute(
            "SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC",
            (conversation_id,),
        ).fetchall()
        return {
            "conversationId": str(conversation["conversation_id"]),
            "externalId": str(conversation["external_id"]),
            "userId": str(conversation["user_id"]),
            "title": str(conversation["title"]),
            "createdAt": str(conversation["created_at"]),
            "updatedAt": str(conversation["updated_at"]),
            "metadata": _load_json(conversation["metadata_json"]),
            "turns": [
                {
                    "turnId": str(row["turn_id"]),
                    "seq": int(row["seq"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "runId": row["run_id"],
                    "createdAt": str(row["created_at"]),
                }
                for row in turns
            ],
            "runs": [
                {
                    "runId": str(row["run_id"]),
                    "scenario": str(row["scenario"]),
                    "status": str(row["status"]),
                    "finalState": row["final_state"],
                    "createdAt": str(row["created_at"]),
                    "finishedAt": row["finished_at"],
                }
                for row in runs
            ],
        }
    except sqlite3.Error as exc:
        _LOGGER.warning("conversation detail read failed: %s", exc)
        return None
    finally:
        connection.close()


__all__ = [
    "ClientRunRecord",
    "ClientStore",
    "ConversationRecord",
    "NEGOTIATION_DIRECTION_INBOUND",
    "NEGOTIATION_DIRECTION_OUTBOUND",
    "NegotiationEventRecord",
    "RUN_STATUSES",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_FINISHED",
    "RUN_STATUS_INTERRUPTED",
    "RUN_STATUS_RUNNING",
    "SqliteClientStore",
    "TaskSnapshotRecord",
    "TURN_ROLE_ASSISTANT",
    "TURN_ROLE_SYSTEM",
    "TURN_ROLE_USER",
    "TurnRecord",
    "now_iso",
    "read_client_run_detail",
    "read_client_run_summaries",
    "read_conversation_detail",
    "read_conversation_summaries",
]
