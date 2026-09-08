# Python Backend Guide

## 入口与生命周期

Python Backend 有两个显式入口，均只监听 `127.0.0.1`：

```powershell
python -m harness_shell_sidecar serve --port 8765 --data-dir E:\absolute\dev-data
harness-shell-sidecar.exe desktop --port 0 --data-dir E:\absolute\data --control-read-handle <n> --ready-write-handle <n>
```

`serve` 只用于源码、Python-only 和 SSH Lab；port 必须为 1..65535。`desktop` 只由 Launcher 使用，要求 port 0、绝对 data directory 与两个 inherited Windows handles。不得增加 host override、默认 data dir、自动端口 fallback 或第二 transport。

FastAPI ASGI lifespan 从 `RuntimeSettings` 创建唯一 `RuntimeResources`，完成 database/repository/SSH/PTY/Manual SFTP/Agent/dispatcher 初始化后才接受请求。失败不发布部分 owner。shutdown 只执行一次并继续清理所有 owner，同时保留首个错误。

## 目录职责

- `web/`：Uvicorn、lifespan、typed HTTP/WebSocket gateway、Agent SSE encoder/session/startup barrier、Problem Details 与 OpenAPI export。
- `runtime/`：settings、resources、dispatcher、request context 与 Desktop control pipe。
- `storage/`：同步 SQLAlchemy Engine/短 Session、Alembic 启动升级、STRICT 自检与 plaintext records。
- `credentials/`：request envelope 解封、kind-checked plaintext credential repository、temporary secret cleanup；不提供独立 credential mutation route。
- `ssh/`、`terminal/`：SSH/ProxyJump/Host Key/PTY owner。
- `manual_sftp/`：remote-only listing、mutation、temporary/commit/abort/recovery；不得读取或写入本地用户文件。
- `agent/`：Provider metadata、Python credential lookup/zeroize、conversation/run/message、durable stream lifecycle、实时发布可见文本及修订的 model gateway 与 strict `execute_command` loop。

`ModelGateway` 使用显式 `ModelApiConfig.api_type` 在官方 `openai==3.6.0` SDK 的 `AsyncOpenAI.responses.create(stream=True)` 与 `AsyncOpenAI.chat.completions.create(stream=True)` 之间选择。两种 API 各自拥有 request mapper 和 typed stream parser，最终统一返回 `langchain-core` `AIMessage`；禁止自动探测、协议 fallback 和 SDK 内部 retry。每次 invocation 独占一个 client，每次 attempt 独占一个 stream，并在成功、失败和取消时关闭。Responses replay items 按严格 JSON 语义校验，只有 `api_config_id` 相同时回放。

Chat Completions 与 Responses 接收采用类似 Open WebUI 的宽松聚合规则：SDK 对象转为普通字段映射，不做全字段 Schema 复验。Chat 只消费第一个 choice，忽略空 choices / 未识别元数据，并在空 choices chunk 中独立提取有效 usage；不要求 finish_reason、下标或重复终止标记满足严格状态机。Responses 按 SSE 到达顺序聚合，忽略未知事件和未消费的元数据；item 身份优先、下标其次，允许缺少序号、下标、冗余 name、status 和部分生命周期事件。done 更新已收集字段，非空最终 output 替换累积 output，空最终 output 保留累积值；不再比较 delta/done/final 的逐字段相等性。正常 EOF 结束聚合，重复完成与完成后元数据不报协议错误；明确 Provider error / response.failed 和传输异常仍传播。

普通模型调用实时发布可见文本，包括工具调用前的说明。前缀扩展发送 text_delta，done / final 或 Chat message 修订通过 text_replace 更新完整显示内容；大快照以最多 4096 字符的替换首帧及后续增量编码，仍遵守 SSE 字节预算。工具执行期间保留说明，下一次模型调用首段文本替换上一调用内容，成功时显示文本仍与最后一条 AIMessage 一致。已发布任何可见更新后不再重试网络超时；尚未发布文本时维持原有有界重试。publisher 异常身份和取消传播不变。内部摘要仍聚合完整结果并校验完成状态，不发布可见内容。工具参数允许 JSON 对象、JSON 字符串和安全解析的 Python literal 字典，并统一规范为 JSON；不能解析为对象或缺少函数名仍失败，不执行猜测的命令。工具名、参数字段和命令安全由现有 execute_command / Agent graph 边界检查。Responses 本地 Replay 仍只保存支持的 message / reasoning / function_call，按严格本地 JSON Schema 和 api_config_id 校验；不将未知 hosted-tool 元数据转成可执行命令。

