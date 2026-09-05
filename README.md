# Harness Shell

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用。项目以人工 SSH 终端为核心，在显式连接、Host Key 信任和远程会话边界内提供 Manual SFTP，以及实验性的 ReAct Shell Agent。

当前版本为 `0.1.0`，仍处于开发与验证阶段。

## 当前能力

- SSH 连接配置、显式 Host Key 信任、直连与单层 ProxyJump。
- 多标签人工 PTY，输入和运行时事件通过单个 Runtime WebSocket 传输。
- 仅由用户显式发起的 Manual SFTP，包括上传、下载、远端临时文件、commit、abort 和 recovery。
- 实验性 ReAct Shell Agent：每个 turn 固定所选 Provider 与已连接 SSH Session，只允许严格的 `execute_command` 工具。
- Agent 最终可见文本通过独立 SSE response 流式返回，不复用 Runtime WebSocket。
- 简体中文、繁体中文和英文界面。

以下能力尚未完成或不能由现有自动验证证明：真实 Provider 全矩阵、完整安装版 Desktop matrix、服务端审批、自动恢复、生产 SSH、部署和旧数据迁移。自动测试、构建、打包或容器 OpenSSH Lab 通过，不代表这些场景已经验收。

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
- Runtime SQLite 只接受全新 schema v6，不迁移或兼容读取旧数据库。
- schema v6 是 plaintext store。凭据、Agent conversation/message/output、remote recovery 和其他业务 payload 可能明文落盘，目前没有 at-rest encryption。
- 连接私钥和 Manual SFTP 本地文件由 React 选择与读取；Python 不接收本地绝对路径。
- 日志、Problem Details、SSE terminal event 和 Runtime WebSocket event 不应包含 secret、Provider response body、命令、stdout/stderr 或文件内容。

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

## 前置环境

- Windows 10/11 x64。
- Node.js 22 与 npm 10。
- Python 3.12 或更高版本；可复现 Sidecar 打包和仓库门禁严格要求 Python `3.12.13`。
- Rust stable MSVC toolchain，host 为 `x86_64-pc-windows-msvc`。
- Microsoft C++ Build Tools、Windows SDK 和 WebView2 Runtime。
- Docker Desktop、Docker Compose v2 和 Windows OpenSSH `ssh-keygen.exe`，仅在运行 M2、Manual SFTP 和 M3 SSH Lab 门禁时需要。

## 安装依赖

在仓库根目录执行：

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm install
```

如果需要执行可复现打包或完整仓库门禁，请确保虚拟环境实际使用 Python `3.12.13`，并与 `backend/build-requirements.lock` 保持一致。

## 本地开发

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
npm.cmd --prefix frontend run dev
```

这不是完整 Tauri Desktop 路径，并且需要通过 `VITE_BACKEND_URL` 提供合法的 loopback Backend 地址，才能初始化业务 client。

## 测试与契约检查

最小完整回归：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets --offline
cargo test --manifest-path launcher\Cargo.toml --all-targets --offline
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
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

## 构建 Windows 安装包

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
```

脚本按 Backend → Launcher → Frontend → NSIS 顺序 fail fast。生成的 Sidecar/Launcher executable、`dist/`、`target/` 和安装包均为本地构建产物，不得提交到 Git。

安装版只能从 `harness-shell-launcher.exe` 对应的快捷方式或安装完成入口启动，不能把 UI 或 Backend executable 当作独立用户入口。

## 进一步阅读

- [架构与进程所有权](docs/agents/architecture.md)
- [Protocol 与安全边界](docs/agents/protocol-security.md)
- [Frontend 指南](docs/agents/frontend.md)
- [Python Backend 指南](docs/agents/python-sidecar.md)
- [Launcher 与 Tauri 指南](docs/agents/rust-core.md)
- [测试与验收分层](docs/agents/testing.md)

## License

本项目采用 [MIT License](LICENSE)。
