# Rust Core Guide

## 何时必须读取

修改以下内容时必须读取本文档：

- `frontend/src-tauri/src/` 中的 Rust 代码；
- Tauri command 注册、capability、permission 或窗口暴露面；
- DPAPI Vault、Sidecar 进程、Broker、Supervisor、Job Object 或 RuntimeStatus；
- Rust Protocol codec/model、WebView 事件或 packaged Sidecar 测试。

还必须读取 `frontend/src-tauri/AGENTS.md`。涉及跨进程数据或敏感信息时同时读取 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

Rust Core 是桌面应用的特权边界，负责：

- 注册和验证 WebView 可调用的 Tauri commands；
- 用 capability/permission 控制每个窗口的最小权限；
- 通过 DPAPI Vault 管理持久化凭据和运行时密钥；
- 通过专用模型 API Key kind 与 Agent Tauri commands 注入最短生命周期的 secret frame；
- 启动、监督、停止 packaged Python Sidecar；
- 将 request 与 response 通过 `request_id` 精确关联；
- 只向 WebView 发布白名单内、已投影和脱敏的事件与状态。

Rust Core 不应复制 Python Sidecar 的 SSH/PTY 领域逻辑，也不得把原始 transport 或秘密暴露到 WebView。

## 当前源码真源

- Tauri 构建和 command 注册：[frontend/src-tauri/src/lib.rs](../../frontend/src-tauri/src/lib.rs)
- 持久化日志策略：[frontend/src-tauri/src/logging.rs](../../frontend/src-tauri/src/logging.rs)
- App runtime state：[frontend/src-tauri/src/app_state.rs](../../frontend/src-tauri/src/app_state.rs)
- Commands：[frontend/src-tauri/src/commands/](../../frontend/src-tauri/src/commands/)
- 固定日志目录 commands：[frontend/src-tauri/src/commands/diagnostics.rs](../../frontend/src-tauri/src/commands/diagnostics.rs)
- Agent commands：[frontend/src-tauri/src/commands/agent.rs](../../frontend/src-tauri/src/commands/agent.rs)、[frontend/src-tauri/src/commands/credentials.rs](../../frontend/src-tauri/src/commands/credentials.rs)
- Protocol：[frontend/src-tauri/src/protocol/](../../frontend/src-tauri/src/protocol/)
- 用户手动 SFTP coordinator、严格模型、typed transfer-progress sink、私有 wire、本地文件 owner 与 DPAPI journal actor：[frontend/src-tauri/src/sftp/](../../frontend/src-tauri/src/sftp/)
- Sidecar lifecycle：[frontend/src-tauri/src/sidecar/](../../frontend/src-tauri/src/sidecar/)
- Vault：[frontend/src-tauri/src/vault/](../../frontend/src-tauri/src/vault/)
- Capabilities：[frontend/src-tauri/capabilities/](../../frontend/src-tauri/capabilities/)
- Permissions：[frontend/src-tauri/permissions/](../../frontend/src-tauri/permissions/)
- Rust contract tests：[frontend/src-tauri/tests/](../../frontend/src-tauri/tests/)
- Crate manifest：[frontend/src-tauri/Cargo.toml](../../frontend/src-tauri/Cargo.toml)

## 项目结构规范

