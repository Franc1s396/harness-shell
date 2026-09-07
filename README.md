# Harness Shell

简体中文 | [English](README.en.md)

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用。项目以人工 SSH 终端为核心，在显式连接、Host Key 信任和远程会话边界内提供 Manual SFTP，以及实验性的 ReAct Shell Agent。

当前版本为 `0.2.1`，仍处于开发与验证阶段。适用于需要在本机管理 SSH 连接、操作远程终端、手动传输文件，并在已连接会话中尝试 AI 辅助运维的开发者。

项目将人工终端、文件传输和 Agent 放在同一桌面工作区；使用 SSH/SFTP 不需要配置模型 Provider，使用 Agent 时才需要模型服务。

## 功能特性

- SSH 连接配置、显式 Host Key 信任、直连与单层 ProxyJump。
- 多标签人工 PTY，输入和运行时事件通过单个 Runtime WebSocket 传输。
- 仅由用户显式发起的 Manual SFTP，包括上传、下载、远端临时文件、commit、abort 和 recovery。
- 实验性 ReAct Shell Agent：每个 turn 固定所选 Provider 与已连接 SSH Session，只允许严格的 `execute_command` 工具。
- Agent 可见文本（含工具前说明）通过独立 SSE response 流式返回，支持完整文本更新，不复用 Runtime WebSocket。
- 显式选择 Chat Completions 或 Responses API，支持 Provider 上下文预算、滚动摘要和工具输出首部裁剪。
- 简体中文、繁体中文和英文界面。

以下能力尚未完成或不能由现有自动验证证明：真实 Provider 全矩阵、完整安装版 Desktop matrix、服务端审批、自动恢复、生产 SSH、部署和旧数据迁移。自动测试、构建、打包或容器 OpenSSH Lab 通过，不代表这些场景已经验收。

## 技术栈

| 层 | 技术与用途 |
| --- | --- |
| 前端 | React 19、TypeScript 5.8、Vite 7、Tailwind CSS 4；Zustand 管理状态，i18next 提供国际化，xterm.js 渲染终端 |
| 桌面 | Tauri 2 最小 UI shell；Rust Launcher 使用 Windows Job 和匿名管道管理子进程 |
| Backend | Python ≥3.12、FastAPI、Uvicorn、Pydantic、AsyncSSH |
| Agent | LangGraph、langchain-core、OpenAI Python SDK、tiktoken |
| 存储 | SQLite STRICT、SQLAlchemy 2.0、Alembic |
| 测试与打包 | Pytest、Vitest、Testing Library、Cargo tests、PyInstaller、NSIS；Docker Compose 提供 SSH Lab |

依赖范围见 [package.json](frontend/package.json)、[pyproject.toml](backend/pyproject.toml) 和各 Rust `Cargo.toml`；锁定版本以对应 lock 文件为准。

## 架构

生产桌面路径固定为：

```text
harness-shell-launcher.exe
  -> harness-shell-sidecar.exe desktop --port 0
  -> Backend 绑定动态 127.0.0.1 端口并发送 ready frame
  -> harness-shell-ui.exe --backend-url http://127.0.0.1:<port>
  -> React 直连 Python typed HTTP、Agent SSE 与 Runtime WebSocket
```

各组件的职责边界如下：

| 组件 | 主要职责 |
| --- | --- |
| Launcher | 独占 packaged Backend/UI child、Windows Job、动态端口协商、ready/control pipe 和退出顺序 |
| Tauri 2 UI shell | 只提供 Backend bootstrap，以及主窗口关闭和销毁权限 |
| React/TypeScript WebView | UI 状态、typed loopback client、Runtime WebSocket、Agent SSE、本地文件选择和 Manual SFTP chunk iteration |
| Python FastAPI Backend | SQLite、凭据、SSH/PTY、远端 Manual SFTP、Agent、dispatcher 和日志 |

