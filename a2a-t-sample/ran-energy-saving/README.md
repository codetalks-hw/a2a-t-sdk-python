# RAN Energy-Saving Sample: Task-T and Negotiation-T

An A2A-T sample with a client agent, a RAN domain server agent, and a mock registry center for
AgentCard discovery.

## What this sample demonstrates

A client agent commissions a RAN domain agent to run an energy-saving task over A2A. On this one
scenario the sample demonstrates the two A2A-T capability layers:

```text
Layer 1 — Task-T, the one-shot task
  natural language ──> task prompt ──> validated ──> executed ──> reported

Layer 2 — Negotiation-T, negotiate first when the request is not executable as-is
  information missing / target unclear / feasibility uncertain
    ──> the two agents negotiate ──> agreed ──> the task then runs as in Layer 1
```

Read it one of three ways:

- **"Just run it"** — [Quickstart](#quickstart).
- **"I integrate the one-shot task (Task-T)"** — [Layer 1](#layer-1--task-t-the-one-shot-task)
  plus the [Replace checklist](#replace-checklist-demo--production).
- **"I integrate negotiation (Negotiation-T)"** —
  [Layer 2](#layer-2--negotiation-t-negotiate-first-then-execute), plus
  [Transport](#transport-sse-default-or-webhook) if your client cannot hold a stream open.

## Quickstart

Prerequisites: Python (the repository `.venv`) and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1) repository root: create .venv and install the SDK
uv sync --dev

# 2) a2a-t-sample/ — the directory that holds .env
cd a2a-t-sample
cp env.example .env
uv pip install -r requirements.txt

# 3) run the sample (registry + server as background subprocesses, client in the foreground)
cd ran-energy-saving
python run_demo.py           # English (default); add --language zh-CN for the Chinese run
```

Use an interpreter that has the sample dependencies (the repository `.venv`), e.g.
`..\..\.venv\Scripts\python.exe run_demo.py` on Windows; both sides' output streams to the
terminal live and interleaved, then the background processes are stopped.

## Layer 1 — Task-T: the one-shot task

The business flow in four steps:

1. **Generate** — the client turns its natural-language input into a Task-T task request (a
   rendered prompt) with the A2A-T client SDK's `generate_task_prompt_from_text`.
2. **Send** — the prompt travels to the server in the message `metadata`.
3. **Validate** — the server reads the prompt from the metadata and calls the A2A-T server SDK's
   `validate_task_prompt_and_data_filling`, which validates it and extracts the parameters
   (`operationType`, `region`, `energySavingGoal`, `guaranteedThroughputTarget` — the caller schema
   `TASK_PARAM_SCHEMA` shared by both sides in [`src/common/task_schema.py`](src/common/task_schema.py));
   a rejected prompt is mapped to `TASK_STATE_REJECTED` instead of crashing the server.
4. **Execute and report** — the extracted parameters feed the business execution; progress comes
   back as status updates and the result as a report artifact, which the client presents.

Client side, the whole exchange is ([`client_flow.py`](src/client/client_flow.py)):

```python
prompt_content = prompt_client.generate_task_prompt_from_text(input_text, ENERGY_SAVING_TEMPLATE_URI)
prompt_text, extension_uri = _require_prompt_text(prompt_content)
request = SendMessageRequest()
request.message.parts.add().text = _build_request_metadata(initial_input)
request.message.metadata[extension_uri] = prompt_text   # the prompt travels in the metadata
request.message.metadata["templateUri"] = getattr(prompt_content, "template_uri", None) or ENERGY_SAVING_TEMPLATE_URI

call_context = ClientCallContext(service_parameters={"A2A-Extensions": extension_uri})
async for stream_response in a2a_client.send_message(request, context=call_context):
    ...  # consume the status updates and the report artifact
```

On the server the flow mirrors this: `_extract_prompt_text` reads the metadata and
`validate_task_prompt_and_data_filling` validates and extracts
([`server_flow.py:82`](src/server/server_flow.py#L82), [`server_flow.py:486`](src/server/server_flow.py#L486));
the validated parameters go into the execution seam. [Client key files](#client-key-files) shows
the layout.

**What you replace:** the template (`ENERGY_SAVING_TEMPLATE_URI` =
`Task-T/network-layer/ran-energy-saving/v1`) and the parameter schema are sample-specific — bring
your own ([`scenario_data.py`](src/client/scenario_data.py)); the business execution runs behind
the server-side `TaskExecutionService` seam (see the [Replace checklist](#replace-checklist-demo--production)).

## Client key files

| File | Role |
| --- | --- |
| [`src/client/client_main.py`](src/client/client_main.py) | Assembly root: reads `.env`, builds the runtime (`build_client_runtime`), discovers the server's AgentCard from the registry, starts/stops the webhook receiver, assembles the demo `ScriptedNegotiationResponseProvider`. The client-side `INTEGRATION POINT` comment sits in `main()`. Both layers. |
| [`src/client/client_flow.py`](src/client/client_flow.py) | Orchestration: the message sequence (generate → send → consume), the wire mechanics (metadata keys, `A2A-Extensions` header), the event pipeline, and the two transport drivers (SSE streaming / webhook queue). It calls the negotiation decision seam but owns no decision. Both layers. |
| [`src/client/negotiation_response.py`](src/client/negotiation_response.py) | The negotiation decision seam: the `NegotiationResponseProvider` Protocol, the `NegotiationDecision` answer contract, and the demo `ScriptedNegotiationResponseProvider`. Layer 2. |
| server: [`server_main.py`](src/server/server_main.py) / [`server_flow.py`](src/server/server_flow.py) / [`task_execution.py`](src/server/task_execution.py) | a2a-sdk app assembly with the unconditionally-assembled push machinery (server_main), the validate → negotiate-or-execute flow (server_flow), and the `TaskExecutionService` execution seam with the demo UC1 service (task_execution). The server-side `INTEGRATION POINT` comment sits in `build_server_runtime`. Both layers. |
| supporting: [`src/common/`](src/common), [`src/registry/`](src/registry), [`resources/mock_llm/`](resources/mock_llm), [`run_demo.py`](run_demo.py) | Shared adapters, the schema-routed mock LLM and `WebhookReceiver` (common); the mock registry, port 5001 (registry); scripted slot-extraction and validation replies (resources); the cross-platform launcher (run_demo). |

## Layer 2 — Negotiation-T: negotiate first, then execute

The boundary in one sentence: the SDK owns the wire — rendering, validation, the negotiation
session map, the transport mechanics — your system owns the business: when a validation failure
turns into a negotiation, and how your side answers a propose.

**When does the server open a negotiation?** When the round-1 Task-T validation fails with a
content error it prefers to resolve by talking instead of rejecting. Which failure means which
kind is a **sample-level convention, not an SDK contract** — the wire is self-describing: each
propose carries its own `templateUri`
(`Negotiation-T/{information,target,feasibility}-negotiation/propose/v1`) and the client
dispatches its accept on that URI:

| Validation error code | Trigger in business terms | Round 1 propose → round 2 accept | Demo input |
| --- | --- | --- | --- |
| `content.param_missing` | **information** — the request lacks a parameter the server needs | the server names the missing items (e.g. `region`) → the client answers with the filled values | `--scenario negotiation` |
| `content.value_not_allowed` | **target** — the target is well-formed but beyond or ambiguous for the server's capability | the server restates the intent and asks for confirmation of the clarified target → the client confirms its intent | `--negotiation target` |
| `content.semantic_conflict` | **feasibility** — the goal may not be achievable in the current network state | the server requests a feasibility evaluation of the conflicting contents (`action=REQUEST_FEASIBILITY_EVALUATION`) → the client answers with a feasibility summary | `--negotiation feasibility --channel fromText` |

The mapping constant is `_NEGOTIATION_TYPE_BY_ERROR_CODE`
([`server_flow.py:64`](src/server/server_flow.py#L64)) — edit your copy freely (remap codes, add
your own, or leave codes unmapped; unmapped codes keep the plain `TASK_STATE_REJECTED`).

**The closed loop in four messages:**

1. client → server: the Task-T task request (round 1); the content validation fails.
2. server → client: `TASK_STATE_INPUT_REQUIRED` with the Negotiation-T propose in the status
   message metadata (`build_metadata_content()`); the session travels in the nested
   `negotiationContext` metadata map — the SDK is stateless.
3. client → server: round 2 — the Task-T prompt regenerated from the filled parameters plus the
   Negotiation-T accept, both in the message metadata; the same task re-sent (`task_id` /
   `context_id` repeat round 1) with both extension URIs in the `A2A-Extensions` header.
4. server → client: the now-valid prompt executes — progress updates, the report artifact, and
   `TASK_STATE_COMPLETED` close the negotiation run (the default flow stays long-running).

**The decision seam — the one place you replace.** When message 2 arrives, the client flow builds
a lightweight `NegotiationProposeView` (kind, `templateUri`, rendered text) and calls
([`negotiation_response.py`](src/client/negotiation_response.py)):

```python
@runtime_checkable
class NegotiationResponseProvider(Protocol):
    def respond(
        self, propose: NegotiationProposeView, session: NegotiationContext
    ) -> NegotiationDecision: ...
```

`NegotiationDecision` carries a `conclusion` (`accept` / `reject` / `abort`) plus the per-type
accept field: `filled_params` (information, name → value), `confirmed_intent` (target),
`feasibility_summary` (feasibility), and `reason` (reject/abort). **`accept` is wired
end-to-end**: from your decision the flow regenerates the Task-T prompt, builds the typed accept,
advances the session, and sends round 2 over the configured transport. `respond` is synchronous —
an implementation that needs asynchrony (human confirmation, an upstream call) owns that
internally. The demo implementation is `ScriptedNegotiationResponseProvider`, assembled in
[`client_main.py`](src/client/client_main.py) under the `INTEGRATION POINT` comment.

**reject / abort — the extension point.** A `reject`/`abort` decision is **not wired in this
demo**: the flow logs `negotiation-decision: {conclusion} (not wired in this demo...)` and ends
without sending. The extension point is the reject/abort branch of `_plan_negotiation_accept` in
[`client_flow.py:747`](src/client/client_flow.py#L747): extend it to mirror
`_run_negotiation_accept_round` ([`client_flow.py:865`](src/client/client_flow.py#L865)) — advance
the session with `session.next_round().with_performative(NegotiationPerformative.REJECT)`, build
the proposed type's ending content with conclusion `Reject` (the decision's `reason` becomes the
ending content's `failure_reason`), and generate the round-2 message with the SDK's
`generate_negotiation_reject_prompt_from_data` / `..._from_text`. Abort is type-independent:
`NegotiationAbortContent(termination_reason)` renders against the common
`Negotiation-T/common/abort/v1` template via `generate_negotiation_abort_prompt_from_data`.

**fromData or fromText?** The default `fromData` channel renders the typed content
deterministically — no LLM call. `fromText` (`--channel fromText`) renders each message as
natural-language text first and the SDK extracts the structured content back — one extraction LLM
call per generated message; the wire format is identical.

## Transport: SSE (default) or webhook

Both transports deliver the same task events; the difference is how the client receives them:

- **SSE streaming (default)** — the client holds the streaming `message:send` open and consumes the events as they arrive.
- **Webhook push notifications** — for clients that do not hold a streaming connection open
  (long-running tasks, intermittently connected clients): `python run_demo.py --transport webhook`
  (or `A2AT_SAMPLE_TRANSPORT=webhook`). What changes: the client sends non-blocking
  (`return_immediately`) and every task event arrives as a webhook POST on a receiver embedded in
  the client process (`WebhookReceiver` in
  [`src/common/webhook_receiver.py`](src/common/webhook_receiver.py)), which binds one extra
  listening port (`--webhook-port`, default 9000). The server's pushes carry
  `A2AT_SAMPLE_WEBHOOK_TOKEN` (default `demo-webhook-token`) in the `X-A2A-Notification-Token`
  header — a mismatch answers `401` and the event is dropped. The flow ends on any SDK terminal
  state, with a 30 s idle backstop. The negotiation round trip is statement-identical on both
  transports. Registration paths, end signals, and security hardening: see the
  [Appendix](#appendix).

## Replace checklist (demo → production)

| Demo piece | What it does in the demo | Keep or replace |
| --- | --- | --- |
| mock LLM — [`resources/mock_llm/`](resources/mock_llm) + the schema-routed mock in `src/common/` | scripted slot-extraction and content-validation replies, selected when `A2AT_LLM_API_KEY` is empty | **KEEP** for CI/offline runs; in production remove it and configure a real OpenAI-compatible endpoint |
| scripted execution data — [`src/server/constants_data.py`](src/server/constants_data.py) | the UC1 progress steps and intent report the demo execution streams | **REPLACE** via the `TaskExecutionService` seam ([`src/server/task_execution.py`](src/server/task_execution.py)); once your service is assembled, the demo service and data can be deleted |
| scripted answers — [`src/client/scenario_data.py`](src/client/scenario_data.py) | feeds BOTH sides (see below) | **REPLACE** on both sides: the client answers via your `NegotiationResponseProvider`; the server's propose texts in your copy of `server_flow._negotiation_propose_data` become your business phrasing |
| mock registry — [`src/registry/`](src/registry) (port 5001) | AgentCard register/query for discovery | **REPLACE** with your registry center, or bypass discovery and construct the `a2a-sdk` client from a known AgentCard |
| static webhook token — `A2AT_SAMPLE_WEBHOOK_TOKEN` (default `demo-webhook-token`) | shared static token in the `X-A2A-Notification-Token` header | **REPLACE** with production auth (per-pair secrets, rotation, TLS); treat the receiver as an exposed HTTP endpoint |

> **`scenario_data` feeds BOTH sides** — the client's scripted answers (filled parameters,
> confirmed intent, feasibility summary) go through the provider seam, and the server's propose
> content is built from the same module: `_negotiation_propose_data`
> ([`server_flow.py:207`](src/server/server_flow.py#L207)) fills the target/feasibility propose
> texts from its accessors. Replacing only the provider and the execution service still leaves
> the server proposing the demo's canned texts — the module is not removable.

## Appendix

### Webhook details

- **Two registration paths.** Round 1 registers the push config inline on the request
  (`SendMessageConfiguration.task_push_notification_config`, a flat
  `TaskPushNotificationConfig(url, token)` — the task id is still unknown at this point); round 2
  re-registers the same URL/token through the `POST /tasks/{id}/pushNotificationConfigs` REST
  endpoint (a2a-sdk `BaseClient.create_task_push_notification_config`) before the negotiation
  accept is sent.
- **No server-side mode switch.** The server assembles the push machinery unconditionally
  (a2a-sdk `DefaultRequestHandler` with `InMemoryPushNotificationConfigStore` +
  `BasePushNotificationSender`, [`server_main.py`](src/server/server_main.py)); in SSE mode it
  simply idles.
- **Non-blocking path.** The a2a client is built with `ClientConfig(streaming=False)`, which is
  required to select the non-blocking `message:send` path (the `return_immediately` flag alone
  does not switch it). Push bodies are StreamResponse-shaped JSON — the same shape as the SSE
  events — so negotiation detection, artifact counting, and presentation are shared with SSE.
- **Token auth.** The receiver ([`webhook_receiver.py`](src/common/webhook_receiver.py)) validates
  `X-A2A-Notification-Token` on every POST: a mismatch answers `401` (the event is dropped), a
  malformed or unrecognized body answers `400`. The demo's static shared token is demo-grade — for
  production use per-client secrets with rotation over TLS.
- **End signals.** Any SDK terminal state (`TASK_STATE_COMPLETED` / `FAILED` / `CANCELED` /
  `REJECTED`) ends the flow in both phases — the negotiation scenarios normally end on
  `COMPLETED`, a failed validation or stream error ends on `REJECTED` / `FAILED` right away, and
  the summary notes a terminal end as `end=<state>`. The default scenario still ends on the report
  artifact; `INPUT_REQUIRED` (a negotiation propose) is a phase-1 interrupt, not a flow end. The
  idle backstop (`A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT`, default 30 s, reset per received event) logs
  a `webhook-idle-timeout` line and ends the flow normally with what was collected.
  `A2AT_SAMPLE_MAX_ARTIFACTS` does not apply in webhook mode; each round's single RPC reply is
  logged once as `webhook-rpc-reply` and not counted again, and the `stream-completed` summary
  counts the webhook-received events and appends `transport=webhook`.

### Configuration (a2a-t-sample/.env)

| Key | Meaning |
| --- | --- |
| `A2AT_LANGUAGE` | `zh-CN` / `en-US`; selects the natural-language input and the mock/template tree |
| `A2AT_SAMPLE_SCENARIO` | `default` / `negotiation`; selects the sample scenario variant (same semantics as `--scenario`) |
| `A2AT_SAMPLE_NEGOTIATION_KIND` | `information` / `target` / `feasibility` (default `information`); negotiation scenario input, same semantics as `--negotiation` |
| `A2AT_SAMPLE_CHANNEL` | `data` / `text` (default `data`); generation channel of the negotiation propose/accept messages, same semantics as `--channel` |
| `A2AT_SAMPLE_TRANSPORT` | `sse` / `webhook` (default `sse`); client event transport, same semantics as `--transport` |
| `A2AT_SAMPLE_WEBHOOK_PORT` | Bind port of the embedded webhook receiver (default `9000`; `0` = OS-assigned ephemeral port) |
| `A2AT_SAMPLE_WEBHOOK_TOKEN` | Push token the server must carry in the `X-A2A-Notification-Token` header (default `demo-webhook-token`); a mismatch answers `401` |
| `A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT` | Idle backstop in seconds that ends the webhook flow when no push arrives (default `30`) |
| `A2AT_LLM_API_KEY` | Empty ⇒ mock LLM; otherwise an OpenAI-compatible key |
| `A2AT_LLM_PROVIDER` / `A2AT_LLM_MODEL` / `A2AT_LLM_BASE_URL` | LLM endpoint settings |
| `A2AT_LLM_TIMEOUT_SECONDS` | HTTP client timeout in seconds (default 60) |
| `A2AT_SAMPLE_HOST` / `A2AT_SAMPLE_PORT` | Server bind for the manual `python -m` path (default `127.0.0.1:8000`) |
| `REGISTRY_CENTER_HOST` / `REGISTRY_CENTER_PORT` | Registry bind for the manual `python -m` path (default `127.0.0.1:5001`) |
| `A2AT_SAMPLE_DEBUG` | `true` prints full LLM request/response payloads |
| `A2AT_SAMPLE_MAX_ARTIFACTS` (process env, not `.env`) | Stop after N artifacts (default 0 = no limit) |

Notes: `run_demo.py` always uses `5001` / `8000` for its readiness checks and ignores
`A2AT_SAMPLE_PORT` / `REGISTRY_CENTER_PORT` — set those only for the manual `python -m` path.
Server-side progress and report text is fixed English regardless of `--language`; the flag only
switches the client input, templates, and mock replies, while the negotiation message texts
follow the run language. The template, parameter schema, AgentCard, and natural-language input
are sample-specific; the template must belong to the `Task-T` extension and use version `v1`.
Remote prompt-resource loading is not part of the SDK, and the registry center here is a
sample-side mock for AgentCard discovery, not an SDK capability.

### Run the two sides separately

Point `PYTHONPATH` at the case `src/` and start each part in its own terminal. `.env` is read from
the current directory, and a missing `.env` is a **hard error** for the client and server. Start
the registry and the server first — the client discovers the server's AgentCard from the registry.

```bash
# bash / zsh
cd a2a-t-sample
export PYTHONPATH="$PWD/ran-energy-saving/src"
uv run --no-sync python -m registry.registry_main    # terminal 1: registry center (5001)
uv run --no-sync python -m server.server_main         # terminal 2: server (8000)
uv run --no-sync python -m client.client_main         # terminal 3: client
```

```powershell
# PowerShell
cd a2a-t-sample
$env:PYTHONPATH = "$pwd\ran-energy-saving\src"
uv run --no-sync python -m registry.registry_main
uv run --no-sync python -m server.server_main
uv run --no-sync python -m client.client_main
```

The run language comes from `A2AT_LANGUAGE` in `a2a-t-sample/.env` (`env.example` ships `zh-CN`);
`run_demo.py` overrides it per run via `--language` (default `en-US`).

### Mock LLM mechanism

With an empty `A2AT_LLM_API_KEY` the sample installs the bundled mock LLM
([`src/common/mock_llm.py`](src/common/mock_llm.py)): it routes every structured call by the
**output schema signature** (`route_structured_call`) and replays the scripted slot-extraction and
content-validation responses of [`resources/mock_llm/{zh-CN,en-US}/`](resources/mock_llm).

### Supported flags (`run_demo.py`)

| Flag | Effect |
| --- | --- |
| `--language {en-US,zh-CN}` | Run language (default `en-US`) |
| `--scenario {default,negotiation}` | Scenario variant (default `default`; `negotiation` runs the missing-region information negotiation) |
| `--negotiation {information,target,feasibility}` | Negotiation kind selecting the client scenario input (default `information` = missing region; `target` = 100Mbps throughput beyond cell capability; `feasibility` = 30% saving goal conflicting with a 50Mbps throughput target). `information` keeps the `--scenario` switch (only `--scenario negotiation` runs the missing-region flow); `target`/`feasibility` engage without it. The flag only selects the input — the server stays data-driven on the validation error code |
| `--channel {fromData,fromText}` | Generation channel of the negotiation propose/accept messages (default `fromData`; `fromText` adds one extraction LLM call per generated message) |
| `--transport {sse,webhook}` | Event transport of the client (default `sse`) |
| `--webhook-port N` | Bind port of the embedded webhook receiver used with `--transport webhook` (default 9000; `0` selects an OS-assigned ephemeral port) |
| `--keep-alive` | Leave registry/server running; stop later with `--stop` |
| `--keep-logs` | Keep the temp run directory (logs / `.env`) under `<temp>/a2at-ran-energy-saving/` |
| `--stop` | Stop a previous `--keep-alive` run and exit |
| `--timeout N` | Seconds to wait for each port (default 30) |