- `src/commands/`：按领域组织 Tauri commands；负责 WebView 边界校验、Vault 取值和向 Broker 构造请求。
- `src/sidecar/`：process extraction/start、Windows Job Object、Broker、RuntimeStatus 和 Supervisor；进程与 pending reply 生命周期只在此层管理。
- `src/protocol/`：Protocol v1 envelope、codec 和严格验证；不包含 SSH 业务逻辑。
- `src/sftp/`：用户手动 SFTP 的 Rust coordinator、跨进程严格模型、私有 runtime client、本地文件 owner 与 DPAPI-protected journal；coordinator 通过注入的 typed sink 发布 transfer projection，production sink 由 `lib.rs` 固定绑定 `main` window 与 `manual-sftp://transfer-state`。
- `src/commands/sftp.rs`：21 个用户手动 SFTP typed commands；每个 command 在访问 coordinator 或 native dialog 前校验固定 `main` window。上传/下载 picker 由 Rust 打开，取消返回 `None`，本地绝对路径只进入不可序列化的 coordinator input。
- `src/commands/agent.rs`：四个 Provider metadata CRUD commands 与一个 turn command；turn 先读取当前 config，再从 Vault 解析专用 API Key kind，并只通过 `agent.turn.run` secret frame 传给 Sidecar。`commands/credentials.rs` 另提供两个模型 API Key store/delete commands，因此 Agent 后端新增七个 main-window Tauri commands。
- `src/logging.rs`：在 Runtime setup 前安装 INFO logger，目标固定为启动终端与 Tauri `LogDir`；setup 开始时写一条无业务字段的 Core 启动记录，单个活动文件上限 10 MiB，保留四个 archive 加一个 active `harness-shell.log`。
- `src/commands/diagnostics.rs`：只解析 Tauri `app_log_dir()`，提供无参数 `get_log_directory` / `open_log_directory`；后者仅以单个 `Path` argument 启动 `explorer.exe`，不接受 WebView path 或 shell command string。
- `src/vault/`：DPAPI-backed secret storage、secret type 和清理逻辑；不得提供 WebView 可序列化的原始 secret type。
- `capabilities/`：按固定窗口分配权限集合。
- `permissions/`：按 command 领域维护 permission set；生成 permission 是产物，不手工作为业务真源。
- `tests/`：跨模块契约测试，覆盖 Broker、capability、Protocol、Supervisor、Vault 和 packaged Sidecar。

新增 privileged capability 时，先确定所属窗口和最小 command 集，不得通过扩大现有 capability 规避设计。

## 代码规范

- 公共 type、关键字段和安全边界使用 rustdoc 或邻近注释说明职责、不变量和敏感性。
- command 输入先验证，再读取或注入秘密；调用方不得主动把秘密传给 `Debug`、日志、event 或普通 error details，日志基础设施不提供内容过滤。
- 新增 command 必须同时更新：实现模块、`commands/mod.rs` re-export、`lib.rs` handler 注册、permission、capability、Frontend typed wrapper 和对应测试。
- Broker request 使用唯一 `request_id` 和 oneshot reply；未知、重复或类型错误的 response 必须 fail closed。
- Supervisor 状态转换和退出原因显式发布；不得把 Sidecar 失败转换为 READY，不自动重放不确定请求。
- async task、channel、child process、temporary extraction 和 Job Object 必须有明确 owner、关闭顺序和错误传播。
- 锁中只做最小同步工作；不得在持锁期间执行阻塞 I/O 或跨进程 await。
- 错误通过稳定 code 与安全 message 返回；内部 Logger 完整记录调用方提交的 message 与 exception。

## 长期约束