Launcher 不扫描端口、不 reconnect、不 respawn。Tauri 不代理业务 HTTP/WebSocket，也不拥有 Backend 生命周期、凭据仓库或文件传输。

## 安全与数据边界

- Backend 只监听 `127.0.0.1`，但 loopback 并不是抵御同一用户会话中恶意进程的完整认证边界。
- React 使用 Backend 公钥将连接密码、私钥和 Provider API Key 包装为 RSA-OAEP-256 + AES-256-GCM request envelope，再随所属业务 mutation 提交。
- Runtime SQLite 启动时通过 Alembic 建立 `0001_initial` 基线，或升级已知 revision；旧 schema v7、无有效版本身份的旧库及未知 revision 会被拒绝。没有旧库导入、自动备份、恢复或降级流程。
- 当前数据库是 plaintext store。凭据、Agent conversation/message/output、remote recovery 和其他业务 payload 可能明文落盘，目前没有 at-rest encryption。
- 连接私钥和 Manual SFTP 本地文件由 React 选择与读取；Python 不接收本地绝对路径。
- 日志调用点不得主动记录秘密、请求正文、模型正文、命令或远程输出；日志层不会自动扫描和清除传入内容。提交问题前仍须人工检查日志。
- Agent 的 `execute_command` 可以修改远程状态；固定危险命令正则不构成完整沙箱，当前没有逐条命令审批流程。请仅用于明确授权的 SSH 会话。

## 目录结构

```text
.
├── frontend/
│   ├── src/                          # React UI、typed API、状态与 i18n
│   └── src-tauri/                    # 最小 Tauri UI shell、bootstrap 与 NSIS 配置
├── launcher/                         # Desktop child、Windows Job 与 ready/control 生命周期
├── backend/
│   ├── src/harness_shell_sidecar/    # FastAPI、SSH、PTY、Manual SFTP、Agent 与存储
│   ├── tests/                        # Python 单元、集成与 SSH 测试
│   └── scripts/                      # Sidecar 打包和 smoke test
├── scripts/                          # 构建脚本与 M1/M2/Manual SFTP/M3 门禁
├── tests/ssh_lab/                    # 隔离的双节点 OpenSSH 容器实验室
├── docs/protocol/http/               # HTTP、WebSocket、SSE 契约与 fixture
├── docs/testing/                     # 自动门禁和人工验收记录
├── docs/agents/                      # 架构与领域维护指南
└── docs/superpowers/                 # 本地规格与实施计划，不纳入 Git
```

## 环境要求

- 当前开发与打包目标为 Windows x64，面向 Windows 10/11；最低系统版本和完整兼容矩阵需要确认。
- 建议 Node.js 22.12+ 与 npm 10；Vite 7 的 Node.js 要求为 `^20.19.0 || >=22.12.0`，项目未单独锁定 npm 版本。
- Python 3.12 或更高版本；可复现 Sidecar 打包和仓库门禁严格要求 Python `3.12.14`。
- Rust stable MSVC toolchain，host 为 `x86_64-pc-windows-msvc`。
- Microsoft C++ Build Tools、Windows SDK 和 WebView2 Runtime。
- Docker Desktop、Docker Compose v2（脚本要求 `docker-compose.exe` 可用）和 Windows OpenSSH `ssh-keygen.exe`，仅在运行 M2、Manual SFTP 和 M3 SSH Lab 门禁时需要。

无需单独安装数据库服务或 Redis。SQLite 存放在本机数据目录；SSH 功能需要可访问的远端 SSH 服务，Agent 还需要可访问的、支持所选 API 类型的模型 Provider。源码依赖安装、首次 tokenizer 准备和首次构建依赖下载需要网络。

## 安装步骤