节点观测将 asyncio.CancelledError 和 AgentCancelled 作为控制流，以 INFO 记录 agent_node_cancelled 并原样传播，由 Service 持久化 Run 取消终态；真实节点异常继续记录 ERROR 和 traceback。

## Agent 上下文工程

用户显式重试复用 `user_message_id`，通过 `retry=true` 发起；每次执行仍创建新的 Run。`AgentService` 先按用户消息身份、再按会话身份加锁，处理 started 丢失时的会话定位。已有尝试必须为当前会话最后一个终态 Run，且用户正文一致；在创建新 RUNNING 的同一事务内删除旧 Run 的消息正文/索引和以该 Run 为源的摘要，保留旧 Run 元数据，后续图重新构建历史修复与摘要。尚未落库的尝试没有历史可替换，可按同一消息身份重新发送；不猜测最后一条相似文本。旧命令不撤销、旧审核授权不复用。`0002_agent_retry` 为 `agent_runs` 添加 nullable 用户消息关联和索引，既有 Run 保持空关联。

`RuntimeResources` 在启动时创建 `AgentContextPolicy` 和本地 tokenizer/`ContextBudget`；graph 借用同一数据库创建 `ContextSummaryRepository` 与 `ContextCompactor`。`context_models.py` 定义序号记录、摘要、预算来源和安全错误；`context.py` 负责修复与有效投影；`context_budget.py` 负责估算和预算；`context_summaries.py` 负责短事务；`context_compaction.py` 负责一次有界摘要流程。

调用链固定为 `load_context → compact_context → prepare_model_context → call_model`；工具执行后仅回到 `prepare_model_context`。新 HumanMessage 入库后只检查一次压缩。按 Human 边界保留最近 3 个完整历史轮次及当前用户轮，修复 ToolMessage 归属其前一历史轮；未摘要的历史不会按固定轮数丢弃。模型投影为 canonical System Prompt、可选的带历史数据标记的摘要 HumanMessage、覆盖边界后的完整消息。

每个 Provider 保存 `context_window_size=128000`、`context_compaction_threshold_ratio=0.75`、`max_output_tokens=8192`，本轮沿用冻结配置。达到 `floor(window * ratio)` 触发；输入上限是 `window - max_output_tokens`，等号允许。主模型和摘要分别通过 Chat `max_completion_tokens` / Responses `max_output_tokens` 实际预留输出。轮内超预算以 `CONTEXT_BUDGET_EXCEEDED` 结束，不进行摘要。

主模型响应保存规范化 usage 及 `harness_context_anchor`。从最新有效消息向前匹配 Provider/config/input 静态指纹与摘要 revision，使用 input+output+其后新增消息的映射估算，不重复计算锚点回复。没有兼容 usage 时估算 System、摘要、当前有效历史和工具定义的实际协议映射；本地 `o200k_base` 仅为估算，不声称各 Provider 精确一致。摘要自身的 usage 不进入主对话锚点。

摘要复用本轮 Provider/model/key，走独立 `summarize_once`，无工具、无 UI sink。每次实际请求整体 60 秒，最多 3 次，失败间隔 1/2 秒且可取消；不复用主模型超时重试。摘要只包含有用历史，剔除 usage/anchor/opaque replay。摘要输入先验预算、候选投影再次验预算，通过后 CAS revision 并原子替换唯一摘要；历史消息不变。第三次失败以 `CONTEXT_COMPACTION_FAILED` 终止当前轮，下一轮可重新尝试。取消、预算、数据库错误直接传播；已提交摘要在随后主调用失败时保留。

工具 envelope schema 2 的 `stdout`、`stderr` 分别仅保留首部 6000 个 Unicode code points，由 backend policy 控制。`stdout_truncation` / `stderr_truncation` 包含 original/retained/omitted 字符数和 truncated，字符串不混入裁剪提示。裁剪后的同一 envelope 用于数据库和模型；模型需继续查询后续输出。此限制发生在命令输出收集后，不是远程读取内存上限。