- Rust Core 独占 Sidecar 进程和 DPAPI Vault，不允许 WebView 或 Python 直接管理这两个边界。
- Sidecar ready/initialize 握手成功后才能发布 READY。
- Runtime heartbeat 当前为 5 秒，timeout 为 15 秒；变更时必须同步 Rust、Python、Protocol 和测试。
- Manual SFTP mutation dispatch 由单一 actor 串行拥有。Tauri caller 超时或 drop 不能取消已经派发的 broker request；actor 必须等待真实 response、收敛 encrypted journal，再执行排队的 transfer abort，避免 forward/cleanup 并发。
- Sidecar typed error 可携带受限 `operation_state=cleanup_required|outcome_unknown`；这两种状态必须保留 local recovery record，普通可信 terminal failure 才写 failed 后删除。
- `RuntimeRequest` 的 `Debug` 必须持续脱敏 payload。
- Sidecar stderr 由 Rust process owner 逐行原样转发；仅解析 `component=python_sidecar` 与 `level=INFO|WARNING|ERROR` 选择日志严重级别，不扫描业务字段、不按内容丢弃，也不设置单行截断上限。
- 主窗口的 `diagnostics` permission 只含两个固定日志目录 commands；approval window、通用 shell、filesystem 或 dialog plugin capability 不得因此扩大。
- 模型 API Key 只存在于 Rust DPAPI Vault；Sidecar metadata 仅保存 opaque secret reference。Rust 解析后使用 canonical Base64 发送 secret frame，不得进入 `Debug`、event、普通 error 或持久化明文。
- Agent command 必须先读取当前 `agent.api_configs.get` 结果，再解析该 config 指向的专用 API Key；Sidecar 将该完整非秘密 config 作为 handler 快照，并在 conversation lock 内、创建 Run 和 Provider 调用前再次与持久化值逐字段比较。配置发生竞争变化时以 `MODEL_API_CONFIG_CHANGED` fail closed。
- Connection command 将 Sidecar 返回的 `version` 建模为 `u64`；解析目标和跳板凭据后只能发送 JSON 数字 `profile_version`，不得发送或回退到 `profile_updated_at`。
- WebView runtime event 白名单以 Broker 验证为真源；新增 event 必须显式设计、投影和测试。
- Rust-local manual SFTP transfer event 不经过 Sidecar Broker：production 必须显式注入 main-window sink，sink 只接受 `TransferProgressProjection`。event emit 失败记录 path-free stable diagnostic，但不能使已 dispatch transfer 失败或触发 replay。
- manual SFTP transfer future 在 remote begin 后被取消或丢弃时，由 mutation-dispatch actor 持有 detached cleanup；该 owner 必须覆盖 remote abort、本地 part 清理与 journal terminal/unknown 收敛，并计入 coordinator bounded shutdown drain。
- manual SFTP mutation 的 remote target/source snapshot 与 regular-file SHA-256 由 Rust 在 dispatch 前重新取得，不接受 WebView snapshot。已存在的本地下载目标只允许在支持的 NTFS TxF 路径上以受锁事务句柄完成 compare-and-atomic-replace；事务不可用时 fail closed，不降级为普通 rename。
- manual SFTP local coordination journal 只保存 DPAPI-protected metadata，包括安全 host label、remote path、目标 basename 所需信息和必要本地恢复身份；Sidecar remote operation/delete-plan records 只保存 AES-GCM ciphertext。重启后的 download `.part` inspect/keep/open-folder 由 Rust local-file actor 根据加密记录派生并验证，不调用 Python；本地绝对路径仍不得投影给 WebView。完整自动验证入口是仓库根的 `scripts\verify-manual-sftp.ps1`，其通过仍不等于 Tauri Desktop 人工验收。
- coordinator 以 `ssh_session_id` 记录 active transfer owner；匹配 Session 的 disconnect 和 Tauri `ExitRequested` 在 transfer 收敛前必须被阻止。pre-commit 可显式 cancel 并等待 cleanup，committing 不可取消，cleanup 失败必须保留 recovery record 后才允许用户继续关闭。
- 当前主窗口和审批窗口采用不同 capability；审批窗口不得获得主窗口 SSH/terminal 权限。
- 异常退出或 heartbeat 超时保持显式 PAUSED/FAILED 语义；不通过隐式自动重启伪造成功。
- `frontend/src-tauri/binaries/*.exe` 和 `gen/schemas/` 是生成物，不作为手工源码提交依据。

## 项目命令

从仓库根运行 Rust 全目标测试：

```powershell
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

需要验证 packaged Sidecar 契约时，先构建当前二进制并显式设置路径：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\build_sidecar.ps1
$env:HARNESS_SIDECAR_EXE = (Resolve-Path 'backend\dist\harness-shell-sidecar.exe').Path
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets --quiet
```

输出名称以当前 `backend/scripts/build_sidecar.ps1` 为准；打包规则变化时必须先重新核对路径，不能猜测产物。

Tauri 桌面开发和环境信息从 `frontend/` 运行：

```powershell
npm run tauri:dev
npm run tauri info
```

## 验证要求

- Command：验证输入、失败 code、secret 路径、handler 注册、permission/capability 和 Frontend wrapper。
- Protocol/Broker：运行 `protocol_contract.rs`、`broker_contract.rs`，覆盖 request 关联、未知事件、invalid frame 和 pending reply 清理。
- Vault：运行 `vault_contract.rs`，覆盖 DPAPI 密文、runtime key、删除和 secret non-exposure。
- Supervisor：运行 `supervisor_contract.rs` 和 packaged Sidecar 契约，覆盖 ready、heartbeat、shutdown、异常退出和资源清理。
- Capability：运行 `capability_contract.rs`，确认 main/approval 窗口没有越权。
- 涉及真实窗口生命周期、焦点或关闭行为时，Rust 测试后仍需 Tauri 桌面验收。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- command 注册流程、capability/permission 或窗口权限改变；
- Vault 格式、secret type、DPAPI 使用或运行时密钥生命周期改变；
- Broker、Supervisor、RuntimeStatus、heartbeat、timeout 或重启策略改变；
- Rust module 或契约测试布局改变；
- packaged Sidecar 构建路径、环境变量或 Rust 测试命令改变。

局部 Rust 实现重构未改变安全边界和公共契约时无需更新，但必须执行 AGENTS 影响检查。