在仓库根目录执行：

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm.cmd ci
cd ..
```

`py -3.12` 不保证具体补丁版本。需要打包或运行仓库门禁时，请确认虚拟环境使用 Python `3.12.14`，并从仓库根安装锁定依赖：

```powershell
backend\.venv\Scripts\python.exe --version
backend\.venv\Scripts\python.exe -m pip install -r backend\build-requirements.lock
```

首次启动或测试前，从仓库根显式准备离线 tokenizer 资源：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\prepare_tokenizer.py --output-dir backend/build/tokenizer
backend\.venv\Scripts\python.exe backend\scripts\prepare_tokenizer.py --output-dir backend/build/tokenizer --check
```

运行时不会自行下载 tokenizer，也不会借用用户缓存；缺失或损坏会阻止 Backend 就绪。

## 配置说明

项目没有统一 `.env.example` 或 Backend `.env` 加载入口。Backend 使用 CLI 参数，连接和 Provider 通过 UI 配置并存入 SQLite。

| 配置 | 是否必填 | 说明 |
| --- | --- | --- |
| `serve --port` | 是 | 开发端口，范围 `1..65535`；只监听 `127.0.0.1` |
| `serve --data-dir` | 是 | 绝对路径；保存 `runtime.sqlite3` 和 `logs/` |
| UI `--backend-url` | Tauri 路径必填 | `http://127.0.0.1:<port>`；安装版由 Launcher 注入 |
| `VITE_BACKEND_URL` | 浏览器开发必填，Tauri 开发可选 | 仅开发模式读取，设置后优先于 Tauri bootstrap；不得放入任何密钥 |
| `LOCALAPPDATA` | 安装版依赖的系统变量 | Launcher 使用 `%LOCALAPPDATA%\com.harnessshell.app` 作为数据目录 |
| SSH 配置 | 使用 SSH 时必填 | 主机、端口、用户名与认证信息；可选单层 ProxyJump，连接前需完成 Host Key 信任 |
| Provider 配置 | 使用 Agent 时必填 | `display_name`、`api_type`、`base_url`、`model`、API Key；`api_type` 为 `CHAT_COMPLETIONS` 或 `RESPONSES` |
| Provider 预算 | 可选 | `context_window_size=128000`、`context_compaction_threshold_ratio=0.75`、`max_output_tokens=8192`；`enabled` 默认 `true` |

Provider 窗口和输出预算必须按所选模型配置，默认值不是模型能力探测结果。预算必须满足 `1 <= floor(context_window_size * context_compaction_threshold_ratio) <= context_window_size - max_output_tokens`。

不要把 API Key 放到 `VITE_*` 变量中。UI 会把 API Key、SSH 密码或私钥包装成当前 Backend 公钥对应的 credential envelope；这只保护提交过程，不提供数据库静态加密。

构建配置见 [tauri.conf.json](frontend/src-tauri/tauri.conf.json)、[Vite 配置](frontend/vite.config.ts) 和 [打包依赖锁](backend/build-requirements.lock)。

## 使用方法

### 启动开发环境

源码开发需要分别启动 Python Backend 和 Tauri UI。Backend 必须使用固定 loopback 端口和绝对数据目录。

终端 1，在仓库根目录运行：

```powershell
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve `
  --port 8765 `
  --data-dir E:\absolute\harness-shell-dev
```

终端 2，在仓库根目录运行：

```powershell
npm.cmd --prefix frontend run tauri:dev -- -- -- --backend-url http://127.0.0.1:8765
```

这里的两个 Tauri `--` 分隔符分别界定 runner arguments 和 application arguments。不要直接运行 Backend 的 `desktop` 子命令；它只能由 Launcher 通过 inherited handles 启动。

仅启动 Vite 开发服务器可使用：

```powershell
$env:VITE_BACKEND_URL = "http://127.0.0.1:8765"
npm.cmd --prefix frontend run dev
```

这不是完整 Tauri Desktop 路径，并且需要通过 `VITE_BACKEND_URL` 提供合法的 loopback Backend 地址，才能初始化业务 client。

### 日常操作

