# RAN 节能样例

一个 A2A-T 样例：包含一个客户端 Agent、一个 RAN 域服务端 Agent，以及用于发现 AgentCard 的 mock
注册中心。

**读者定位。** 本文档面向实现 **client agent** 的团队；**server agent** 是可运行的参考对端 ——
生产中两端通常归属不同厂商，因此下文统一按角色说明各自负责什么，不使用含混的第二人称。

## 目录

- [1. 本样例演示了什么](#1-本样例演示了什么)
  - [1.1 目录结构](#11-目录结构)
- [2. 快速开始](#2-快速开始)
- [3. 使用 A2A-T SDK 实现 Task-T 交互](#3-使用-a2a-t-sdk-实现-task-t-交互)
  - [3.1 四个步骤](#31-四个步骤)
  - [3.2 客户端侧](#32-客户端侧)
  - [3.3 服务端侧](#33-服务端侧)
  - [3.4 client 侧要替换什么](#34-client-侧要替换什么)
- [4. 使用 A2A-T SDK 实现 Negotiation-T 交互](#4-使用-a2a-t-sdk-实现-negotiation-t-交互)
  - [4.1 什么时候会发生协商](#41-什么时候会发生协商)
  - [4.2 样例如何判断属于哪一种](#42-样例如何判断属于哪一种)
  - [4.3 共享机制](#43-共享机制)
  - [4.4 信息协商（information）](#44-信息协商information)
  - [4.5 目标协商（target）](#45-目标协商target)
  - [4.6 可行性协商（feasibility）](#46-可行性协商feasibility)
  - [4.7 reject 与 abort —— 扩展点](#47-reject-与-abort--扩展点)
  - [4.8 从结构化数据生成，还是从自然语言生成](#48-从结构化数据生成还是从自然语言生成)
- [5. 替换清单（demo 到生产）](#5-替换清单demo-到生产)
- [6. 附录](#6-附录)
  - [6.1 Webhook 传输](#61-webhook-传输)
  - [6.2 配置（a2a-t-sample/.env）](#62-配置a2a-t-sampleenv)
  - [6.3 分别启动两端](#63-分别启动两端)
  - [6.4 mock LLM 机制](#64-mock-llm-机制)
  - [6.5 支持的参数（`run_demo.py`）](#65-支持的参数run_demopy)

## 1. 本样例演示了什么

一个客户端 Agent 通过 A2A 委托一个 RAN 域 Agent 执行节能任务。在这一个场景上，样例按顺序覆盖客户
需要集成的两项 A2A-T 能力：

```text
Task-T          自然语言 ──> 任务提示词 ──> 校验 ──> 执行 ──> 报告

Negotiation-T   请求不能直接执行时（信息缺失 / 目标不清晰 / 可行性不确定）
                  ──> 双方 Agent 协商 ──> 达成一致 ──> 任务随后按 Task-T 执行
```

按三种阅读路径之一阅读：

- **"先跑起来"** —— [§2 快速开始](#2-快速开始)。
- **"我要集成一次性任务"** —— [§3 Task-T 交互](#3-使用-a2a-t-sdk-实现-task-t-交互)加上
  [替换清单](#5-替换清单demo-到生产)。
- **"我要集成协商"** —— [§4 Negotiation-T 交互](#4-使用-a2a-t-sdk-实现-negotiation-t-交互)。

### 1.1 目录结构

| 路径 | 核心职责 |
| --- | --- |
| [`src/client/client_main.py`](src/client/client_main.py) | 装配根：读取 `.env`、构建运行时（`build_client_runtime`）、从注册中心发现服务端 AgentCard、启动/停止 webhook 接收器、装配 demo 的 `ScriptedNegotiationResponseProvider`。客户端侧 `INTEGRATION POINT` 标注位于 `main()`。 |
| [`src/client/client_flow.py`](src/client/client_flow.py) | 编排：消息序列（生成 → 发送 → 消费）、报文机制（metadata 键、`A2A-Extensions` 头）、事件管线，以及两种传输驱动（SSE 流式 / webhook 队列）。它调用协商决策接入点，但不持有任何决策。 |
| [`src/client/negotiation_response.py`](src/client/negotiation_response.py) | 协商决策接入点：`NegotiationProposeView`、`NegotiationDecision`、`NegotiationResponseProvider` Protocol，以及 demo 实现 `ScriptedNegotiationResponseProvider`。 |
| [`src/client/scenario_data.py`](src/client/scenario_data.py) | 样例输入与脚本化应答 —— 它**同时喂两侧**（见[替换清单](#5-替换清单demo-到生产)），不能原样删除。 |
| [`src/server/server_main.py`](src/server/server_main.py) | a2a-sdk 应用装配，推送通知机制无条件装配。服务端侧 `INTEGRATION POINT` 标注位于 `build_server_runtime`。 |
| [`src/server/server_flow.py`](src/server/server_flow.py) | 服务端流程：校验 → 协商或执行 → 流式推送进度与报告。 |
| [`src/server/task_execution.py`](src/server/task_execution.py) | `TaskExecutionService` 执行接入点，以及 demo 的 UC1 服务。 |
| [`src/common/`](src/common) | 共享适配器、按 schema 路由的 mock LLM、`WebhookReceiver`，以及双端共享的 `TASK_PARAM_SCHEMA`。 |
| [`src/registry/`](src/registry) | mock 注册中心（AgentCard 注册/查询，端口 5001）。 |
| [`resources/mock_llm/`](resources/mock_llm) | 脚本化的槽位抽取与内容校验应答。 |
| [`run_demo.py`](run_demo.py) | 跨平台、非阻塞的启动器。 |

## 2. 快速开始

前置条件：Python（使用仓库 `.venv`）与 [`uv`](https://docs.astral.sh/uv/)。

```bash
# 1) 仓库根目录：创建 .venv 并安装 SDK
uv sync --dev

# 2) a2a-t-sample/ —— 存放 .env 的目录
cd a2a-t-sample
cp env.example .env
uv pip install -r requirements.txt

# 3) 运行样例（注册中心与服务端作为后台子进程启动，客户端在前台运行）
cd ran-energy-saving
python run_demo.py           # 默认英文；加 --language zh-CN 运行中文
```

请使用装有样例依赖的解释器（仓库 `.venv`），例如 Windows 下
`..\..\.venv\Scripts\python.exe run_demo.py`；两端输出会实时交错打印到终端，随后后台进程被停止。

加 `--scenario negotiation`（或 `--negotiation target` / `--negotiation feasibility`）改为运行
协商。若客户端无法保持流式连接，加 `--transport webhook` —— 见 [§6.1](#61-webhook-传输)。

## 3. 使用 A2A-T SDK 实现 Task-T 交互

### 3.1 四个步骤

1. **生成** —— 客户端用 A2A-T 客户端 SDK 的 `generate_task_prompt_from_text`，把自然语言输入转换为
   Task-T 任务请求（渲染好的提示词）。
2. **发送** —— 提示词放在消息 `metadata` 中发给服务端。
3. **校验** —— 服务端从 metadata 中取出提示词，调用 A2A-T 服务端 SDK 的
   `validate_task_prompt_and_data_filling` 校验并抽取参数（`operationType`、`region`、
   `energySavingGoal`、`guaranteedThroughputTarget` —— 双端共享的调用方 schema `TASK_PARAM_SCHEMA`，
   见 [`src/common/task_schema.py`](src/common/task_schema.py)）。
4. **执行并报告** —— 抽取出的参数交给业务执行；进度以状态更新返回、结果以报告 artifact 返回，
   由客户端呈现。

### 3.2 客户端侧

整段交互如下（[`client_flow.py`](src/client/client_flow.py)）：

```python
prompt_content = prompt_client.generate_task_prompt_from_text(input_text, ENERGY_SAVING_TEMPLATE_URI)
prompt_text, extension_uri = _require_prompt_text(prompt_content)
request = SendMessageRequest()
...  # 此处省略 message_id / role
request.message.parts.add().text = _build_request_metadata(initial_input)
request.message.metadata[extension_uri] = prompt_text   # 提示词放在 metadata 中传输
request.message.metadata["templateUri"] = getattr(prompt_content, "template_uri", None) or ENERGY_SAVING_TEMPLATE_URI
call_context = ClientCallContext(service_parameters={"A2A-Extensions": extension_uri})
async for stream_response in a2a_client.send_message(request, context=call_context):
    ...  # 消费状态更新与报告 artifact
```

### 3.3 服务端侧

`_extract_prompt_text` 从 metadata 读取提示词，`validate_task_prompt_and_data_filling` 校验并抽取
([`server_flow.py:82`](src/server/server_flow.py#L82)、[`server_flow.py:486`](src/server/server_flow.py#L486))。
被拒绝的提示词映射为 `TASK_STATE_REJECTED`，而不是让服务崩溃；校验通过的参数进入执行接入点。

### 3.4 client 侧要替换什么

模板与参数 schema 是两端共享的契约：客户端用它渲染任务请求，服务端用它做校验。本样例的模板
（`ENERGY_SAVING_TEMPLATE_URI` = `Task-T/network-layer/ran-energy-saving/v1`）与 schema 都是样例
特有的，因此 client 实现要换成自己的（[`scenario_data.py`](src/client/scenario_data.py)）；服务端
`TaskExecutionService` 接入点之后的业务执行属于 server agent（见
[替换清单](#5-替换清单demo-到生产)）。

## 4. 使用 A2A-T SDK 实现 Negotiation-T 交互

一句话分界：SDK 拥有报文 —— 渲染、校验、协商会话映射、传输机制。业务由两端各自负责：server
agent 决定请求何时无法按原样执行、值得先谈一谈；client agent 决定如何应答收到的 propose。

### 4.1 什么时候会发生协商

服务端无法按请求原样执行，但这个差距又是双方**谈一谈就能补齐**、而不是只能拒绝的时候。样例覆盖三种
情况：

**信息不完整。** 客户端要求执行节能，给了目标、时段和吞吐目标，却没说在哪个区域。服务端缺这一项
无法开工，于是告诉客户端缺什么；客户端补上，任务继续。

**目标自相矛盾。** 客户端给出的分时段保障速率目标里，两个时段重叠：
`{17:30~23:00,20Mbps}` 与 `{20:00~00:00,10Mbps}`。请求本身格式没问题，但重叠时段的保障速率无法
唯一确定。服务端复述它理解的意图，请客户端调整时间；客户端带着修正后的时段回应。

**目标在当前条件下可能做不到。** 客户端要求两个站点在 00:00~18:00 的停电期间保障
`{00:00~06:00,2Mbps}、{06:00~18:00,10Mbps}`，而站点既有的备电要求是至少支撑 18 小时。按这个
速率执行，电池撑不到停电结束。服务端给出不可行的结论和提案（把 06:00~18:00 的保障速率从 10Mbps 下调
到 2Mbps）；客户端接受调整后的目标。

### 4.2 样例如何判断属于哪一种

判断属于哪种情况、以及到底要不要协商，是**server agent 的业务决策，SDK 不持立场** —— client
只对收到的 propose 作出反应。本样例是数据驱动的：首个内容校验错误的错误码通过
`_NEGOTIATION_TYPE_BY_ERROR_CODE` 表
（[`server_flow.py:64`](src/server/server_flow.py#L64)）选出类型。这张表是**样例约定，不是 SDK
契约** —— server 实现可随意改自己的副本（重映射、新增、或干脆不用映射、按自己的业务规则判断；
未映射的失败就是普通拒绝）。

报文并不依赖这张表。每个 propose 都记录自己的 `templateUri` —— 即 metadata 中这段报文由哪个
Prompt 模板渲染而来，例如 `Negotiation-T/information-negotiation/propose/v1`。这个 URI 同时告诉
客户端收到的是哪一类协商，因此客户端按收到的消息决定应答 —— accept、reject 或 abort —— 而不是按
错误码。

### 4.3 共享机制

以下对三类协商通用，各类型小节不再重复。

**一轮 = 一次 propose 加上对它的应答。** 会话上下文携带 1 起始的 `round`；应答沿用 propose 的轮次，
只有**新的** propose 才会推进轮次。客户端最初那条请求不算协商轮 —— 协商从服务端的 propose 才开始。
`maxRounds`（默认 5）限制会话轮次。

**双方都可以发起 propose。** 协商是对称的：客户端与服务端 SDK 都提供同样的四个生成方法
（propose / accept / reject / abort）和同样的校验方法，任何一方都能发起协商，任何一方也都能应答。
本样例让**服务端**发起，是因为发现"请求无法按原样执行"的是服务端 —— 这是本场景的选择，不是报文本身
的约束：客户端同样可以发起协商，例如它无法满足服务端公布的某项约束时，由服务端来 accept / reject /
abort。角色只决定谁发 propose；报文形态、会话处理与传输完全一致。本样例只在客户端侧接了决策
（provider 接入点）；如果由客户端发起协商，对等的决策逻辑要放在服务端侧。

**四条报文的闭环：**

1. 客户端 → 服务端：按原样的节能请求 —— 样例中分别是缺区域、时段重叠、停电时长无法支撑。
2. 服务端 → 客户端：propose —— 缺什么、需要澄清什么、或需要评估什么。
3. 客户端 → 服务端：把缺失或需修正的内容补全后重新发送的那条请求；应答随同一条消息一起发出
   （是一条消息，不是两条）。
4. 服务端 → 客户端：结果 —— 先进度，后报告。

会话（`id` / `round` / `maxRounds` / performative）随每条协商消息放在 `negotiationContext` metadata
中，因此 SDK 本身无状态。

**client 侧要接的地方。** 收到第 2 条报文时，客户端 flow 构建一个轻量的 `NegotiationProposeView`（kind、
`templateUri`、渲染文本）并调用（[`negotiation_response.py`](src/client/negotiation_response.py)）：

```python
@runtime_checkable
class NegotiationResponseProvider(Protocol):
    def respond(
        self, propose: NegotiationProposeView, session: NegotiationContext
    ) -> NegotiationDecision: ...
```

`NegotiationDecision` 携带 `conclusion`（`accept` / `reject` / `abort`）加上对应类型的 accept
字段：`filled_params`（信息协商，name → value）、`confirmed_intent`（目标协商）、
`feasibility_summary`（可行性协商），以及 `reason`（reject/abort）。**`accept` 已端到端接线**：
flow 根据 client 侧的决策重新生成 Task-T 提示词、构建带同轮 `ACCEPT` 标记的类型化 accept，并经所选传输发送。
`respond` 是同步的 —— 需要异步的实现（人工确认、上游系统调用）在实现内部自行处理。

> Provider 只替换对 propose 的脚本化**应答** —— Task-T 补全参数（`negotiation_params`）始终是
> flow 的调用方输入，缺了它们协商不会开启。

### 4.4 信息协商（information）

**① 服务端何时开启。** 请求不完整 —— 缺少服务端必需的参数（样例中为区域）。样例的触发条件是
`content.param_missing` 校验错误，但这个错误码是样例的接线方式，不属于协议本身。

**② 参考代码做了什么。**

| 步骤 | SDK 调用 | 参考代码 |
| --- | --- | --- |
| 服务端 propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, InformationProposeContent(items, relationship)), INFORMATION_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| 客户端 accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, InformationEndingContent(ACCEPT, items)), INFORMATION_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607)、[`client_flow.py:659`](src/client/client_flow.py#L659) |
| 应答来源 | `NegotiationDecision(conclusion="accept", filled_params={...})` | [`negotiation_response.py`](src/client/negotiation_response.py) |

propose 的 `items` 是缺失的参数；accept 的 `items` 是补全后的取值。

**③ client 侧可以原样复用的部分。** 提示词渲染、`templateUri`/`negotiationContext` 的 metadata 组装、
会话处理与传输 —— `client_flow.py` 中除决策本身以外的全部内容。

**④ client 侧必须自己实现的部分。** provider 接入点背后的应答来源。propose 的业务措辞 ——
要问哪些参数、如何命名 —— 属于 server agent（`server_flow._missing_items_from_errors` 加上它的
槽位 schema）。

```python
class MyProvider:
    def respond(self, propose: NegotiationProposeView, session: NegotiationContext) -> NegotiationDecision:
        values = my_system.look_up(propose.kind, propose.prompt_text)   # client 侧的查询
        return NegotiationDecision(conclusion="accept", filled_params=values)
```

### 4.5 目标协商（target）

**① 服务端何时开启。** 请求自相矛盾：分时段保障速率目标里有两个时段重叠，重叠时段的保障速率无法
唯一确定。样例的触发条件是 `content.value_not_allowed` 校验错误。

**② 参考代码做了什么。**

| 步骤 | SDK 调用 | 参考代码 |
| --- | --- | --- |
| 服务端 propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, TargetProposeContent(description, intent_understanding=[...], request_for_clarification=[...])), TARGET_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| 客户端 accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, TargetEndingContent(ACCEPT, confirmed_intent, failure_reason=None)), TARGET_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607) |
| 应答来源 | `NegotiationDecision(conclusion="accept", confirmed_intent="...")` | [`negotiation_response.py`](src/client/negotiation_response.py) |

样例用的是**澄清**形态：服务端复述它理解的意图（`intent_understanding`），并请求调整时间
（`request_for_clarification`）。accept 把修正后的目标放进 `confirmed_intent`。

**③ client 侧可以原样复用的部分。** 与信息协商相同：渲染、metadata 组装、会话处理、传输。

**④ client 侧必须自己实现的部分。** 如何得出 `confirmed_intent` —— 样例从 `scenario_data`
读取；真实 client 的值来自把歧义解决掉的那一方。propose 中澄清请求的措辞属于 server agent
（`_negotiation_propose_data` 的 target 分支）。

> `TargetProposeContent` 还支持确认请求形态（`target_confirm_request` 非空，三个条件列表为空 ——
> SDK 强制的互斥规则），以及后续轮的 `alignment_and_clarification`。样例用的是首轮澄清形态。

### 4.6 可行性协商（feasibility）

**① 服务端何时开启。** 请求在当前条件下可能做不到：保障速率无法支撑所声明站点的停电时长。样例的
触发条件是 `content.semantic_conflict` 校验错误。

**② 参考代码做了什么。**

| 步骤 | SDK 调用 | 参考代码 |
| --- | --- | --- |
| 服务端 propose | `generate_negotiation_propose_prompt_from_data(NegotiationProposeData(context, FeasibilityProposeContent(description, action=PROPOSE_ALTERNATIVE_ON_FAILURE, infeasibility_details_and_proposal=[...])), FEASIBILITY_NEGOTIATION_PROPOSE_URI)` | [`server_flow.py:207`](src/server/server_flow.py#L207) |
| 客户端 accept | `generate_negotiation_accept_prompt_from_data(NegotiationEndingData(context, FeasibilityEndingContent(ACCEPT, feasibility_summary)), FEASIBILITY_NEGOTIATION_ACCEPT_REJECT_URI)` | [`client_flow.py:607`](src/client/client_flow.py#L607) |
| 应答来源 | `NegotiationDecision(conclusion="accept", feasibility_summary="...")` | [`negotiation_response.py`](src/client/negotiation_response.py) |

样例把已完成的评估结论作为不可行上报，并附上替代提案
（`action=PROPOSE_ALTERNATIVE_ON_FAILURE` 加 `infeasibility_details_and_proposal`）；accept 说明
它同意的调整。

**③ client 侧可以原样复用的部分。** 与信息协商相同：渲染、metadata 组装、会话处理、传输。

**④ client 侧必须自己实现的部分。** 应答：`feasibility_summary` 必须来自 client 自己的规划、
仿真或备电容量系统。评估本身 —— 不可行的结论和提案 —— 属于 server agent，在
`_negotiation_propose_data` 的 feasibility 分支构建。

> `FeasibilityProposeContent` 还支持 `REQUEST_FEASIBILITY_EVALUATION`（请对方评估
> `contents_to_evaluate`）与 `feasibility_confirm_request` 形态。样例用的是「不可行 + 提案」形态。

### 4.7 reject 与 abort —— 扩展点

`reject`/`abort` 决策**在本 demo 中未接线**：flow 打日志
`negotiation-decision: {conclusion} (not wired in this demo...)` 后结束、不发送任何报文。扩展点是
[`client_flow.py:752`](src/client/client_flow.py#L752) 中 `_plan_negotiation_accept` 的 reject/abort
分支：扩展它、仿照 `_run_negotiation_accept`（[`client_flow.py:870`](src/client/client_flow.py#L870)）
—— 以 `session.next_round().with_performative(NegotiationPerformative.REJECT)` 推进会话，构建该
类型的结束内容、conclusion 置为 `Reject`（决策的 `reason` 写入结束内容的 `failure_reason`），再用
SDK 的 `generate_negotiation_reject_prompt_from_data` / `..._from_text` 生成报文。abort 与类型无关：
`NegotiationAbortContent(termination_reason)` 经 `generate_negotiation_abort_prompt_from_data` 渲染到
公共的 `Negotiation-T/common/abort/v1` 模板。

### 4.8 从结构化数据生成，还是从自然语言生成

协商消息有两种产生方式，最终报文格式完全一致：

- **从结构化数据生成（默认）**：在代码里构造类型化内容，SDK 直接渲染模板 —— 确定性，不调用模型。
- **从自然语言生成**（`--channel fromText`）：先写一段自然语言，SDK 让模型抽取成结构化内容，再渲染
  模板 —— 每条生成的消息多一次抽取调用。

## 5. 替换清单（demo 到生产）

**归属**列标明该项由哪一端负责。client 团队只需处理标注为 client 的行；server 行说明对端换成真实系统
时要替换什么。

| demo 组成 | 归属 | demo 中的作用 | 保留还是替换 |
| --- | --- | --- | --- |
| mock LLM —— [`resources/mock_llm/`](resources/mock_llm) + `src/common/` 中按 schema 路由的 mock | 两端 | 脚本化的槽位抽取与内容校验应答，`A2AT_LLM_API_KEY` 为空时启用 | CI/离线运行**保留**；生产中两端各自移除并配置真实的 OpenAI 兼容端点 |
| 脚本化执行数据 —— [`src/server/constants_data.py`](src/server/constants_data.py) | server | demo 执行流式推送的 UC1 进度步骤与意图报告 | server agent 经 `TaskExecutionService` 接入点**替换**（[`src/server/task_execution.py`](src/server/task_execution.py)）；它的服务装配后，demo 服务与数据即可删除 |
| 脚本化应答 —— [`src/client/scenario_data.py`](src/client/scenario_data.py) | 两端（见下） | 同时喂两侧 | 两侧都要**替换**：client 侧应答经自己的 `NegotiationResponseProvider`；server 侧在其副本的 `server_flow._negotiation_propose_data` 中改为自己的业务措辞 |
| mock 注册中心 —— [`src/registry/`](src/registry)（端口 5001） | client | AgentCard 注册/查询，用于发现 | **替换**为 client 侧的注册中心，或绕过发现、直接从已知 AgentCard 构造 `a2a-sdk` 客户端 |
| 静态 webhook token —— `A2AT_SAMPLE_WEBHOOK_TOKEN`（默认 `demo-webhook-token`） | 两端 | `X-A2A-Notification-Token` 头中的共享静态 token | **替换**为生产鉴权（逐对密钥、轮换、TLS）；把接收器当作暴露在外的 HTTP 端点对待 |

> **`scenario_data` 同时喂两侧** —— 客户端的脚本化应答（补全参数、确认意图、可行性摘要）经
> provider 接入点使用；服务端的 propose 内容也由同一模块构建：`_negotiation_propose_data`
> ([`server_flow.py:207`](src/server/server_flow.py#L207)) 用其访问器填充 target/feasibility 的
> propose 文案。只替换 provider 与执行服务，服务端仍会用 demo 的预制文案发起 propose ——
> 该模块不可删除。

## 6. 附录

### 6.1 Webhook 传输

两种传输投递相同的任务事件；区别在客户端如何接收。默认是 SSE 流式：客户端保持流式 `message:send`
连接，事件到达即消费。Webhook 推送通知面向不保持流式连接的客户端（长时任务、间歇在线的客户端）：
`--transport webhook`（或 `A2AT_SAMPLE_TRANSPORT=webhook`）。

webhook 模式下的变化：客户端非阻塞发送（`return_immediately`），每个任务事件以 webhook POST 形式
送达客户端进程内嵌的接收器（[`src/common/webhook_receiver.py`](src/common/webhook_receiver.py) 的
`WebhookReceiver`），它额外绑定一个监听端口（`--webhook-port`，默认 9000）。服务端推送在
`X-A2A-Notification-Token` 头中携带 `A2AT_SAMPLE_WEBHOOK_TOKEN`（默认 `demo-webhook-token`）——
不匹配回 `401`、事件被丢弃。流程在任一 SDK 终态结束，另有 30 秒空闲兜底。协商往返在两种传输上
逐语句一致。

- **两条注册路径。** 初始请求在请求上内联注册推送配置
  （`SendMessageConfiguration.task_push_notification_config`，即扁平的
  `TaskPushNotificationConfig(url, token)` —— 此时 task id 尚未知）；发送 accept 前，经
  `POST /tasks/{id}/pushNotificationConfigs` REST 端点（a2a-sdk
  `BaseClient.create_task_push_notification_config`）重注册同一 URL/token。
- **没有服务端模式开关。** 服务端无条件装配推送机制（a2a-sdk `DefaultRequestHandler` 携带
  `InMemoryPushNotificationConfigStore` + `BasePushNotificationSender`，
  [`server_main.py`](src/server/server_main.py)）；SSE 模式下它只是闲置。
- **非阻塞路径。** a2a 客户端以 `ClientConfig(streaming=False)` 构建 —— 这是选择非阻塞
  `message:send` 路径的必要条件（仅设置 `return_immediately` 不会切换路径）。推送报文是与 SSE 事件
  同构的 StreamResponse JSON —— 因此协商检测、artifact 计数与展示逻辑与 SSE 共享。
- **Token 鉴权。** 接收器对每个 POST 校验 `X-A2A-Notification-Token`：不匹配回 `401`（事件被丢弃），
  畸形或无法识别的报文回 `400`。demo 的静态共享 token 是 demo 级的 —— 生产请使用 TLS 之上可轮换的
  逐客户端密钥。
- **结束信号。** 任一 SDK 终态（`TASK_STATE_COMPLETED` / `FAILED` / `CANCELED` / `REJECTED`）都会在
  两个阶段立即结束流程 —— 协商场景正常以 `COMPLETED` 结束，校验失败或流异常以 `REJECTED` /
  `FAILED` 直接结束，summary 行以 `end=<state>` 记录终态结束。默认场景仍以报告 artifact 结束；
  `INPUT_REQUIRED`（协商 propose）只是初始阶段的中断点，不是流程结束。空闲兜底
  （`A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT`，默认 30 秒，每收到一个事件重置）打印 `webhook-idle-timeout`
  行后带着已收集的事件正常结束。`A2AT_SAMPLE_MAX_ARTIFACTS` 在 webhook 模式下不生效；每个阶段的唯一
  RPC 应答以 `webhook-rpc-reply` 记录一次、不重复计数，`stream-completed` 汇总统计经 webhook 收到
  的事件并追加 `transport=webhook`。

### 6.2 配置（a2a-t-sample/.env）

| 键 | 含义 |
| --- | --- |
| `A2AT_LANGUAGE` | `zh-CN` / `en-US`；决定自然语言输入与 mock/模板语言树 |
| `A2AT_SAMPLE_SCENARIO` | `default` / `negotiation`；样例场景变体（与 `--scenario` 语义相同） |
| `A2AT_SAMPLE_NEGOTIATION_KIND` | `information` / `target` / `feasibility`（默认 `information`）；协商场景输入，与 `--negotiation` 语义相同 |
| `A2AT_SAMPLE_CHANNEL` | `data` / `text`（默认 `data`）；协商消息是从结构化数据生成还是从自然语言生成，与 `--channel` 语义相同 |
| `A2AT_SAMPLE_TRANSPORT` | `sse` / `webhook`（默认 `sse`）；客户端事件传输方式，与 `--transport` 语义相同 |
| `A2AT_SAMPLE_WEBHOOK_PORT` | 内嵌 webhook 接收器的绑定端口（默认 `9000`；`0` = 操作系统分配临时端口） |
| `A2AT_SAMPLE_WEBHOOK_TOKEN` | 服务端在 `X-A2A-Notification-Token` 头中必须携带的推送 token（默认 `demo-webhook-token`）；不匹配回 `401` |
| `A2AT_SAMPLE_WEBHOOK_IDLE_TIMEOUT` | 空闲兜底超时（秒）：webhook 流程超过该时长未收到推送即结束（默认 `30`） |
| `A2AT_LLM_API_KEY` | 留空 ⇒ mock LLM；否则为 OpenAI 兼容的 key |
| `A2AT_LLM_PROVIDER` / `A2AT_LLM_MODEL` / `A2AT_LLM_BASE_URL` | LLM 端点配置 |
| `A2AT_LLM_TIMEOUT_SECONDS` | HTTP 客户端超时（秒，默认 60） |
| `A2AT_SAMPLE_HOST` / `A2AT_SAMPLE_PORT` | 手动 `python -m` 路径的服务端绑定（默认 `127.0.0.1:8000`） |
| `REGISTRY_CENTER_HOST` / `REGISTRY_CENTER_PORT` | 手动 `python -m` 路径的注册中心绑定（默认 `127.0.0.1:5001`） |
| `A2AT_SAMPLE_DEBUG` | `true` 时打印完整的 LLM 请求/响应报文 |
| `A2AT_SAMPLE_MAX_ARTIFACTS`（进程环境变量，非 `.env`） | 收到 N 个 artifact 后停止（默认 0 = 不限制） |

说明：`run_demo.py` 的就绪检查固定使用 `5001` / `8000`，不读取 `A2AT_SAMPLE_PORT` /
`REGISTRY_CENTER_PORT` —— 这两对仅在手动 `python -m` 路径下设置。服务端的进度与报告文案是内置的
英文，与 `--language` 无关；该开关只切换客户端输入、模板与 mock 应答，协商消息文本则跟随运行语言。
模板、参数 schema、AgentCard 与自然语言输入都是本样例特有的；模板必须属于 `Task-T` 扩展且使用
`v1` 版本。远程 prompt 资源加载不属于 SDK；本处的注册中心是样例侧用于 AgentCard 发现的 mock，
不是 SDK 能力。

### 6.3 分别启动两端

把 `PYTHONPATH` 指向用例的 `src/`，在各自终端分别启动（`.env` 从当前目录读取；缺失 `.env` 对客户端
与服务端而言会**直接报错**）。先启动注册中心与服务端 —— 客户端要从注册中心发现服务端的 AgentCard。

```bash
# bash / zsh
cd a2a-t-sample
export PYTHONPATH="$PWD/ran-energy-saving/src"
uv run --no-sync python -m registry.registry_main    # 终端一：注册中心（5001）
uv run --no-sync python -m server.server_main         # 终端二：服务端（8000）
uv run --no-sync python -m client.client_main         # 终端三：客户端
```

```powershell
# PowerShell
cd a2a-t-sample
$env:PYTHONPATH = "$pwd\ran-energy-saving\src"
uv run --no-sync python -m registry.registry_main
uv run --no-sync python -m server.server_main
uv run --no-sync python -m client.client_main
```

运行语言取自 `a2a-t-sample/.env` 的 `A2AT_LANGUAGE`（`env.example` 默认是 `zh-CN`）；`run_demo.py`
每次运行用 `--language` 覆盖（默认 `en-US`）。

### 6.4 mock LLM 机制

`A2AT_LLM_API_KEY` 为空时，样例安装随包的 mock LLM
([`src/common/mock_llm.py`](src/common/mock_llm.py))：它按**输出 schema 签名**
（`route_structured_call`）路由每个结构化调用，回放
[`resources/mock_llm/{zh-CN,en-US}/`](resources/mock_llm) 中脚本化的槽位抽取与内容校验应答。

### 6.5 支持的参数（`run_demo.py`）

| 参数 | 作用 |
| --- | --- |
| `--language {en-US,zh-CN}` | 运行语言（默认 `en-US`） |
| `--scenario {default,negotiation}` | 场景变体（默认 `default`；`negotiation` 运行缺失 region 的信息协商） |
| `--negotiation {information,target,feasibility}` | 协商类型，选择客户端场景输入（默认 `information` = 缺失 region；`target` = 分时段保障速率目标中两个时段重叠；`feasibility` = 保障速率无法支撑 18 小时停电）。`information` 仍受 `--scenario` 开关控制（只有 `--scenario negotiation` 运行缺失 region 流程）；`target`/`feasibility` 无需该开关即可触发。该参数只选择输入 —— 服务端依据内容校验错误码数据驱动地分派 |
| `--channel {fromData,fromText}` | 协商消息从结构化数据生成（`fromData`，默认）还是从自然语言生成（`fromText`，每条消息多一次抽取模型调用） |
| `--transport {sse,webhook}` | 客户端的事件传输方式（默认 `sse`）；见 [§6.1](#61-webhook-传输) |
| `--webhook-port N` | `--transport webhook` 时内嵌 webhook 接收器的绑定端口（默认 9000；`0` 表示由操作系统分配临时端口） |
| `--keep-alive` | 保留注册中心/服务端不停止；之后用 `--stop` 关闭 |
| `--keep-logs` | 保留临时运行目录（日志 / `.env`），位于 `<temp>/a2at-ran-energy-saving/` |
| `--stop` | 停止之前 `--keep-alive` 留下的后台进程并退出 |
| `--timeout N` | 等待每个端口的秒数（默认 30） |