本地资源 loader 只使用固定位置及 `o200k_base`，校验 ranks SHA-256 与固定样本；启动失败为 `CONTEXT_TOKENIZER_UNAVAILABLE`，不得运行时下载、借用 cache 或换 encoding。资源准备和 packaged 验证命令见 [Testing Guide](testing.md)。

## 存储

`RuntimeDatabase.open` 在资源发布前执行包内 Alembic 升级：全新数据库建立 `0001_initial`，已知 revision 升至唯一 head；旧 schema v7 和未知 revision 只读拒绝。迁移专用连接先关闭 foreign_keys，再 `BEGIN IMMEDIATE`，全部 revision、版本号、schema/STRICT/约束/索引和 integrity/foreign_key_check 共享一个真实事务；失败整体回滚并终止启动。成功后业务 Engine 启用 WAL、foreign_keys 和固定 busy_timeout。没有自动备份、恢复、降级或旧库导入。

Runtime 长期只持有 Engine/Session 工厂。repository 构造接收 Session，不 commit/close；handler、Agent 数据库阶段和 SFTP operation store 拥有 `read_session`/`write_session`。读作用域退出回滚；写作用域使用 `BEGIN IMMEDIATE`，flush 后关闭 Session，由外层 Connection 提交。禁止跨 SSH/模型/SSE await 保留 Session，禁止把 ORM 行对象作为领域返回值。资源关闭前必须归还全部 Session。

revision 必须独立声明历史 DDL，不能导入当前 ORM metadata；修改 ORM 时同步编写并审查 revision。batch 重建显式保留 STRICT、未命名 CHECK、索引和外键，不能依赖 autogenerate 完全保留；已验证 SQLite 内联外键的 ondelete 反射可能丢失，重建时使用显式历史表定义（`copy_from`）保留删除动作。禁止 revision 内 commit、autocommit、VACUUM 或文件副作用；自检失败不得自动修复 schema。

`agent_context_summaries` 每会话至多一条，保存 revision、covered_through_sequence、summary_text、source_run_id 和时间戳；读取校验真实历史/工具边界，损坏以 `CONTEXT_SUMMARY_INVALID` 失败。conversation 外键级联删除摘要；不回写或替换 `agent_messages`。

`runtime_records.payload` 与 credential records 是 plaintext。不要通过命名、注释或文档暗示 at-rest encryption。新增持久化内容时必须明确字段、nullability、删除、敏感性、schema self-check 和测试；没有业务读取或导出闭环的诊断数据不得新增 SQLite 表。

## Protocol 约束

- route 在进入 dispatcher 前完成 strict Pydantic/header/media-type/body-size 校验。
- Agent turn route 要求 `Accept: text/event-stream`。worker 在 shared dispatcher 内取得 conversation lock、创建 durable RUNNING Run、建立 capacity 64 queue 并入队 started 后才允许 HTTP 200；consumer 发送 terminal frame 后才释放 dispatcher request ID/capacity，断连与 shutdown 都必须取消并 await worker。route 在 HTTP 200 启动屏障前也监听 ASGI disconnect，取消并等待启动任务与 worker，沿用 AGENT_CANCELLED Problem 映射；正常启动后停止该监听，再交由 StreamingResponse 独占断连接收。SSE session 取消 worker 后，在 AnyIO shield scope 内等待其退出，防止已取消的 HTTP scope 通过 gather 再次取消正在关闭 SDK/SSH 资源的 worker。
- Agent SSE 固定 frame 65,536 bytes、body 4,194,304 bytes、terminal reserve 65,536 bytes；producer awaited put，不 drop/merge/truncate。完整 Agent result 的 1,048,576-byte 逻辑预算继续生效。
- Manual SFTP chunk 固定最大 262,144 bytes，使用 raw `application/octet-stream` 和 exact offset/operation identity。
- WebSocket inbound/outbound queue capacity 固定且不 drop/merge；只有 strict ping 刷新 heartbeat。
- credential、command、model response body/text、stdout/stderr、SFTP bytes 和 HTTP body 不得主动进入日志或 Problem detail；Provider failure 日志显式字段只允许 stable metadata，异常 traceback 遵循 Python Style Guide。
- Connection 与 Provider handler 必须在同一 `RuntimeDatabase` 事务中维护业务记录及其拥有的 credential；更新省略 envelope 时保留现有引用，删除业务记录时同步删除 credential。
- 未知字段、stale profile/session、duplicate owner、取消和 cleanup failure 都显式失败，不重放远程 mutation。