1. 在连接管理中创建 SSH 配置，填写认证信息，按需要选择跳板。
2. 检查并显式信任 Host Key，建立 SSH 会话后使用终端标签页。
3. 在文件工作区显式选择上传或下载；页面 reload 会丢失本地文件 preparation，不承诺下载续传。
4. 使用 Agent 前添加并启用 Provider，选择已连接 SSH 会话后发送消息。本轮固定 Provider 和 SSH Session。

### 常用命令

以下命令在仓库根运行：

```powershell
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\build_sidecar.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-launcher.ps1
```

`frontend build` 只生成前端资源；完整桌面构建见下文。项目没有统一 lint、format 或云部署命令。

## API 与 CLI

### 本机 API

API 面向本机 UI 集成，不是公开远程服务。请求要求 UUID 格式的 `X-Request-ID`；JSON 请求使用 `Content-Type: application/json`，失败返回 Problem Details。完整字段、状态码和限制以 [OpenAPI](docs/protocol/http/openapi-v1.json)、[HTTP 路由](backend/src/harness_shell_sidecar/web/routes) 和严格请求模型为准。

| 方法与路径 | 主要参数 | 返回结果 |
| --- | --- | --- |
| `GET /v1/health/live` | 请求头 `X-Request-ID` | `request_id`、`live` |
| `GET /v1/health/ready` | 请求头 `X-Request-ID` | `request_id`、`ready`、`state`；未就绪为 503 |
| `GET /v1/runtime/state` | 请求头 `X-Request-ID` | `request_id`、`state` |
| `GET /v1/runtime/credential-encryption-key` | 请求头 `X-Request-ID` | 当前进程凭据加密公钥信息 |
| `GET /v1/connections` | 请求头 `X-Request-ID` | 连接配置列表 |
| `POST /v1/connections` | `display_name`、`host`、`username`、`auth_kind`、`credential_envelope`；可选 `port` 等字段 | 新连接配置；201 |
| `PATCH /v1/connections/{connection_id}` | 连接 ID、完整非秘密连接字段；可选替换凭据 | 更新后的连接配置 |
| `POST /v1/host-key-inspections` | `connection_id` | Host Key 检查结果；确认与替换使用对应 typed routes |
| `POST /v1/ssh/sessions` | `connection_id` | `request_id`、`status`；201 |
| `DELETE /v1/ssh/sessions/{ssh_session_id}` | SSH 会话 ID | 关闭后的 `status` |
| `POST /v1/pty/sessions` | `ssh_session_id`、`cols`、`rows` | `request_id`、`pty_session`；201 |
| `POST /v1/pty/sessions/{pty_session_id}/resize` | PTY ID、`cols`、`rows` | 更新后的 `pty_session` |
| `GET /v1/agent/api-configs` | 请求头 `X-Request-ID` | `request_id`、`configs`，不返回 API Key 原文 |
| `POST /v1/agent/api-configs` | Provider 字段、`api_key_envelope` | `request_id`、`config`；201 |
| `POST /v1/agent/turns` | `ssh_session_id`、`api_config_id`、`user_message`；可选 `conversation_id` | 200 SSE，要求 `Accept: text/event-stream` |
| `PUT /v1/sftp/uploads/{operation_id}/chunks/{sequence}` | 操作 ID、序号、`X-Chunk-Offset` 和 raw bytes | chunk 接收结果；须先创建上传操作 |
| `GET /v1/sftp/downloads/{operation_id}/chunks/{sequence}` | 操作 ID、序号 | 二进制 chunk；须先创建下载操作 |

SFTP 还提供 context、目录列表、元数据、SHA-256、上传/下载开始与结束、abort、重命名、删除和 recovery 接口。chunk 使用 `application/octet-stream`，最大 256 KiB；不要把本地绝对路径传给 Backend。

Runtime WebSocket 路径是 `/v1/runtime/events`，承载 heartbeat、PTY 输入及 SSH/PTY/SFTP 事件，仅允许一个活动 owner。Agent SSE 独立返回 `started → (text_delta | text_replace)* → completed | failed`，没有自动重连或 replay。详见 [WebSocket schema](docs/protocol/http/runtime-websocket-v1.schema.json) 和 [流式契约](docs/protocol/http/README.md)。

