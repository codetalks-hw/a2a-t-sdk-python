# RAN Energy Saving Sample

An A2A-T sample with a client agent, a RAN domain server agent, and a mock registry center for
AgentCard discovery.

## 1. What this sample demonstrates

A client agent commissions a RAN domain agent to run an energy-saving task over A2A. On this one
scenario the sample covers the two A2A-T capabilities a customer integrates, in order:

```text
Task-T          natural language ──> task prompt ──> validated ──> executed ──> reported

Negotiation-T   when the request is not executable as-is
                (information missing / target unclear / feasibility uncertain)
                  ──> the two agents negotiate ──> agreed ──> the task then runs as Task-T
```

Read it one of three ways:

- **"Just run it"** — [§2 Quickstart](#2-quickstart).
- **"I integrate the one-shot task"** — [§3 Task-T interaction](#3-using-the-a2a-t-sdk-to-implement-task-t-interaction)
  plus the [replace checklist](#5-replace-checklist-demo--production).
- **"I integrate negotiation"** — [§4 Negotiation-T interaction](#4-using-the-a2a-t-sdk-to-implement-negotiation-t-interaction).

### 1.1 Project layout

| Path | Core responsibility |
| --- | --- |
| [`src/client/client_main.py`](src/client/client_main.py) | Assembly root: reads `.env`, builds the runtime (`build_client_runtime`), discovers the server's AgentCard from the registry, starts/stops the webhook receiver, and assembles the demo `ScriptedNegotiationResponseProvider`. The client-side `INTEGRATION POINT` comment sits in `main()`. |
| [`src/client/client_flow.py`](src/client/client_flow.py) | Orchestration: the message sequence (generate → send → consume), the wire mechanics (metadata keys, `A2A-Extensions` header), the event pipeline, and the two transport drivers (SSE streaming / webhook queue). It calls the negotiation decision integration point but owns no decision. |
| [`src/client/negotiation_response.py`](src/client/negotiation_response.py) | The negotiation decision integration point: `NegotiationProposeView`, `NegotiationDecision`, the `NegotiationResponseProvider` Protocol, and the demo `ScriptedNegotiationResponseProvider`. |
| [`src/client/scenario_data.py`](src/client/scenario_data.py) | The sample's inputs and scripted answers — it feeds **both** sides (see the [replace checklist](#5-replace-checklist-demo--production)); not removable as-is. |
| [`src/server/server_main.py`](src/server/server_main.py) | a2a-sdk app assembly, with the push-notification machinery assembled unconditionally. The server-side `INTEGRATION POINT` comment sits in `build_server_runtime`. |
| [`src/server/server_flow.py`](src/server/server_flow.py) | The server flow: validate → negotiate-or-execute → stream progress and the report. |
| [`src/server/task_execution.py`](src/server/task_execution.py) | The `TaskExecutionService` execution integration point plus the demo UC1 service. |
| [`src/common/`](src/common) | Shared adapters, the schema-routed mock LLM, the `WebhookReceiver`, and the shared `TASK_PARAM_SCHEMA`. |
| [`src/registry/`](src/registry) | Mock registry center (AgentCard register/query, port 5001). |
| [`resources/mock_llm/`](resources/mock_llm) | Scripted slot-extraction and content-validation replies. |
| [`run_demo.py`](run_demo.py) | Cross-platform, non-blocking launcher. |

## 2. Quickstart

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

Add `--scenario negotiation` (or `--negotiation target` / `--negotiation feasibility`) to run a
negotiation instead. If your client cannot hold a stream open, add `--transport webhook` — see
[§6.1](#61-webhook-transport).

## 3. Using the A2A-T SDK to implement Task-T interaction

### 3.1 The four steps

1. **Generate** — the client turns its natural-language input into a Task-T task request (a
   rendered prompt) with the A2A-T client SDK's `generate_task_prompt_from_text`.
2. **Send** — the prompt travels to the server in the message `metadata`.
3. **Validate** — the server reads the prompt from the metadata and calls the A2A-T server SDK's
   `validate_task_prompt_and_data_filling`, which validates it and extracts the parameters
   (`operationType`, `region`, `energySavingGoal`, `guaranteedThroughputTarget` — the caller schema
   `TASK_PARAM_SCHEMA` shared by both sides in [`src/common/task_schema.py`](src/common/task_schema.py)).
4. **Execute and report** — the extracted parameters feed the business execution; progress comes
   back as status updates and the result as a report artifact, which the client presents.

### 3.2 Client side

The whole exchange is ([`client_flow.py`](src/client/client_flow.py)):

```python
prompt_content = prompt_client.generate_task_prompt_from_text(input_text, ENERGY_SAVING_TEMPLATE_URI)
prompt_text, extension_uri = _require_prompt_text(prompt_content)
request = SendMessageRequest()
...  # message_id / role omitted
request.message.parts.add().text = _build_request_metadata(initial_input)
request.message.metadata[extension_uri] = prompt_text   # the prompt travels in the metadata
request.message.metadata["templateUri"] = getattr(prompt_content, "template_uri", None) or ENERGY_SAVING_TEMPLATE_URI
call_context = ClientCallContext(service_parameters={"A2A-Extensions": extension_uri})
async for stream_response in a2a_client.send_message(request, context=call_context):
    ...  # consume the status updates and the report artifact
```

### 3.3 Server side

`_extract_prompt_text` reads the metadata and `validate_task_prompt_and_data_filling` validates and
extracts ([`server_flow.py:82`](src/server/server_flow.py#L82), [`server_flow.py:486`](src/server/server_flow.py#L486)).
A rejected prompt is mapped to `TASK_STATE_REJECTED` instead of crashing the server; the validated
parameters go into the execution integration point.

### 3.4 What you replace

The template (`ENERGY_SAVING_TEMPLATE_URI` = `Task-T/network-layer/ran-energy-saving/v1`) and the
parameter schema are sample-specific — bring your own
([`scenario_data.py`](src/client/scenario_data.py)); the business execution runs behind the
server-side `TaskExecutionService` integration point (see the
[replace checklist](#5-replace-checklist-demo--production)).

## 4. Using the A2A-T SDK to implement Negotiation-T interaction

The boundary in one sentence: the SDK owns the wire — rendering, validation, the negotiation
session map, the transport mechanics — your system owns the business: when a request cannot be
carried out as-is and a conversation would settle it, and how your side answers a propose.

### 4.1 When a negotiation happens

A negotiation opens when the server cannot carry out the request as it stands, but the gap is
something the two agents can settle by talking rather than by refusing to act. The demo covers
three such situations:

**Information — the request is incomplete.** The client asks to run energy saving and names a goal,
a time window and a throughput target, but never says which region. The server cannot start without
it, so it says which piece is missing; the client supplies it and the task proceeds.

**Target — the request is internally inconsistent.** The client sends a segmented guaranteed
throughput target in which two time windows overlap: `{17:30~23:00,20Mbps}` and
`{20:00~00:00,10Mbps}`. The request is well-formed, but the guaranteed rate for the overlapping
period cannot be determined. The server restates the intent it understood and asks which time
window should be adjusted; the client answers with the corrected segments.

**Feasibility — the request may be impossible under current conditions.** The client asks two
stations to hold `{00:00~06:00,2Mbps}, {06:00~18:00,10Mbps}` through an outage that lasts from
00:00 to 18:00, while an existing constraint requires at least 18 hours of power autonomy. Holding
that rate would drain the batteries faster than the outage lasts. The server reports the
infeasibility together with the evidence and a proposal (lower the 06:00~18:00 rate from 10 Mbps to
2 Mbps); the client accepts the adjusted target.

### 4.2 How the demo decides which one applies

Which situation applies — and whether to negotiate at all — is **your business decision; the SDK
has no opinion**. In this demo it is data-driven: the first content-validation error's code selects
the kind, via the `_NEGOTIATION_TYPE_BY_ERROR_CODE` table
([`server_flow.py:64`](src/server/server_flow.py#L64)). That table is a **sample convention, not an
SDK contract** — edit your copy freely (remap codes, add your own, or drop the mapping and decide
from your own business rules; an unmapped failure is simply rejected).

The wire does not depend on that table. Each propose records its `templateUri` — the prompt
template its message text was rendered from, such as
`Negotiation-T/information-negotiation/propose/v1`. That URI also tells the client which kind of
propose it received, so the client picks its answer — accept, reject or abort — from the message it
got, not from any error code.

### 4.3 Shared mechanics

These apply to all three kinds; the per-kind sections below do not repeat them.

**A round is one propose plus the answer to it.** The session context carries a 1-based `round`;
the answer keeps the propose's round, and only a *new* propose advances it. The client's opening
request is not a negotiation round — the negotiation starts with the server's propose.
`maxRounds` (default 5) bounds the session.

**The closed loop, in four messages:**

1. client → server: the energy-saving request as it stands — in the demo, missing the region.
2. server → client: the propose — what is missing, what needs clarifying, or what needs evaluating.
3. client → server: the request re-sent with the missing or corrected content filled in; the
   answer travels in that same message (it is one message, not two).
4. server → client: the result — progress, then the report.

The session (`id` / `round` / `maxRounds` / performative) travels with every negotiation message in
the `negotiationContext` metadata map, so the SDK itself stays stateless.

**Where your decision plugs in.** When message 2 arrives, the client flow builds a lightweight
`NegotiationProposeView` (kind, `templateUri`, rendered text) and calls the provider
([`negotiation_response.py`](src/client/negotiation_response.py)):

```python
@runtime_checkable
class NegotiationResponseProvider(Protocol):
    def respond(
        self, propose: NegotiationProposeView, session: NegotiationContext
    ) -> NegotiationDecision: ...
```

`NegotiationDecision` carries a `conclusion` (`accept` / `reject` / `abort`) plus the per-kind
accept field: `filled_params` (information, name → value), `confirmed_intent` (target),
`feasibility_summary` (feasibility), and `reason` (reject/abort). **`accept` is wired
end-to-end**: from your decision the flow regenerates the Task-T prompt, builds the typed accept
with the same round stamped as `ACCEPT`, and sends it over the configured transport. `respond` is
synchronous — an implementation that needs asynchrony (human confirmation, an upstream call) owns
that internally.

> The provider replaces only the scripted **answer** to a propose — the Task-T refill parameters
> (`negotiation_params`) stay caller input to the flow, which will not engage a negotiation without them.

### 4.4 Information negotiation

**① When the server opens it.** The request is incomplete — it lacks a parameter the server needs
(in the demo, the region). The demo's trigger is a `content.param_missing` validation error, but
that code is the sample's wiring, not part of the protocol.

**② What the reference code does.**

| Step | SDK call | Reference code |
| --- | --- | --- |
| Server propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, InformationProposeContent(items, relationship)), INFORMATION_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| Client accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, InformationEndingContent(ACCEPT, items)), INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607), [`client_flow.py:659`](src/client/client_flow.py#L659) |
| Answer source | `NegotiationDecision(conclusion="accept", filled_params={...})` | [`negotiation_response.py`](src/client/negotiation_response.py) |

The propose's `items` are the missing parameters; the accept's `items` are the filled values.

**③ What you can copy as-is.** The prompt rendering, the `templateUri`/`negotiationContext`
metadata assembly, the session handling, and the transport — everything in `client_flow.py` except
the decision itself.

**④ What you must write yourself.** The propose's business phrasing (which parameters to ask for
and how to name them — `server_flow._missing_items_from_errors` plus your slot schema) and the
answer source behind the provider integration point:

```python
class MyProvider:
    def respond(self, propose: NegotiationProposeView, session: NegotiationContext) -> NegotiationDecision:
        values = my_system.look_up(propose.kind, propose.prompt_text)   # your lookup
        return NegotiationDecision(conclusion="accept", filled_params=values)
```

### 4.5 Target negotiation

**① When the server opens it.** The request contradicts itself: two time windows of the segmented
guaranteed throughput target overlap, so the guaranteed rate for the overlapping period is
ambiguous. The demo's trigger is a `content.value_not_allowed` validation error.

**② What the reference code does.**

| Step | SDK call | Reference code |
| --- | --- | --- |
| Server propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, TargetProposeContent(description, intent_understanding=[...], request_for_clarification=[...])), TARGET_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| Client accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, TargetEndingContent(ACCEPT, confirmed_intent, failure_reason=None)), TARGET_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607) |
| Answer source | `NegotiationDecision(conclusion="accept", confirmed_intent="...")` | [`negotiation_response.py`](src/client/negotiation_response.py) |

The demo proposes the *clarification* form: the server restates the intent it understood
(`intent_understanding`) and asks for the time adjustment (`request_for_clarification`). The accept
carries the corrected target as `confirmed_intent`.

**③ What you can copy as-is.** The same as information: rendering, metadata assembly, session
handling, transport.

**④ What you must write yourself.** The clarification wording for the propose
(`_negotiation_propose_data`'s target branch) and how your side derives `confirmed_intent` — the
demo reads it from `scenario_data`; yours comes from whoever resolves the ambiguity.

> `TargetProposeContent` also supports the confirm-request form (`target_confirm_request`, with the
> three conditional lists empty — the mutual-exclusion rule the SDK enforces) and
> `alignment_and_clarification` for later rounds. The demo uses the first-round clarification form.

### 4.6 Feasibility negotiation

**① When the server opens it.** The request may be impossible under current conditions: the
guaranteed rate cannot sustain the declared outage duration for the listed stations. The demo's
trigger is a `content.semantic_conflict` validation error.

**② What the reference code does.**

| Step | SDK call | Reference code |
| --- | --- | --- |
| Server propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, FeasibilityProposeContent(description, action=PROPOSE_ALTERNATIVE_ON_FAILURE, infeasibility_details_and_proposal=[...])), FEASIBILITY_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| Client accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, FeasibilityEndingContent(ACCEPT, feasibility_summary)), FEASIBILITY_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607) |
| Answer source | `NegotiationDecision(conclusion="accept", feasibility_summary="...")` | [`negotiation_response.py`](src/client/negotiation_response.py) |

The demo reports the completed assessment as infeasible and attaches the evidence plus an
alternative proposal (`action=PROPOSE_ALTERNATIVE_ON_FAILURE` with
`infeasibility_details_and_proposal`); the accept states the adjustment it agrees to.

**③ What you can copy as-is.** The same as information: rendering, metadata assembly, session
handling, transport.

**④ What you must write yourself.** The assessment itself — the infeasibility findings and the
proposal — and the answer: both must come from your planning, simulation or power-capacity system.

> `FeasibilityProposeContent` also supports `REQUEST_FEASIBILITY_EVALUATION` (asking the client to
> evaluate `contents_to_evaluate`) and the `feasibility_confirm_request` form. The demo uses the
> infeasibility-and-proposal form.

### 4.7 Reject and abort — the extension point

A `reject`/`abort` decision is **not wired in this demo**: the flow logs
`negotiation-decision: {conclusion} (not wired in this demo...)` and ends without sending. The
extension point is the reject/abort branch of `_plan_negotiation_accept` in
[`client_flow.py:752`](src/client/client_flow.py#L752): extend it to mirror
`_run_negotiation_accept` ([`client_flow.py:870`](src/client/client_flow.py#L870)) — advance the
session with `session.next_round().with_performative(NegotiationPerformative.REJECT)`, build the
proposed kind's ending content with conclusion `Reject` (the decision's `reason` becomes the
ending content's `failure_reason`), and generate the message with the SDK's
`generate_negotiation_reject_prompt_from_data` / `..._from_text`. Abort is kind-independent:
`NegotiationAbortContent(termination_reason)` renders against the common
`Negotiation-T/common/abort/v1` template via `generate_negotiation_abort_prompt_from_data`.

### 4.8 Generating messages from structured data or from natural language

There are two ways to produce a negotiation message, and both end up on the wire in the same form:

- **from structured data (default).** You build the typed content in code and the SDK renders the
  template directly — deterministic, no model call.
- **from natural language** (`--channel fromText`). You write the message as plain text and the SDK
  asks the model to extract the structured content first, then renders the template — one extra
  extraction call per generated message.

## 5. Replace checklist (demo → production)

| Demo piece | What it does in the demo | Keep or replace |
| --- | --- | --- |
| mock LLM — [`resources/mock_llm/`](resources/mock_llm) + the schema-routed mock in `src/common/` | scripted slot-extraction and content-validation replies, selected when `A2AT_LLM_API_KEY` is empty | **KEEP** for CI/offline runs; in production remove it and configure a real OpenAI-compatible endpoint |
| scripted execution data — [`src/server/constants_data.py`](src/server/constants_data.py) | the UC1 progress steps and intent report the demo execution streams | **REPLACE** via the `TaskExecutionService` integration point ([`src/server/task_execution.py`](src/server/task_execution.py)); once your service is assembled, the demo service and data can be deleted |
| scripted answers — [`src/client/scenario_data.py`](src/client/scenario_data.py) | feeds BOTH sides (see below) | **REPLACE** on both sides: the client answers via your `NegotiationResponseProvider`; the server's propose texts in your copy of `server_flow._negotiation_propose_data` become your business phrasing |
| mock registry — [`src/registry/`](src/registry) (port 5001) | AgentCard register/query for discovery | **REPLACE** with your registry center, or bypass discovery and construct the `a2a-sdk` client from a known AgentCard |
| static webhook token — `A2AT_SAMPLE_WEBHOOK_TOKEN` (default `demo-webhook-token`) | shared static token in the `X-A2A-Notification-Token` header | **REPLACE** with production auth (per-pair secrets, rotation, TLS); treat the receiver as an exposed HTTP endpoint |

> **`scenario_data` feeds BOTH sides** — the client's scripted answers (filled parameters,
> confirmed intent, feasibility summary) go through the provider integration point, and the server's propose
> content is built from the same module: `_negotiation_propose_data`
> ([`server_flow.py:207`](src/server/server_flow.py#L207)) fills the target/feasibility propose
> texts from its accessors. Replacing only the provider and the execution service still leaves
> the server proposing the demo's canned texts — the module is not removable.

## 6. Appendix

### 6.1 Webhook transport

Both transports deliver the same task events; the difference is how the client receives them.
SSE streaming is the default: the client holds the streaming `message:send` open and consumes the
events as they arrive. Webhook push notifications are for clients that do not hold a streaming
connection open (long-running tasks, intermittently connected clients): `--transport webhook` (or
`A2AT_SAMPLE_TRANSPORT=webhook`).

What changes under webhook: the client sends non-blocking (`return_immediately`) and every task
event arrives as a webhook POST on a receiver embedded in the client process
(`WebhookReceiver` in [`src/common/webhook_receiver.py`](src/common/webhook_receiver.py)), which
binds one extra listening port (`--webhook-port`, default 9000). The server's pushes carry
`A2AT_SAMPLE_WEBHOOK_TOKEN` (default `demo-webhook-token`) in the `X-A2A-Notification-Token`
header — a mismatch answers `401` and the event is dropped. The flow ends on any SDK terminal
state, with a 30 s idle backstop. The negotiation round trip is statement-identical on both
transports.

- **Two registration paths.** The initial request registers the push config inline
  (`SendMessageConfiguration.task_push_notification_config`, a flat
  `TaskPushNotificationConfig(url, token)` — the task id is still unknown at this point); before
  sending the accept the flow re-registers the same URL/token through the
  `POST /tasks/{id}/pushNotificationConfigs` REST endpoint (a2a-sdk
  `BaseClient.create_task_push_notification_config`).
- **No server-side mode switch.** The server assembles the push machinery unconditionally
  (a2a-sdk `DefaultRequestHandler` with `InMemoryPushNotificationConfigStore` +
  `BasePushNotificationSender`, [`server_main.py`](src/server/server_main.py)); in SSE mode it
  simply idles.
- **Non-blocking path.** The a2a client is built with `ClientConfig(streaming=False)`, required to
  select the non-blocking `message:send` path (the `return_immediately` flag alone does not switch
  it). Push bodies are StreamResponse-shaped JSON — the same shape as the SSE events — so
  negotiation detection, artifact counting, and presentation are shared with SSE.
- **Token auth.** The receiver validates `X-A2A-Notification-Token` on every POST: a mismatch
  answers `401` (the event is dropped), a malformed or unrecognized body answers `400`. The demo's
  static shared token is demo-grade — for production use per-client secrets with rotation over TLS.
- **End signals.** Any SDK terminal state (`TASK_STATE_COMPLETED` / `FAILED` / `CANCELED` /
  `REJECTED`) ends the flow in both phases — negotiation scenarios normally end on `COMPLETED`, a
  failed validation or stream error on `REJECTED` / `FAILED`; the summary notes a terminal end as
  `end=<state>`. The default scenario still ends on the report artifact; `INPUT_REQUIRED` (a
  negotiation propose) is an initial-phase interrupt, not a flow end. The idle backstop
  (`A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT`, default 30 s, reset per received event) logs
  `webhook-idle-timeout` and ends the flow with what was collected. `A2AT_SAMPLE_MAX_ARTIFACTS`
  does not apply in webhook mode; each phase's single RPC reply is logged once as
  `webhook-rpc-reply` and not counted again, and the `stream-completed` summary counts the
  webhook-received events and appends `transport=webhook`.

### 6.2 Configuration (a2a-t-sample/.env)

| Key | Meaning |
| --- | --- |
| `A2AT_LANGUAGE` | `zh-CN` / `en-US`; selects the natural-language input and the mock/template tree |
| `A2AT_SAMPLE_SCENARIO` | `default` / `negotiation`; selects the sample scenario variant (same semantics as `--scenario`) |
| `A2AT_SAMPLE_NEGOTIATION_KIND` | `information` / `target` / `feasibility` (default `information`); negotiation scenario input, same semantics as `--negotiation` |
| `A2AT_SAMPLE_CHANNEL` | `data` / `text` (default `data`); whether negotiation messages are generated from structured data or from natural language, same semantics as `--channel` |
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

### 6.3 Run the two sides separately

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

### 6.4 Mock LLM mechanism

With an empty `A2AT_LLM_API_KEY` the sample installs the bundled mock LLM
([`src/common/mock_llm.py`](src/common/mock_llm.py)): it routes every structured call by the
**output schema signature** (`route_structured_call`) and replays the scripted slot-extraction and
content-validation responses of [`resources/mock_llm/{zh-CN,en-US}/`](resources/mock_llm).

### 6.5 Supported flags (`run_demo.py`)

| Flag | Effect |
| --- | --- |
| `--language {en-US,zh-CN}` | Run language (default `en-US`) |
| `--scenario {default,negotiation}` | Scenario variant (default `default`; `negotiation` runs the missing-region information negotiation) |
| `--negotiation {information,target,feasibility}` | Negotiation kind selecting the client scenario input (default `information` = missing region; `target` = two overlapping time segments in the guaranteed throughput target; `feasibility` = the guaranteed rate cannot sustain an 18-hour power outage). `information` keeps the `--scenario` switch (only `--scenario negotiation` runs the missing-region flow); `target`/`feasibility` engage without it. The flag only selects the input — the server stays data-driven on the validation error code |
| `--channel {fromData,fromText}` | Whether negotiation messages are generated from structured data (`fromData`, default) or from natural language (`fromText`, one extra extraction model call per message) |
| `--transport {sse,webhook}` | Event transport of the client (default `sse`); see [§6.1](#61-webhook-transport) |
| `--webhook-port N` | Bind port of the embedded webhook receiver used with `--transport webhook` (default 9000; `0` selects an OS-assigned ephemeral port) |
| `--keep-alive` | Leave registry/server running; stop later with `--stop` |
| `--keep-logs` | Keep the temp run directory (logs / `.env`) under `<temp>/a2at-ran-energy-saving/` |
| `--stop` | Stop a previous `--keep-alive` run and exit |
| `--timeout N` | Seconds to wait for each port (default 30) |
