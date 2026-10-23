"""Tests for the Run Explorer trace foundation (``shared.trace``)."""

from __future__ import annotations

from shared.trace import (
    MAX_PAYLOAD_CHARS,
    PHASE_DISPATCH,
    PHASE_NEGOTIATION,
    TRUNCATED_MARKER,
    Span,
    TraceRecorder,
    current_trace,
    emit_span,
    set_global_sink,
    trace_scope,
    trace_stage,
)


def test_recorder_assigns_seq_phase_step_and_span_id() -> None:
    recorder = TraceRecorder("run-1")

    first = recorder.emit(kind="run_started", phase=PHASE_DISPATCH)
    second = recorder.emit(kind="runtime_ready", phase=PHASE_DISPATCH)
    third = recorder.emit(kind="propose", phase=PHASE_NEGOTIATION)

    assert (first.seq, second.seq, third.seq) == (1, 2, 3)
    assert (first.phase_step, second.phase_step, third.phase_step) == (1, 2, 1)
    assert first.span_id and second.span_id and first.span_id != second.span_id
    assert first.run_id == "run-1"


def test_recorder_respects_explicit_seq_step_and_span_id() -> None:
    recorder = TraceRecorder("run-1")

    recorder.emit(kind="a", phase=PHASE_DISPATCH)
    explicit = recorder.emit(
        kind="b",
        phase=PHASE_DISPATCH,
        seq=10,
        phase_step=7,
        span_id="custom-span",
    )

    assert explicit.seq == 10
    assert explicit.phase_step == 7
    assert explicit.span_id == "custom-span"
    assert recorder.get("custom-span") is explicit
    assert recorder.emit(kind="c", phase=PHASE_DISPATCH).seq == 11


def test_recorder_redacts_secrets_and_truncates_large_payloads() -> None:
    recorder = TraceRecorder("run-1")

    span = recorder.emit(
        kind="llm",
        phase=PHASE_NEGOTIATION,
        detail={
            "headers": {
                "Authorization": "Bearer secret",
                "X-API-Key": "key",
                "api_key": "key-2",
                "safe": "value",
            },
            "prompt": "a" * (MAX_PAYLOAD_CHARS + 1000),
        },
    )

    headers = span.detail["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "[redacted]"
    assert headers["X-API-Key"] == "[redacted]"
    assert headers["api_key"] == "[redacted]"
    assert headers["safe"] == "value"
    prompt = span.detail["prompt"]
    assert isinstance(prompt, str)
    assert "truncated" in prompt
    assert len(prompt) < MAX_PAYLOAD_CHARS
    assert span.detail[TRUNCATED_MARKER] is True


def test_span_wire_form_uses_the_envelope_keys() -> None:
    span = Span(run_id="r", seq=4, span_id="s", phase=PHASE_NEGOTIATION, phase_step=2, kind="llm")
    payload = span.to_dict()

    for key in (
        "schemaVersion",
        "runId",
        "seq",
        "spanId",
        "parentSpanId",
        "contextId",
        "taskId",
        "phase",
        "phaseStep",
        "round",
        "actor",
        "side",
        "kind",
        "stage",
        "title",
        "purpose",
        "status",
        "startedAt",
        "endedAt",
        "durationMs",
        "direction",
        "model",
        "tokens",
        "error",
        "detail",
    ):
        assert key in payload
    assert payload["schemaVersion"] == 1
    assert Span.from_dict(payload).span_id == "s"


def test_trace_context_scope_and_stage_nest_and_reset() -> None:
    assert current_trace().run_id is None

    with trace_scope(run_id="run-9", role="client", context_id="ctx-1") as scope:
        assert scope.run_id == "run-9"
        assert current_trace().role == "client"
        assert current_trace().context_id == "ctx-1"
        with trace_stage("client_decision_agent"):
            assert current_trace().stage == "client_decision_agent"
            assert current_trace().run_id == "run-9"
        assert current_trace().stage is None
        assert current_trace().run_id == "run-9"

    assert current_trace().run_id is None
    assert current_trace().role is None


def test_global_sink_receives_spans_and_is_noop_when_unset() -> None:
    seen: list[dict[str, object]] = []
    set_global_sink(seen.append)
    try:
        emit_span(Span(run_id="run-1", seq=1, span_id="s1", kind="llm"))
        emit_span({"runId": "run-1", "seq": 2, "spanId": "s2", "kind": "tool"})
    finally:
        set_global_sink(None)

    assert [payload["spanId"] for payload in seen] == ["s1", "s2"]

    # With no sink attached the producer is a no-op and never raises.
    emit_span(Span(run_id="run-1", seq=3, span_id="s3"))

    def failing_sink(span: dict[str, object]) -> None:
        del span
        raise RuntimeError("sink down")

    set_global_sink(failing_sink)
    try:
        emit_span(Span(run_id="run-1", seq=4, span_id="s4"))
    finally:
        set_global_sink(None)