Backend 启动后的只读请求示例：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8765/v1/health/live" `
  -Headers @{ "X-Request-ID" = [guid]::NewGuid().ToString() }
```

响应包含本次 `request_id` 和 `live: true`；存活不等于资源就绪，应继续检查 `/v1/health/ready`。

### Backend CLI

```powershell
backend\.venv\Scripts\python.exe -m harness_shell_sidecar --help
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve --help
```

`serve` 的两个必填参数见配置表。`desktop` 仅供 Launcher 使用，要求 `--port 0`、绝对 `--data-dir`、`--control-read-handle` 和 `--ready-write-handle`；不能手工伪造句柄启动。安装 Python 包后也可使用虚拟环境中的 `harness-shell-sidecar` 命令。

## 测试

先完成依赖安装和 tokenizer 准备。

源码回归与契约检查（仓库根目录）：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets
cargo test --manifest-path launcher\Cargo.toml --all-targets
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
```

Pytest 覆盖单元、存储、HTTP/WebSocket/SSE 集成测试；Vitest 使用 jsdom/Testing Library，Cargo tests 覆盖 UI shell 和 Launcher。默认 Pytest 会跳过 SSH integration，不能把零失败解释成已运行真实 SSH 测试。

单独运行 SSH integration（需要 Docker Desktop、Compose v2 和 `ssh-keygen.exe`）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-ssh-lab.ps1
try {
  $env:HARNESS_RUN_SSH_INTEGRATION = "1"
  backend\.venv\Scripts\python.exe -m pytest backend\tests\ssh_integration -q
} finally {
  Remove-Item Env:HARNESS_RUN_SSH_INTEGRATION -ErrorAction SilentlyContinue
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-ssh-lab.ps1
}
```

仓库级门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

各层证据必须分开理解：

- Python/Frontend/Rust tests 证明对应源码与静态契约。
- packaged smoke 证明本次 Backend executable 的局部 loopback 行为。
- OpenSSH Lab 证明容器化测试环境中的 Direct、ProxyJump、SFTP 或 Agent 行为。
- NSIS build 和 installer 静态检查不等于真实安装与 Desktop 人工验收。
- fake ChatModel 不等于真实 Provider，容器 OpenSSH 不等于生产 SSH。

## 构建与部署

### Windows 安装包

Launcher 构建脚本使用 `--offline --locked`。首次构建前，从仓库根下载两个 Rust 工程的锁定依赖，再执行构建：

```powershell
cargo fetch --locked --manifest-path launcher\Cargo.toml
cargo fetch --locked --manifest-path frontend\src-tauri\Cargo.toml
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
```

脚本按 Backend → Launcher → Frontend → NSIS 顺序 fail fast。生成的 Sidecar/Launcher executable、`dist/`、`target/` 和安装包均为本地构建产物，不得提交到 Git。

安装包输出目录为 `frontend/src-tauri/target/release/bundle/nsis/`。安装生成的 NSIS `.exe` 后，安装版只能从 `harness-shell-launcher.exe` 对应的快捷方式或安装完成入口启动，不能把 UI 或 Backend executable 当作独立用户入口。

### Docker、云平台与生产运行

仓库中的 [Dockerfile](tests/ssh_lab/Dockerfile) 和 [Compose](tests/ssh_lab/docker-compose.yml) 只构建双节点 OpenSSH 测试实验室：jump 绑定 `127.0.0.1:2222`，target 仅在内部网络访问。它们不用于部署 Harness Shell。

当前没有应用 Docker 镜像、云平台部署配置、远程 Backend、TLS、HTTP 用户认证或 Windows Service 支持，也未发现仓库 CI/CD workflow。不要通过反向代理将 loopback API 暴露到公网。正式使用入口为 Windows 安装版 Launcher；发布下载地址、代码签名方案和完整安装兼容矩阵需要确认。

升级前应在应用完全退出后备份数据目录。已知 Alembic revision 的升级由启动流程执行，迁移失败会阻止启动；不要手工修改版本号或删除旧数据来绕过校验。明文凭据和会话数据需要由使用者妥善保护。

## 常见问题与已知限制

- **Backend 报数据目录错误**：`serve --data-dir` 必须是绝对路径；示例目录应替换为自己的开发目录。
- **`CONTEXT_TOKENIZER_UNAVAILABLE`**：运行安装步骤中的资源准备与 `--check`，检查资源是否完整；运行时不会联网补齐。
- **UI 报 `BACKEND_BOOTSTRAP_INVALID` 或无法初始化**：先确认 Backend 已就绪；地址必须是带显式端口的 `http://127.0.0.1:<port>`。Tauri 使用 CLI 参数，浏览器开发使用 `VITE_BACKEND_URL`。
- **打包提示 Python 或依赖版本不匹配**：核对 `backend/.venv` 的 Python 3.12.14 与 `backend/build-requirements.lock`。源码的 `>=3.12` 声明不等于打包允许任意版本。
- **Launcher 离线构建找不到依赖**：先执行上述 `cargo fetch --locked`，保留脚本的离线校验。
- **旧数据库无法打开**：旧 schema v7 不接管，未知版本拒绝启动；旧数据迁移方案需要确认。当前仅实现已知 Alembic revision 升级。
- **SSH tests 显示 skipped**：必须启动 SSH Lab 并设置 `HARNESS_RUN_SSH_INTEGRATION=1`，或运行对应仓库门禁。
- **刷新后传输状态不完整**：浏览器本地文件 preparation 会丢失；remote recovery 需要用户显式操作，不会自动联网或重放 mutation。
- **Agent 是否只读**：不是。它能执行改变远端状态的命令，没有完整沙箱或逐条审批。工具输出仅保留首部，裁剪发生在输出收集后，不代表远程读取内存已有上限。

