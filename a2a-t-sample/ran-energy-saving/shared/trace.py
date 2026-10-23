"""Unified trace model of the RAN energy-saving sample (Run Explorer design §3).

The Web UI, the client flow and the LLM/tool producers share one span envelope so the timeline can
order protocol events, decisions and LLM calls with a single ``seq``. This module owns:

* the :class:`Span` dataclass and its camelCase wire form;
* the eight phase constants;
* :class:`TraceRecorder`, a thread-safe span factory that assigns ``seq``, per-phase ``phaseStep``
  and ``spanId`` and applies secret redaction plus payload truncation;
* :class:`TraceContext` + :func:`trace_scope` / :func:`trace_stage`, the ``ContextVar`` correlation
  used by the LLM/tool capture of the next task;
* :func:`set_global_sink` / :func:`emit_span`, the producer-side hook that is a no-op when the Web
  UI (or another consumer) has not attached a sink.

Tracing is an observer: no helper here may change the protocol flow, and a failing sink is ignored.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from a2a_t.core.metadata import NEGOTIATION_CONTEXT_METADATA_KEY

#: Version of the span envelope written by this module.
SCHEMA_VERSION: Final[int] = 1

#: Maximum characters kept per captured payload (design §3: 256KB per request/response).
MAX_PAYLOAD_CHARS: Final[int] = 256 * 1024

#: Characters kept at each end of a truncated string.
_TRUNCATED_HEAD_CHARS: Final[int] = 8 * 1024
_TRUNCATED_TAIL_CHARS: Final[int] = 8 * 1024

#: Marker added to a sanitized detail map when any value was redacted or truncated.
TRUNCATED_MARKER: Final[str] = "_traceTruncated"

#: Metadata keys whose values are never recorded (design §7).
_REDACTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api_key",
        "api-key",
        "apikey",
        "x-api-key",
        "x-auth-token",
        "access_token",
        "a2at_sample_trace_token",
        "webhook_token",
    }
)

#: The eight Run Explorer phases, in display order.
PHASE_DISPATCH: Final[str] = "dispatch"
PHASE_TASK_PROMPT: Final[str] = "task_prompt"
PHASE_TASK_VALIDATION: Final[str] = "task_validation"
PHASE_NEGOTIATION: Final[str] = "negotiation"
PHASE_RESOLUTION: Final[str] = "resolution"
PHASE_EXECUTION: Final[str] = "execution"
PHASE_REPORT: Final[str] = "report"
PHASE_DONE: Final[str] = "done"

#: All phases, in display order.
PHASES: Final[tuple[str, ...]] = (
    PHASE_DISPATCH,
    PHASE_TASK_PROMPT,
    PHASE_TASK_VALIDATION,
    PHASE_NEGOTIATION,
    PHASE_RESOLUTION,
    PHASE_EXECUTION,
    PHASE_REPORT,
    PHASE_DONE,
)

#: Sink signature shared by producers and the Web UI.
TraceSink = Callable[[dict[str, object]], None]


def _now_iso() -> str:
    """Return the current UTC time in ISO 8601 form."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_str(value: object, default: str = "") -> str:
    """Return one value as a string, ``default`` when absent."""
    return value if isinstance(value, str) else default


def _as_optional_str(value: object) -> str | None:
    """Return one value as an optional string."""
    return value if isinstance(value, str) and value else None


def _as_int(value: object, default: int = 0) -> int:
    """Return one value as an integer, ``default`` when absent or invalid."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_optional_number(value: object) -> float | None:
    """Return one value as an optional number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _truncate_text(value: str) -> tuple[str, bool]:
    """Keep the head and tail of one oversized string and mark the cut."""
    if len(value) <= MAX_PAYLOAD_CHARS:
        return value, False
    removed = len(value) - _TRUNCATED_HEAD_CHARS - _TRUNCATED_TAIL_CHARS
    return (
        value[:_TRUNCATED_HEAD_CHARS]
        + f"\n… [truncated {removed} characters] …\n"
        + value[-_TRUNCATED_TAIL_CHARS:],
        True,
    )


