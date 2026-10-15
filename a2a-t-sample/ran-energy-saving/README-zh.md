# RAN 节能（Task-T）样例

## 目录

- [概览](#概览)
  - [目录结构](#目录结构)
- [如何运行](#如何运行)
  - [分别启动两端](#分别启动两端)
  - [配置](#配置)
- [如何使用 A2A-T SDK](#如何使用-a2a-t-sdk)
  - [客户端：生成并发送 Task-T prompt](#客户端生成并发送-task-t-prompt)
  - [服务端：校验、抽取并流式推送](#服务端校验抽取并流式推送)
- [协商场景（Negotiation-T）](#协商场景negotiation-t)

## 概览

本样例是 A2A-T Python SDK 的一个完整 **Task-T** 示例。它包含：面向 RAN 节能任务的 A2A-T
**客户端**与**服务端**，以及用于发现 AgentCard 的**注册中心**。

样例按下列步骤交互：

1. **构造请求**——客户端调用 A2A-T 客户端 SDK 的 `generate_task_prompt_from_text`，把原始自然语言输入
   转换为 Task-T 任务请求（渲染好的 prompt）。
2. **发送请求**——客户端把任务请求发给服务端；渲染好的 prompt 放在 `message.metadata` 中。
3. **校验**——服务端从 `message.metadata` 取出 prompt，调用 A2A-T 服务端 SDK 的
   `validate_task_prompt_and_data_filling` 做校验并抽取参数。
4. **执行并上报**——服务端把抽取出的参数交给业务逻辑，执行节能任务，生成状态更新与节能报告并发送给
   客户端。
5. **接收与呈现**——客户端消费流式事件：进度取自状态更新消息的文本，报告正文取自
   `artifact.metadata`，并呈现两者。

### 目录结构

| 路径 | 作用 |
| --- | --- |
| `src/client/` | 客户端：注册中心发现 → `generate_task_prompt_from_text` → 消费流 |
| `src/server/` | 服务端：`validate_task_prompt_and_data_filling` → 流式推送进度与意图报告 |
| `src/registry/` | mock 注册中心（AgentCard 注册/查询，端口 5001） |
| `src/common/` | 共享适配器 + 按 schema 路由的 mock LLM |
| `resources/mock_llm/{zh-CN,en-US}/` | 脚本化的槽位抽取与内容校验应答 |
| `run_demo.py` | 跨平台、非阻塞启动器（后台注册中心/服务端，自动清理） |

## 如何运行

> **前置条件：** Python（使用仓库 `.venv`）与 [`uv`](https://docs.astral.sh/uv/)。

先在仓库根目录和 `a2a-t-sample/` 各执行一次：

```bash
# 仓库根目录：创建 .venv 并安装 SDK
uv sync --dev

# a2a-t-sample/（存放 .env 的目录）
cd a2a-t-sample
cp env.example .env
uv pip install -r requirements.txt
```

然后用一条跨平台命令运行样例：

```bash
cd a2a-t-sample/ran-energy-saving
python run_demo.py --language zh-CN   # 中文（默认是 en-US）
```

`run_demo.py` 会把注册中心和服务端作为**后台子进程**启动，并在前台运行客户端。两端输出会**实时交错**
打印到终端（日志同时写入运行目录），随后停止后台进程。请使用装有样例依赖的解释器（仓库 `.venv`），
例如 Windows 下 `..\..\.venv\Scripts\python.exe run_demo.py`。

| 参数 | 作用 |
| --- | --- |
| `--language {en-US,zh-CN}` | 运行语言（默认 `en-US`） |
| `--scenario {default,negotiation}` | 场景变体（默认 `default`；`negotiation` 运行缺失 region 的信息协商，见[协商场景](#协商场景negotiation-t)） |
| `--negotiation {information,target,feasibility}` | 协商类型，选择客户端场景输入（默认 `information` = 缺失 region；`target` = 100Mbps 速率超出小区能力；`feasibility` = 30% 节能目标与 50Mbps 速率目标冲突）。该参数只选择输入——服务端依据内容校验错误码数据驱动地分派，见[协商场景](#协商场景negotiation-t) |
| `--channel {fromData,fromText}` | 协商 propose/accept 消息的生成通道（默认 `fromData`；`fromText` 每条生成的消息多一次抽取 LLM 调用，见[协商场景](#协商场景negotiation-t)） |
| `--keep-alive` | 保留注册中心/服务端不停止；之后用 `--stop` 关闭 |
| `--keep-logs` | 保留临时运行目录（日志 / `.env`），位于 `<temp>/a2at-ran-energy-saving/` |
| `--stop` | 停止之前 `--keep-alive` 留下的后台进程并退出 |
| `--timeout N` | 等待每个端口的秒数（默认 30） |

> 服务端的进度与报告文案是内置的英文，与 `--language` 无关；该开关只切换客户端输入、模板与 mock 应答。

**预期输出**（已裁剪）：

```text
[client] a2a-event: {"task": {"id": "...", "status": {"state": "TASK_STATE_SUBMITTED", ...}}}
[client] a2a-event: {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING", ...}}}
...
[client] a2a-event: {"artifactUpdate": {"artifact": {"name": "energy saving intent report", ...}}}
[client] stream-completed: events=12 artifacts=1
```

### 分别启动两端

需要把服务端和客户端放在不同终端运行时（例如托管你自己的服务端，或分别查看两端日志），把
`PYTHONPATH` 指向用例的 `src/` 后分别启动即可。`.env` 从当前目录读取，缺失 `.env` 对客户端/服务端
而言会**直接报错**。

> [!IMPORTANT]
> 先启动注册中心与服务端；客户端要从注册中心发现服务端的 AgentCard，因此它们必须已在运行。

运行语言取自 `a2a-t-sample/.env` 的 `A2AT_LANGUAGE`（`env.example` 默认是 `zh-CN`）；`run_demo.py`
每次运行用 `--language` 覆盖（默认 `en-US`）。

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

### 配置

本样例用到的配置（来自 `a2a-t-sample/.env`）：

| 键 | 含义 |
| --- | --- |
| `A2AT_LANGUAGE` | `zh-CN` / `en-US`；决定自然语言输入与 mock/模板语言树 |
| `A2AT_SAMPLE_SCENARIO` | `default` / `negotiation`；选择样例场景变体（`negotiation` = 缺失 region 的信息协商，见[协商场景](#协商场景negotiation-t)） |
| `A2AT_SAMPLE_NEGOTIATION_KIND` | `information` / `target` / `feasibility`（默认 `information`）；选择协商场景输入，与 `--negotiation` 语义相同 |
| `A2AT_SAMPLE_CHANNEL` | `data` / `text`（默认 `data`）；协商 propose/accept 消息的生成通道，与 `--channel` 语义相同 |
| `A2AT_LLM_API_KEY` | 留空 ⇒ mock LLM；否则为 OpenAI 兼容的 key |
| `A2AT_LLM_PROVIDER` / `A2AT_LLM_MODEL` / `A2AT_LLM_BASE_URL` | LLM 端点配置 |
| `A2AT_LLM_TIMEOUT_SECONDS` | HTTP 客户端超时（秒，默认 60） |
| `A2AT_SAMPLE_HOST` / `A2AT_SAMPLE_PORT` | 手动 `python -m` 路径的服务端绑定（默认 `127.0.0.1:8000`） |
| `REGISTRY_CENTER_HOST` / `REGISTRY_CENTER_PORT` | 手动 `python -m` 路径的注册中心绑定（默认 `127.0.0.1:5001`） |
| `A2AT_SAMPLE_DEBUG` | `true` 时打印完整的 LLM 请求/响应报文 |

环境变量（从进程环境读取，非 `.env`）：

| 键 | 含义 |
| --- | --- |
| `A2AT_SAMPLE_MAX_ARTIFACTS` | 收到 N 个 artifact 后停止（默认 0 = 不限制） |

说明：

- `run_demo.py` 的就绪检查固定使用 `5001` / `8000`，不读取 `A2AT_SAMPLE_PORT` /
  `REGISTRY_CENTER_PORT`；这两对仅在手动 `python -m` 路径下设置。
- 上面这些值（`template_uri`、参数 schema、AgentCard、自然语言输入）都是**本样例特有**的，请替换为
  你自己的。模板必须属于 `Task-T` 扩展且使用 `v1` 版本。
- 内置资源与语言覆盖有限；远程 prompt 资源加载不属于 SDK；本处的注册中心是样例侧的 mock，用于
  AgentCard 发现，不是 SDK 能力。

## 如何使用 A2A-T SDK

### 客户端：生成并发送 Task-T prompt

用 A2A-T SDK 把你的自然语言输入渲染成 Task-T prompt ——
[`client_flow.py:286`](src/client/client_flow.py#L286)：

```python
prompt_content = prompt_client.generate_task_prompt_from_text(input_text, ENERGY_SAVING_TEMPLATE_URI)
prompt_text, extension_uri = _require_prompt_text(prompt_content)
```

它返回 `MetadataContent`（`prompt_text`、`extension_uri`、`template_uri`、`build_metadata_content()`），
并会抛出 `PromptGenerationError`（错误码如 `template.not_found`、`slot.not_provided`、
`llm.not_configured`、`input.text_too_long`）。

把 prompt 放进消息 metadata，用 `a2a-sdk` 发送（流式）——
[`client_flow.py:300`](src/client/client_flow.py#L300)：

```python
request = SendMessageRequest()
request.message.message_id = str(uuid.uuid4())
request.message.role = Role.ROLE_USER
request.message.parts.add().text = _build_request_metadata(initial_input)
request.message.metadata[extension_uri] = prompt_text
request.message.metadata["templateUri"] = (
    getattr(prompt_content, "template_uri", None) or ENERGY_SAVING_TEMPLATE_URI
)

call_context = ClientCallContext(
    service_parameters={"A2A-Extensions": extension_uri},
)

async for stream_response in a2a_client.send_message(request, context=call_context):
    ...
```

这段之前的目标 AgentCard 发现与 `a2a-sdk` 客户端构建见
[`client_main.py`](src/client/client_main.py)。

### 服务端：校验、抽取并流式推送

从客户端发来的消息 metadata 中读取 A2A-T 报文 ——
[`server_flow.py:52`](src/server/server_flow.py#L52)：

```python
def _extract_prompt_text(request_context: RequestContext) -> str:
    """Extract the prompt text from metadata under the Task-T extension URI."""
    if request_context.message is None or request_context.message.metadata is None:
        raise ValueError("Expected message metadata for Task-T prompt")
    metadata = MessageToDict(request_context.message.metadata)
    return str(metadata.get(TASK_T_EXTENSION_URI, ""))
```

用 A2A-T SDK 校验 prompt 并抽取参数 ——
[`server_flow.py:296`](src/server/server_flow.py#L296)：

```python
filled_params = prompt_server.validate_task_prompt_and_data_filling(
    prompt=prompt_text,
    schema=TASK_PARAM_SCHEMA,
    template_uri=ENERGY_SAVING_TEMPLATE_URI,
)
```

`schema` 由**调用方**提供：它声明要抽取哪些参数；`filled_params.data` 是合并后的参数 map（数组型
schema 则为 list）。prompt 被拒绝时会抛出 `ContentValidationError`（错误码如
`negotiation.semantic_rejected`、`llm.invocation_failed`、`template.not_found`），并携带
`exc.errors`，含每槽位的 `slot_name` / `code` / `message`；样例将其映射为 `TASK_STATE_REJECTED`
而不是让服务崩溃 ——
[`server_flow.py:375`](src/server/server_flow.py#L375)：

```python
if validation_failure is not None:
    await emit_status_update(
        request_context=request_context,
        event_queue=event_queue,
        context_id=resolved_context_id,
        task_id=resolved_task_id,
        state=TaskState.TASK_STATE_REJECTED,
        text=f"Prompt validation failed: {validation_failure}",
    )
    ...
    return
```

整条流程运行在 `a2a-sdk` 的 `AgentExecutor` 中，它把执行委托给 `execute_server_flow` ——
[`server_main.py:55`](src/server/server_main.py#L55)：

```python
async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
    kwargs = {
        "request_context": context,
        "event_queue": event_queue,
        "prompt_server": self._prompt_server,
        "log_sink": self._log_sink,
    }
    await self._execute_flow(**kwargs)
```

`a2a-sdk` 应用装配（routes、task store）见
[`src/server/server_main.py`](src/server/server_main.py)。

## 协商场景（Negotiation-T）

除上述默认流程外，本样例还提供多个协商场景，在同一条流式链路上演示 **Negotiation-T** 协商。支持三类
协商类型，由服务端产生的内容校验错误码数据驱动地选择：

| 内容校验错误码 | 协商类型 | 第 1 轮 propose → 第 2 轮 accept |
| --- | --- | --- |
| `content.param_missing` | 信息协商（information） | 服务端索要缺失参数（`region`）；客户端补全后应答 |
| `content.value_not_allowed` | 目标协商（target） | 服务端提出澄清后的目标请求确认（确认请求轮）；客户端以确认后的意图应答 |
| `content.semantic_conflict` | 可行性协商（feasibility） | 服务端请求对冲突内容做可行性评估（`action=REQUEST_FEASIBILITY_EVALUATION`，携带 `contents_to_evaluate`）；客户端以可行性摘要应答 |

> 该映射是样例层的约定。报文是自描述的：每个 propose 携带各自的 `templateUri`
> （`Negotiation-T/{information,target,feasibility}-negotiation/propose/v1`），客户端基于该 URI
> 分派 accept。

```bash
cd a2a-t-sample/ran-energy-saving
python run_demo.py --scenario negotiation                         # 缺失 region 的输入；信息协商
python run_demo.py --negotiation target                           # 100Mbps 速率超出小区能力；目标协商
python run_demo.py --negotiation feasibility --channel fromText   # 30% 节能目标与 50Mbps 速率冲突；可行性协商
```

各参数的默认值（`default` 场景、`information` 类型、`fromData` 通道）保持上文流程不变；协商类型与
通道也可以通过 `.env` 中的 `A2AT_SAMPLE_NEGOTIATION_KIND` / `A2AT_SAMPLE_CHANNEL` 选择
（见[配置](#配置)）。

**第一轮——服务端发起 propose。** 客户端用 `generate_task_prompt_from_text`
（[`client_flow.py:448`](src/client/client_flow.py#L448)）从自然语言输入生成 Task-T prompt。服务端用
`validate_task_prompt_and_data_filling`（[`server_flow.py:455`](src/server/server_flow.py#L455)）校验后
得到按槽位命名的拒绝；错误码选择协商类型（见上表），服务端用
`generate_negotiation_propose_prompt_from_data`
（[`server_flow.py:285`](src/server/server_flow.py#L285)）从类型化内容（`a2a_t.negotiation.content.models`
中的 `InformationProposeContent` / `TargetProposeContent` / `FeasibilityProposeContent`）渲染对应的
Negotiation-T propose——fromData 通道下渲染不涉及 LLM 调用——并发出 `TASK_STATE_INPUT_REQUIRED`，
propose 放在该状态消息的 metadata 中（`build_metadata_content()`）。

**第二轮——客户端 accept。** 客户端从 INPUT_REQUIRED 的 metadata 中读取 propose 并解析
`negotiationContext` 会话，然后基于 propose 的 `templateUri` 分派应答的 ending 内容——信息协商：
`InformationEndingContent`（补全后的应答项）；目标协商：`TargetEndingContent`（`ACCEPT` +
`confirmed_intent`）；可行性协商：`FeasibilityEndingContent`（`ACCEPT` + `feasibility_summary`）。
客户端用 `generate_task_prompt_from_data_with_schema`
（[`client_flow.py:302`](src/client/client_flow.py#L302)）从补全后的参数重新生成 Task-T prompt，用
`generate_negotiation_accept_prompt_from_data`
（[`client_flow.py:317`](src/client/client_flow.py#L317)）渲染 accept，然后把两个 prompt 一并放进
消息 metadata 重新发送同一任务。服务端检测到 Negotiation-T URI 即视为协商续轮，校验补全后的
prompt，流式推送 UC1 进度与报告 artifact，最后以 `TASK_STATE_COMPLETED` 结束任务——协商运行到此
结束，而默认流程保持长连接运行。

**fromText 通道。** 选择 `--channel fromText`（`A2AT_SAMPLE_CHANNEL=text`）时，每条协商消息先渲染为
自然语言文本，再由 SDK 从文本中抽取结构化内容：服务端调用
`generate_negotiation_propose_prompt_from_text`
（[`server_flow.py:282`](src/server/server_flow.py#L282)），客户端调用
`generate_negotiation_accept_prompt_from_text`
（[`client_flow.py:311`](src/client/client_flow.py#L311)）——每条生成的协商消息多一次抽取 LLM 调用
（使用内置 mock LLM 时表现为额外的 `llm-mock` 日志行）。报文格式与 `fromData` 完全一致。各类型的
消息形态：目标协商的 propose 携带协商摘要与确认请求，accept 携带确认后的意图；可行性协商的 propose
携带评估请求与 `contents_to_evaluate` 条目，accept 携带可行性摘要。

> **语言行为：** 协商消息文本（propose 摘要、确认请求、可行性条目、accept 摘要）跟随运行语言
> （`--language`）；UC1 进度与报告文案不随语言变化，保持内置英文。

报文约定：

| 内容 | 位置 |
| --- | --- |
| propose（第 1 轮）/ accept（第 2 轮）文本 | 消息 `metadata` 中 Negotiation-T 扩展 URI 之下（`https://projects.tmforum.org/a2aproject/telecommunication/extensions/Negotiation-T/v1`） |
| 协商会话 | 嵌套的 `negotiationContext` metadata map（`id` / `round` / `maxRounds` / `performative`） |
| 第 2 轮续轮信号 | 第 2 轮消息 `metadata` 中出现 Negotiation-T URI；`task_id` / `context_id` 沿用第 1 轮 |
| 第 2 轮 `A2A-Extensions` 头 | 同时携带两个扩展 URI（Task-T **与** Negotiation-T），逗号拼接 |

**预期输出**（已裁剪）：

```text
[client] a2a-event: {"task": {"id": "...", "status": {"state": "TASK_STATE_SUBMITTED", ...}}}
[client] a2a-event: {"statusUpdate": {"status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"metadata": {"negotiationContext": {"round": 1.0, ..., "performative": "PROPOSE"}, ".../extensions/Negotiation-T/v1": "## Information Negotiation ...", "templateUri": "Negotiation-T/information-negotiation/propose/v1"}}}}}
[client] negotiation-accept: generated (round=2) template_uri=Negotiation-T/information-negotiation/accept-reject/v1 items=[('region', 'Songshanhu Administration Committee')]
[client] a2a-send-message: round=2 streaming text_part=create ran energy saving task metadata_keys=[<Task-T/v1>, <Negotiation-T/v1>, templateUri, negotiationContext] header[A2A-Extensions]=<Negotiation-T/v1>,<Task-T/v1>
[client] a2a-event: {"artifactUpdate": {"artifact": {"name": "energy saving intent report", ...}}}
[client] a2a-event: {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED", ...}}}
[client] stream-completed: events=14 artifacts=1
```
