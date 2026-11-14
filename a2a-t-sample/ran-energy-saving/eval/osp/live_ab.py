"""Run the OSP corpus with the real SDK against the live model, both carriers, and compare.

The scripted batch (`audit_batch`) answers "can the artifacts bind"; this one answers "what does the
real extraction produce, what does it cost, and does binding survive". Both arms run the same two
SDK calls (Task-T generation and semantic validation) against the live endpoint, so the token
counts are directly comparable; the only difference is whether the typed statement rides along.

The client receives no intent oracle here: ``filled`` comes from the SDK's own validation, which is
what a real deployment has.

Usage::

    PYTHONPATH=. python -m eval.osp.live_ab --json /tmp/a2at-live-ab.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
from demo import ran_data
from demo.approval_answer import approval_answer
from demo.approval_plan import plan_from_task_t
from demo.nms_sim.app import build_sim_app
from demo.nms_sim.config import FIDELITY_FAST, RATE_POLICY_OPTIONAL, WINDOW_POLICY_ANY, SimConfig
from demo.typed_payload import typed_statement_from_task_t
from protocol.client_flow import ClientFlowResult, run_client_flow
from protocol.decision_provider import ScriptedDecisionProvider
from protocol.nms_client import NmsClient, NmsConfig, NmsSseTransport
from shared.config import load_config
from shared.models import NegotiationDecision
from shared.run_constants import FINAL_STATE_TASK_COMPLETED, RUN_MODE_CREATE, RUN_MODE_MODIFY
from shared.sdk_env import prepare_sdk_env
from shared.trace import set_global_sink

from eval.osp.audit_batch import PROTOCOL_A2A_T, PROTOCOL_A2A_T_TYPED, _runnable_cases, _token

A2A_BASE: Final[str] = "http://sim/a2a/json"
TOKEN_URL: Final[str] = "http://sim/rest/plat/smapp/v1/sessions"
USER: Final[str] = "snbitestDefaultNorth"
VALUE: Final[str] = "Changeme_123"

#: Live settings of this measurement; recorded in the report header.
LIVE_ENV: Final[dict[str, str]] = {
    "A2AT_SAMPLE_MODEL_MODE": "live",
    "ADK_MODEL_NAME": "openai/mass/volcengine/deepseek-v4.1-flash",
    "ADK_MODEL_API_BASE": "http://127.0.0.1:31943/v1",
    "ADK_MODEL_API_KEY": "not-needed",
    # The validator's structured call returns empty content under the SDK default cap.
    "A2AT_LLM_MAX_TOKENS": "16384",
    "A2AT_LLM_REASONING_EFFORT": "none",
    "A2AT_LANGUAGE": "en-US",
}


@dataclass
class LiveRow:
    """One (case, carrier) live run and what it cost."""

    case_id: int
    operation: str
    carrier: str
    final_state: str
    completed: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    matched: int = 0
    mismatched: int = 0
    unverifiable: int = 0
    full_chain: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-friendly row."""
        return {
            "case": self.case_id,
            "operation": self.operation,
            "carrier": self.carrier,
            "finalState": self.final_state,
            "completed": self.completed,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "llmCalls": self.llm_calls,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "unverifiable": self.unverifiable,
            "fullChain": self.full_chain,
            "error": self.error,
        }


def _live_env_file() -> Path:
    """Write the live settings to a temporary ``.env`` and return its path."""
    directory = Path(tempfile.mkdtemp(prefix="a2at-live-ab-"))
    path = directory / "live.env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in LIVE_ENV.items()), encoding="utf-8"
    )
    return path


def _sim_config() -> SimConfig:
    return SimConfig(
        fidelity=FIDELITY_FAST,
        window_policy=WINDOW_POLICY_ANY,
        rate_policy=RATE_POLICY_OPTIONAL,
        approval=True,
    )


def _tokens(spans: Sequence[Mapping[str, object]]) -> tuple[int, int, int, int]:
    """Return prompt, completion, total tokens and the number of LLM spans."""
    prompt = completion = total = calls = 0
    for span in spans:
        if span.get("kind") != "llm":
            continue
        usage = span.get("tokens")
        if not isinstance(usage, Mapping):
            continue
        calls += 1
        prompt += int(usage.get("prompt") or 0)
        completion += int(usage.get("completion") or 0)
        total += int(usage.get("total") or 0)
    return prompt, completion, total, calls


def _row(
    case: Mapping[str, Any],
    carrier: str,
    result: ClientFlowResult,
    spans: Sequence[Mapping[str, object]],
) -> LiveRow:
    prompt, completion, total, calls = _tokens(spans)
    audit = result.audit or {}
    summary = audit.get("summary") or {}
    row = LiveRow(
        case_id=int(case["id"]),
        operation=str(case["operation"]),
        carrier=carrier,
        final_state=result.final_state,
        completed=result.final_state == FINAL_STATE_TASK_COMPLETED,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        llm_calls=calls,
        matched=int(summary.get("matched", 0)),
        mismatched=int(summary.get("mismatched", 0)),
        unverifiable=int(summary.get("unverifiable", 0)),
        error=result.error or "",
    )
    row.full_chain = row.completed and row.unverifiable == 0 and row.matched > 0
    return row