def _sanitize_value(value: object, truncated: list[bool]) -> object:
    """Recursively redact secrets and truncate oversized strings inside one payload."""
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.lower() in _REDACTED_KEYS:
                sanitized[key] = "[redacted]"
                continue
            sanitized[key] = _sanitize_value(raw_value, truncated)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, truncated) for item in value]
    if isinstance(value, str):
        text, was_truncated = _truncate_text(value)
        truncated[0] = truncated[0] or was_truncated
        return text
    if isinstance(value, bytes):
        text, was_truncated = _truncate_text(value.decode("utf-8", errors="replace"))
        truncated[0] = truncated[0] or was_truncated
        return text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_value(str(value), truncated)


def sanitize_payload(payload: Mapping[str, object] | None) -> tuple[dict[str, object], bool]:
    """Redact and truncate one detail payload.

    Args:
        payload: Arbitrary JSON-ish mapping supplied by a producer.

    Returns:
        A sanitized copy and whether anything was redacted or truncated. The copy carries
        :data:`TRUNCATED_MARKER` when the flag is set.
    """
    truncated = [False]
    sanitized_value = _sanitize_value(dict(payload or {}), truncated)
    sanitized: dict[str, object]
    if isinstance(sanitized_value, dict):
        sanitized = {str(key): value for key, value in sanitized_value.items()}
    else:
        sanitized = {"value": sanitized_value}
    rendered = json.dumps(sanitized, ensure_ascii=False, default=str)
    if len(rendered) > MAX_PAYLOAD_CHARS:
        truncated[0] = True
        sanitized = {
            "truncated": True,
            "originalLength": len(rendered),
            "preview": rendered[:2048],
        }
    if truncated[0]:
        sanitized[TRUNCATED_MARKER] = True
    return sanitized, truncated[0]