真实 Provider、完整安装版 Desktop、生产 SSH 和用户旧数据迁移仍需单独验收。部分已有协议说明保留历史 schema 描述；数据库行为以当前 storage 代码和 [Backend 指南](docs/agents/python-sidecar.md) 为准。

## 贡献指南

1. 先阅读 [AGENTS.md](AGENTS.md)，按任务范围查看领域指南和局部规则。
2. 保持修改范围明确，不提交密钥、数据库、日志、`.runtime/`、依赖目录和构建产物。
3. 行为变更补充相应测试；协议、存储或跨层契约变更同步实现、测试和唯一文档真源。
4. 提交 PR 时说明具体问题、行为变化、验证命令与结果，并区分自动测试和人工验收。

文档变更至少检查相对链接、标题、占位内容和空白差异：

```powershell
git diff --check
```

尚未发现独立 `CONTRIBUTING.md`；贡献约束以现有 AGENTS 文档为准。`docs/superpowers/` 为本地规格与计划，不纳入 Git。

## 进一步阅读

- [架构与进程所有权](docs/agents/architecture.md)
- [Protocol 与安全边界](docs/agents/protocol-security.md)
- [Frontend 指南](docs/agents/frontend.md)
- [Python Backend 指南](docs/agents/python-sidecar.md)
- [Launcher 与 Tauri 指南](docs/agents/rust-core.md)
- [测试与验收分层](docs/agents/testing.md)

## 许可证

本项目采用 [MIT License](LICENSE)。

## 联系方式与问题反馈

通过当前 Git remote 对应的 [GitHub 仓库](https://github.com/Franc1s396/harness-shell) 提交 Issue 或 Pull Request。报告问题时提供系统与工具链版本、复现步骤、预期与实际结果，以及已人工脱敏的日志。维护者邮箱和私密安全报告渠道需要确认；不要在公开 Issue 中上传凭据或运行时数据库。