async def _run_case(
    case: Mapping[str, Any],
    client: Any,
    http: httpx.AsyncClient,
    *,
    carrier: str,
    task_id: str | None,
    context_id: str | None,
) -> LiveRow:
    from eval.osp.audit_batch import _create_baseline

    spans: list[dict[str, object]] = []
    token = set_global_sink(spans.append)
    try:
        nms = NmsClient(
            NmsConfig(
                a2a_base=A2A_BASE,
                token_url=TOKEN_URL,
                token_user=USER,
                token_value=VALUE,
                wire_log=False,
                defer_to_tracker=True,
            ),
            http_client=http,
        )
        result = await run_client_flow(
            prompt_client=client,
            transport=NmsSseTransport(nms, request_timeout=60.0),
            user_request=str(case.get("user_input") or ""),
            provider=ScriptedDecisionProvider(
                [NegotiationDecision(action="accept", rationale="scripted accept")]
            ),
            language="en-US",
            # The sample's pre-send gate: without it there is no extraction, hence no plan.
            validate_before_send=True,
            approval_answer=approval_answer(),
            approval_plan=plan_from_task_t,
            typed_statement=(
                typed_statement_from_task_t if carrier == PROTOCOL_A2A_T_TYPED else None
            ),
            run_mode=RUN_MODE_MODIFY if task_id else RUN_MODE_CREATE,
            target_task_id=task_id,
            target_context_id=context_id,
        )
    finally:
        set_global_sink(None)
        del token, _create_baseline
    return _row(case, carrier, result, spans)


async def run_batch(carriers: Sequence[str]) -> list[LiveRow]:
    """Run every runnable case for each carrier against the live model."""
    from a2a_t.client.a2at_client import A2ATClient

    env_path = _live_env_file()
    config = load_config(env_path=env_path)
    sdk_env = prepare_sdk_env(config, env_path=env_path)
    client = A2ATClient(env_path=sdk_env.path, logger=None)

    rows: list[LiveRow] = []
    for case in _runnable_cases():
        ran_data.reset()
        app = build_sim_app(_sim_config())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sim"
        ) as http:
            token = await _token(http)
            task_id: str | None = None
            context_id: str | None = None
            baseline = (case.get("baseline") or {}).get("request")
            if case.get("operation") == "Modify" and isinstance(baseline, str):
                from eval.osp.audit_batch import _create_baseline

                try:
                    task_id, context_id = await _create_baseline(http, token, baseline)
                except Exception:  # noqa: BLE001 - a case that cannot be prepared is a row
                    task_id = context_id = None
            for carrier in carriers:
                try:
                    rows.append(
                        await _run_case(
                            case,
                            client,
                            http,
                            carrier=carrier,
                            task_id=task_id,
                            context_id=context_id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one case must not end the batch
                    rows.append(
                        LiveRow(
                            case_id=int(case["id"]),
                            operation=str(case["operation"]),
                            carrier=carrier,
                            final_state="error",
                            completed=False,
                            error=f"{type(exc).__name__}: {exc}"[:300],
                        )
                    )
    return rows


def summarize(rows: Sequence[LiveRow]) -> dict[str, object]:
    """Return the per-carrier totals a report quotes."""
    out: dict[str, object] = {}
    for carrier in sorted({row.carrier for row in rows}):
        subset = [row for row in rows if row.carrier == carrier]
        done = [row for row in subset if row.completed]
        out[carrier] = {
            "cases": len(subset),
            "completed": len(done),
            "fullChain": sum(1 for row in done if row.full_chain),
            "promptTokens": sum(row.prompt_tokens for row in subset),
            "completionTokens": sum(row.completion_tokens for row in subset),
            "totalTokens": sum(row.total_tokens for row in subset),
            "tokensPerCompleted": (
                round(sum(row.total_tokens for row in done) / len(done)) if done else 0
            ),
            "llmCalls": sum(row.llm_calls for row in subset),
            "errors": sum(1 for row in subset if row.error),
        }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--carriers", default=f"{PROTOCOL_A2A_T},{PROTOCOL_A2A_T_TYPED}")
    args = parser.parse_args(argv)
    carriers = tuple(part.strip() for part in str(args.carriers).split(",") if part.strip())
    rows = asyncio.run(run_batch(carriers))
    summary = summarize(rows)
    for carrier, counts in summary.items():
        if isinstance(counts, dict):
            print(f"{carrier}: {counts}")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "settings": {**LIVE_ENV, "approval": True, "fidelity": FIDELITY_FAST},
                    "summary": summary,
                    "rows": [row.to_dict() for row in rows],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