@dataclass
class Span:
    """One trace span (the unified protocol/decision/LLM/tool envelope).

    Attributes:
        schema_version: Envelope version.
        run_id: Owning run.
        seq: Global ordering number assigned by the recorder or the Web UI.
        span_id: Stable span identifier.
        parent_span_id: Optional parent span for nested spans.
        context_id: A2A context id, when known.
        task_id: A2A task id, when known.
        phase: One of :data:`PHASES`.
        phase_step: Step number inside ``phase``.
        round: Negotiation round, when applicable.
        actor: ``client`` / ``server`` / ``llm`` / ``tool`` / ``human`` / ``system``.
        side: ``client`` / ``server`` / ``shared``.
        kind: Semantic event kind (``message_sent``, ``decision_made``, ``llm``, ...).
        stage: Capture stage label (``client_decision_agent``, ...).
        title: One-line timeline title.
        purpose: Why this step happened.
        status: ``ok`` / ``error`` / ``pending`` / ``skipped``.
        started_at: UTC start time.
        ended_at: UTC end time, when measured.
        duration_ms: Duration in milliseconds, when measured.
        direction: ``client->server`` / ``server->client`` / ``internal``.
        model: Model id of an LLM span.
        tokens: Token usage map of an LLM span.
        error: Error text of a failed span.
        detail: Sanitized payload (prompt, raw message, tool args, ...).
    """

    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    seq: int = 0
    span_id: str = ""
    parent_span_id: str | None = None
    context_id: str | None = None
    task_id: str | None = None
    phase: str = PHASE_DISPATCH
    phase_step: int = 0
    round: int | None = None
    actor: str = ""
    side: str = ""
    kind: str = ""
    stage: str | None = None
    title: str = ""
    purpose: str | None = None
    status: str = "ok"
    started_at: str = field(default_factory=_now_iso)
    ended_at: str | None = None
    duration_ms: float | None = None
    direction: str | None = None
    model: str | None = None
    tokens: dict[str, object] | None = None
    error: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return the camelCase JSON envelope of this span."""
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "seq": self.seq,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "contextId": self.context_id,
            "taskId": self.task_id,
            "phase": self.phase,
            "phaseStep": self.phase_step,
            "round": self.round,
            "actor": self.actor,
            "side": self.side,
            "kind": self.kind,
            "stage": self.stage,
            "title": self.title,
            "purpose": self.purpose,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationMs": self.duration_ms,
            "direction": self.direction,
            "model": self.model,
            "tokens": self.tokens,
            "error": self.error,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Span:
        """Build one span from an envelope mapping (used by the Web UI trace ingestion)."""
        detail = data.get("detail")
        tokens = data.get("tokens")
        return cls(
            schema_version=_as_int(data.get("schemaVersion"), SCHEMA_VERSION),
            run_id=_as_str(data.get("runId")),
            seq=_as_int(data.get("seq")),
            span_id=_as_str(data.get("spanId")),
            parent_span_id=_as_optional_str(data.get("parentSpanId")),
            context_id=_as_optional_str(data.get("contextId")),
            task_id=_as_optional_str(data.get("taskId")),
            phase=_as_str(data.get("phase"), PHASE_DISPATCH) or PHASE_DISPATCH,
            phase_step=_as_int(data.get("phaseStep")),
            round=_as_int(data["round"]) if data.get("round") is not None else None,
            actor=_as_str(data.get("actor")),
            side=_as_str(data.get("side")),
            kind=_as_str(data.get("kind")),
            stage=_as_optional_str(data.get("stage")),
            title=_as_str(data.get("title")),
            purpose=_as_optional_str(data.get("purpose")),
            status=_as_str(data.get("status"), "ok") or "ok",
            started_at=_as_str(data.get("startedAt"), _now_iso()) or _now_iso(),
            ended_at=_as_optional_str(data.get("endedAt")),
            duration_ms=_as_optional_number(data.get("durationMs")),
            direction=_as_optional_str(data.get("direction")),
            model=_as_optional_str(data.get("model")),
            tokens=dict(tokens) if isinstance(tokens, Mapping) else None,
            error=_as_optional_str(data.get("error")),
            detail=dict(detail) if isinstance(detail, Mapping) else {},
        )


#: Backwards-friendly alias: the brief calls the same envelope ``TraceEvent``.
TraceEvent = Span


class TraceRecorder:
    """Thread-safe span factory owning the ordering state of one run.

    ``emit`` assigns a global ``seq``, a per-``phase`` ``phaseStep`` and a unique ``spanId`` when the
    caller does not supply them. Every detail payload is redacted and truncated before it is stored.

    Attributes:
        run_id: Run id stamped on every span generated here.
    """

    def __init__(self, run_id: str = "") -> None:
        """Create one recorder for a run (``run_id`` may be empty in CLI mode)."""
        self.run_id = run_id
        self._lock = threading.RLock()
        self._seq = 0
        self._span_counter = 0
        self._phase_steps: dict[str, int] = {}
        self._spans: dict[str, Span] = {}

    def new_span_id(self) -> str:
        """Return a fresh, unique span id."""
        with self._lock:
            return self._next_span_id()

    def _next_span_id(self) -> str:
        """Return the next unused generated span id (the lock must be held)."""
        while True:
            self._span_counter += 1
            candidate = f"span_{self._span_counter:04d}"
            if candidate not in self._spans:
                return candidate

    def emit(
        self,
        *,
        kind: str = "",
        phase: str = PHASE_DISPATCH,
        title: str = "",
        actor: str = "",
        side: str = "",
        direction: str | None = None,
        status: str = "ok",
        round: int | None = None,
        context_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
        purpose: str | None = None,
        model: str | None = None,
        tokens: Mapping[str, object] | None = None,
        error: str | None = None,
        detail: Mapping[str, object] | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        seq: int | None = None,
        phase_step: int | None = None,
        run_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        duration_ms: float | None = None,
    ) -> Span:
        """Create, store and return one span.

        Args:
            kind: Semantic kind of the span.
            phase: Phase constant the span belongs to.
            title: One-line timeline title.
            actor: Actor of the span.
            side: Side of the actor.
            direction: Direction of a protocol span.
            status: Status label.
            round: Negotiation round.
            context_id: A2A context id.
            task_id: A2A task id.
            stage: Capture stage label.
            purpose: Why the step happened.
            model: Model id of an LLM span.
            tokens: Token usage map.
            error: Error text.
            detail: Arbitrary payload (redacted/truncated).
            span_id: Explicit span id; generated when omitted.
            parent_span_id: Parent span id.
            seq: Explicit global sequence number; assigned when omitted.
            phase_step: Explicit per-phase step; assigned when omitted.
            run_id: Override of the recorder run id.
            started_at: Explicit start time.
            ended_at: Explicit end time.
            duration_ms: Explicit duration.

        Returns:
            The stored span.
        """
        sanitized_detail, _ = sanitize_payload(detail)
        safe_tokens = dict(tokens) if tokens is not None else None
        with self._lock:
            if seq is None:
                self._seq += 1
                resolved_seq = self._seq
            else:
                resolved_seq = max(0, int(seq))
                self._seq = max(self._seq, resolved_seq)
            if phase_step is None:
                step = self._phase_steps.get(phase, 0) + 1
                self._phase_steps[phase] = step
                resolved_step = step
            else:
                resolved_step = max(0, int(phase_step))
                self._phase_steps[phase] = max(self._phase_steps.get(phase, 0), resolved_step)
            resolved_span_id = span_id or self._next_span_id()
            span = Span(
                run_id=run_id if run_id is not None else self.run_id,
                seq=resolved_seq,
                span_id=resolved_span_id,
                parent_span_id=parent_span_id,
                context_id=context_id,
                task_id=task_id,
                phase=phase,
                phase_step=resolved_step,
                round=round,
                actor=actor,
                side=side,
                kind=kind,
                stage=stage,
                title=title,
                purpose=purpose,
                status=status,
                started_at=started_at or _now_iso(),
                ended_at=ended_at,
                duration_ms=duration_ms,
                direction=direction,
                model=model,
                tokens=safe_tokens,
                error=error,
                detail=sanitized_detail,
            )
            self._spans[resolved_span_id] = span
            return span

    def get(self, span_id: str) -> Span | None:
        """Return one recorded span by id, ``None`` when unknown."""
        with self._lock:
            return self._spans.get(span_id)

    @property
    def spans(self) -> list[Span]:
        """Every recorded span, ordered by ``seq``."""
        with self._lock:
            return sorted(self._spans.values(), key=lambda span: (span.seq, span.span_id))

    def to_dict(self) -> dict[str, object]:
        """Return the recorder document with every span in ``seq`` order."""
        return {
            "schemaVersion": SCHEMA_VERSION,
            "runId": self.run_id,
            "spans": [span.to_dict() for span in self.spans],
        }


@dataclass(frozen=True)
class TraceContext:
    """Correlation context carried across async boundaries (LLM/tool capture).

    Attributes:
        run_id: Current run id, when known.
        role: ``client`` / ``server``.
        context_id: A2A context id.
        task_id: A2A task id.
        stage: Explicit capture stage label.
    """

    run_id: str | None = None
    role: str | None = None
    context_id: str | None = None
    task_id: str | None = None
    stage: str | None = None


_CURRENT_TRACE: ContextVar[TraceContext] = ContextVar("a2at_sample_trace", default=TraceContext())


def current_trace() -> TraceContext:
    """Return the trace context of the current task."""
    return _CURRENT_TRACE.get()


@contextmanager
def trace_scope(
    *,
    run_id: str | None = None,
    role: str | None = None,
    context_id: str | None = None,
    task_id: str | None = None,
    stage: str | None = None,
) -> Generator[TraceContext, None, None]:
    """Temporarily merge trace correlation values into the current context.

    A value of ``None`` keeps the current value, so nested scopes can override a single field.
    """
    current = _CURRENT_TRACE.get()
    merged = TraceContext(
        run_id=run_id if run_id is not None else current.run_id,
        role=role if role is not None else current.role,
        context_id=context_id if context_id is not None else current.context_id,
        task_id=task_id if task_id is not None else current.task_id,
        stage=stage if stage is not None else current.stage,
    )
    token = _CURRENT_TRACE.set(merged)
    try:
        yield merged
    finally:
        _CURRENT_TRACE.reset(token)


@contextmanager
def trace_stage(
    stage: str,
    *,
    role: str | None = None,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Generator[TraceContext, None, None]:
    """Temporarily label the current capture stage, keeping every other correlation value."""
    with trace_scope(stage=stage, role=role, context_id=context_id, task_id=task_id) as context:
        yield context


_GLOBAL_SINK: TraceSink | None = None
_SINK_LOCK = threading.Lock()


def set_global_sink(sink: TraceSink | None) -> None:
    """Attach (or clear) the process-wide sink used by :func:`emit_span`.

    The Web UI attaches the current run state; CLI mode leaves it unset. A sink failure never
    propagates into the producer.
    """
    global _GLOBAL_SINK
    with _SINK_LOCK:
        _GLOBAL_SINK = sink


def emit_span(span: Span | Mapping[str, object]) -> None:
    """Forward one span to the global sink; no-op when no sink is attached."""
    with _SINK_LOCK:
        sink = _GLOBAL_SINK
    if sink is None:
        return
    payload = span.to_dict() if isinstance(span, Span) else dict(span)
    try:
        sink(payload)
    except Exception:  # noqa: BLE001 - tracing must never break the observed flow
        return


def infer_event_phase(event: Mapping[str, object]) -> str:
    """Infer the Run Explorer phase of one normalized A2A event."""
    kind = _as_str(event.get("kind"))
    if kind == "artifact":
        return PHASE_REPORT
    if kind == "status":
        if _as_str(event.get("state")) == "TASK_STATE_COMPLETED":
            return PHASE_REPORT
        return PHASE_EXECUTION
    if kind == "message":
        metadata = event.get("metadata")
        performative = ""
        if isinstance(metadata, Mapping):
            raw_context = metadata.get(NEGOTIATION_CONTEXT_METADATA_KEY)
            if isinstance(raw_context, Mapping):
                performative = _as_str(raw_context.get("performative")).upper()
        if performative in {"ABORT", "ACCEPT", "REJECT"}:
            return PHASE_RESOLUTION
        return PHASE_NEGOTIATION
    return PHASE_NEGOTIATION


def infer_decision_phase(action: str) -> str:
    """Infer the phase of one provider decision (counter stays in negotiation)."""
    return PHASE_NEGOTIATION if action == "counter" else PHASE_RESOLUTION


__all__ = [
    "MAX_PAYLOAD_CHARS",
    "PHASES",
    "PHASE_DISPATCH",
    "PHASE_DONE",
    "PHASE_EXECUTION",
    "PHASE_NEGOTIATION",
    "PHASE_REPORT",
    "PHASE_RESOLUTION",
    "PHASE_TASK_PROMPT",
    "PHASE_TASK_VALIDATION",
    "SCHEMA_VERSION",
    "TRUNCATED_MARKER",
    "Span",
    "TraceContext",
    "TraceEvent",
    "TraceRecorder",
    "TraceSink",
    "current_trace",
    "emit_span",
    "infer_decision_phase",
    "infer_event_phase",
    "sanitize_payload",
    "set_global_sink",
    "trace_scope",
    "trace_stage",
]
