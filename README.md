# Harness Shell

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用，采用 React/TypeScript WebView、Tauri 2 Rust Core 和 Python Sidecar。Rust Core 独占 packaged child、Windows Job、DPAPI Vault 和动态 loopback 端口，并通过 sealed typed HTTP API 与单个 Runtime WebSocket 管理 Python；WebView 只接触受控 Tauri commands 和安全事件投影。

当前 v0.1.0 已实现连接管理、显式 Host Key 信任、直连与单层 ProxyJump、多标签人工 PTY，以及只由用户显式操作的 Manual SFTP。实验性 M3 ReAct Shell Agent 后端与 React 前端也已接入：模型 API Key 只保存在 Rust DPAPI Vault 中，Sidecar 保存非秘密 Provider 配置和加密的 conversation/run/message，Agent Workspace 按 terminal tab 隔离，并将每次 turn 绑定到启动时选择的 Provider 与 connected SSH Session。

Agent 当前只允许严格的 `execute_command` 工具，不接入 Manual SFTP、Artifact 或任意兼容路由。真实 Provider、完整 Tauri Desktop Agent matrix、服务端审批、自动恢复、生产 SSH、部署和迁移仍需分别验收；自动测试、构建或容器 OpenSSH Lab 通过不能替代这些验收。

## 目录结构

```text
.
├── frontend/                         # React/TypeScript WebView 与 Tauri Rust Core
│   ├── src/                          # UI、typed API、状态与 i18n
│   └── src-tauri/                    # Tauri commands、Vault、HTTP/WS Runtime 与 Sidecar 生命周期
├── backend/                          # Python Sidecar
│   ├── src/harness_shell_sidecar/    # SSH、PTY、Manual SFTP、Agent、存储与遥测
│   └── tests/                        # Python 单元、集成与 SSH 测试
├── scripts/                          # M1、M2、Manual SFTP 与 M3 自动门禁
├── tests/ssh_lab/                    # 隔离的双节点 OpenSSH 容器实验室
├── docs/protocol/http/               # HTTP/WebSocket v1 契约与 fixture
└── docs/testing/                     # 自动门禁和人工验收记录
```

## 前置环境

- Node.js 22 与 npm 10。
- Python 3.12 或更高版本；可复现 Sidecar 打包严格要求 Python `3.12.13`。
- Rust stable MSVC toolchain。
- Microsoft C++ Build Tools、Windows SDK 和 WebView2 Runtime。
- M2、Manual SFTP 和 M3 自动门禁还要求 Docker Desktop、Docker Compose v2 和 Windows OpenSSH `ssh-keygen.exe`。

## Web 前端

```powershell
cd frontend
npm install
npm run dev
```

运行测试与生产构建：

```powershell
npm run test
npm run build
```

## Tauri 桌面应用

开发模式会先构建 packaged Python Sidecar，再启动 Tauri：

```powershell
cd frontend
npm install
npm run tauri:dev
```

检查本机 Tauri 构建环境：

```powershell
npm run tauri info
```