## 日志与异常诊断

- stderr console 格式固定为 `yyyy-MM-dd HH:mm:ss.SSS | LEVEL | reqId | thread | logger | message`，timestamp 使用设备本地系统时间，stdout 保持为空；请求外日志的 `reqId` 列为空。源码 `serve` 模式使用 ANSI 为 timestamp、level、thread 与 logger 分级着色；`desktop` 模式保持无 ANSI 的纯文本，并由 Launcher 原样写入独立 Backend 轮转日志。
- HTTP access middleware 在 response 完成后直接调用标准 Logger；除 `GET /v1/runtime/state` 轮询接口不打印 access log 外，每个请求只记录 method、route template、实际返回 status 和 duration。它不记录 raw path、query、headers 或 payload；无法匹配 route 时使用 `<unmatched>`。
- HTTP `2xx/3xx` 使用 INFO，`4xx` 使用 WARNING，`5xx` 使用 ERROR。Uvicorn native access log 关闭，原生启动细节在 WARNING threshold，避免和应用日志重复。
- Agent Run start/terminal lifecycle 保留 INFO 或 ERROR；node start/completion 与 route decision 属于 DEBUG。Provider、node 和 unexpected HTTP failure 使用 ERROR；捕获异常时的日志调用及 traceback 规则遵循 [Python Style Guide](python-style.md#异常与失败传播)。
- 具有稳定 error code 的领域异常必须同时携带每个 raise point 的具体、经过安全审查的 `safe_message`，不得只用 error code 作为异常文本；已知异常的外部 Problem/SSE 原样使用该 `safe_message`，不得再按 error code 替换。未知异常仍使用固定安全内容，禁止复制任意异常文本。

## 验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\build_sidecar.ps1
```

Python tests证明源码行为；packaged smoke证明本次 `.exe` 的局部 loopback 行为；SSH Lab、Desktop、真实 Provider 与生产主机必须分别验收。

Agent graph 在工具参数校验与安全审查通过后、调用执行器前，经本轮 event sink 发布 `tool_started` 状态。该事件携带工具调用 ID、名称与已校验的完整参数，不新增持久化记录；事件发布失败时不派发命令。协议与展示区间以 [HTTP 契约](../protocol/http/README.md) 为准。


## Agent 人工审核所有权

`command_policy.py` 使用确定性的封闭字面量语法识别明确只读命令，其余 REQUIRE_APPROVAL；原危险命令硬阻断保留且不可通过审核覆盖。不得依赖模型声明只读或改写命令。

`AgentService` 每 Run 创建 LangGraph `InMemorySaver`，原 SSE worker 在 `prepare_tool → await_approval → execute_tool → record_tool_result` 间恢复。`await_approval` 为纯 interrupt 节点，重跑不执行 I/O；GraphInterrupt 只记 DEBUG 节点中断，不当作 ERROR。`ApprovalRegistry` 只拥有冻结请求、原子决定和一次消费标志，不拥有 worker。决定 HTTP 不调用 graph、不创建新 Run、不返回执行结果。拒绝写配对 COMMAND_REJECTED_BY_USER ToolMessage 并继续模型循环。

审核无限等待，监听取消与原 SSH/ProxyJump 失效；冻结目标来自活动 SshSession 而非后来编辑的配置。SSH 移除立即通知失效，借用的 wait_closed 监听只取消自身。派发前检查原调用参数、Session 与取消，并消费一次授权；执行器再次检查传输可用性。Run 退出在 finally 释放审核记录，并同步删除纯内存 saver 的 thread、writes、blobs；不允许 pickle fallback，不跨重启恢复或复用授权，业务 SQLite 历史仍按原规则持久化。

shared dispatcher 保留普通请求 16 容量，只有固定 `agent.approvals.decide` 使用额外 1 个 control 槽；两类共用 request ID 表、取消和 shutdown owner。
