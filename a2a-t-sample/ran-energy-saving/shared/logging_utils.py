"""Logging utilities: stage formatting, text summaries, and the sample logger."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def format_stage_log(*, role: str, stage: str, detail: str) -> str:
    return f"[{role}] {stage}: {detail}"


def summarize_text(text: str, *, max_length: int = 80) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def resolve_sample_debug(*, env_path: Path | None = None) -> bool:
    raw_value = os.environ.get("A2AT_SAMPLE_DEBUG")
    if raw_value is None:
        resolved_env_path = env_path or Path.cwd() / ".env"
        env_values = dotenv_values(resolved_env_path) if resolved_env_path.exists() else {}
        raw_value = env_values.get("A2AT_SAMPLE_DEBUG")
    return str(raw_value or "").strip().lower() in _TRUTHY_VALUES


class SampleLogger:
    """Lightweight logger that emits level-prefixed messages to a sink, with debug gated by a flag."""
    def __init__(self, *, sink: Any, debug_enabled: bool) -> None:
        self._sink = sink
        self._debug_enabled = debug_enabled

    def info(self, message: str, *args: object) -> None:
        self._emit("info", message, *args)

    def debug(self, message: str, *args: object) -> None:
        if self._debug_enabled:
            self._emit("debug", message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._emit("warning", message, *args)

    def error(self, message: str, *args: object) -> None:
        self._emit("error", message, *args)

    def _emit(self, level: str, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self._sink(f"[sdk][{level}] {rendered}")


