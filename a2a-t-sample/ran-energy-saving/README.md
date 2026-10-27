# RAN energy saving: OSS and RAN NMS negotiate, then execute (A2A-T solution)

> **In one sentence:** the OSS asks for an energy-saving goal; the RAN network management system
> negotiates to fill or correct it, then evaluates, applies and reports — automatically, with a
> full audit trail.

```mermaid
flowchart LR
    OSS["OSS/BSS<br/>client side<br/>requests energy saving"] -->|"A2A-T pre-task negotiation"| NMS["RAN network management<br/>validate · negotiate · evaluate · apply"]
    NMS -->|"plan applied"| CELL["Cells / base stations"]
    NMS -->|"task status + report"| OSS
```

## 1. System-to-system interaction

The OSS and the RAN NMS do two things: **agree on the goal, then execute the plan**.

| System | Role | Responsibility |
| --- | --- | --- |
| OSS/BSS | client side | requests the energy-saving goal and decides accept / reject / counter |
| RAN NMS | execution side | validates the goal, negotiates, evaluates and applies the plan, writes the report |

```mermaid
sequenceDiagram
    participant O as OSS/BSS
    participant N as RAN NMS
    O->>N: energy-saving goal (for example "save energy while keeping the guaranteed rate")
    N->>O: proposal: add a value / fix a conflict / offer an alternative
    O->>N: accept / reject / abort
    N->>O: task status + energy-saving report
```

Negotiation may take two or three rounds, but **the task is created only after both sides agree**,
so a wrong goal is never applied.

Typical problems map to three kinds of negotiation:

- missing parameters;
- overlapping time segments;
- infeasible conditions (for example a guaranteed rate during an outage).

## 2. Typical scenarios

### 2.1 Missing value: the NMS asks, the OSS fills it

```mermaid
sequenceDiagram
    participant O as OSS
    participant N as RAN NMS
    O->>N: energy-saving goal (no time-segmented guaranteed rate)
    N->>O: please provide: the time-segmented guaranteed rate
    O->>N: accept and fill the value
    N->>O: evaluate → apply → report
```

**Outcome:** the task completes; no manual ticket is needed.

### 2.2 Overlapping segments: correct before executing

```mermaid
sequenceDiagram
    participant O as OSS
    participant N as RAN NMS
    O->>N: energy-saving goal (two overlapping windows)
    N->>O: proposal: provide non-overlapping segments
    O->>N: counter-proposal: corrected segments
    N->>O: confirm the revised plan
    O->>N: accept
    N->>O: evaluate → apply → report
```

**Outcome:** the conflict is fixed before any task exists, so nothing wrong is applied.

### 2.3 Infeasible condition: the NMS proposes an alternative

```mermaid
sequenceDiagram
    participant O as OSS
    participant N as RAN NMS
    O->>N: energy-saving goal (guaranteed rate during an outage)
    N->>O: proposal: use a feasible alternative
    O->>N: accept
    N->>O: evaluate → apply → report
```

**Outcome:** the infeasible plan is never applied; the alternative is executable.

The other two outcomes are simple conclusions:

| Scenario | Outcome |
| --- | --- |
| the OSS cannot provide a required value | negotiation ends; no task is created |
| counter-proposals never converge | the NMS aborts at the round limit; no task is created |

## 3. Customer value

| Pain | Without pre-task negotiation | With this solution |
| --- | --- | --- |
| Missing parameters | the task fails, or someone asks by ticket | the NMS asks; the OSS fills the value automatically |
| Conflicting targets | a wrong plan may be applied | the conflict is fixed before the task exists |
| Infeasible conditions | the apply fails and has to be rolled back | an alternative is proposed; nothing runs until accepted |
| Audit | emails and tickets | every proposal, decision and apply is recorded |

## 4. Demo

```bash
# CLI (offline, deterministic, no key)
python run_demo.py --scenario information --mode auto --model-mode offline
# Web UI
python run_demo.py --ui --scenario information --mode auto --model-mode offline
# Optional: expose the OSS side as an A2A backend
python run_demo.py --ui --gateway --ui-port 8080 --gateway-port 8001 --model-mode offline
```

With `--gateway`, the OSS side is itself an A2A agent: an external caller such as Gemini Enterprise
sends one request and receives progress notifications (negotiation and execution milestones) before
the final energy-saving report, either on a stream or through a registered webhook.

The page shows the run as a timeline: dispatch → task generation → validation → negotiation →
resolution → NMS execution → report. Every row is labelled **OSS** or **NMS**, and the inspector
shows the message or decision behind it. Installation, environment variables and the full command
reference are in the [technical guide](TECHNICAL.md).

## 5. Integration boundary

| OSS side | NMS side |
| --- | --- |
| Integrate the A2A-T client; the SDK carries the protocol | Keep the existing NMS evaluation, apply and reporting capability |
| Provide a decision agent: accept / reject / counter | Replace the sample tools with real NMS interfaces |
| Keep your own policy, approval and audit | Keep the negotiation policy and task store |

In the reference implementation the OSS side uses an ADK decision agent and the NMS side uses
LangGraph; those are implementation choices and do not change the interaction above.

## 6. Key characteristics

| Characteristic | What it means |
| --- | --- |
| Auditable | every proposal, decision, apply and report is recorded |
| Model-independent | offline runs are reproducible; a live model only affects decisions and planning |
| Replaceable | tools and policy can be replaced with the real NMS implementation |

## 7. Technical guide

Protocol details, implementation structure, configuration, tests and production notes are in the
[technical guide](TECHNICAL.md).
