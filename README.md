# Harness Shell

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用。M1 已实现 Tauri Core 管理的 Python Sidecar、版本化私有 stdio 协议、DPAPI Vault、AES-GCM 运行时存储、篡改可见 Audit/本地 Trace，以及主窗口与审批窗口的 Capability 隔离。SSH、Agent Workflow 和真实审批仍未实现。

## 目录结构

```text
.
├── frontend/                  # Tauri 2 + React + TypeScript
│   ├── src/                   # React WebView
│   └── src-tauri/             # Tauri Rust Core
├── backend/                   # Python Sidecar
│   ├── src/harness_shell_sidecar/
│   └── tests/
└── docs/superpowers/          # 设计规格与实施计划
```

## 前置环境

- Node.js 22 与 npm 10。
- Python 3.12 或更高版本。
- Tauri 桌面开发额外需要 Rust stable MSVC toolchain、Microsoft C++ Build Tools、Windows SDK 和 WebView2。

## Web 前端

```powershell
cd frontend
npm install
npm run dev
```

生产构建：

```powershell
npm run build
```

## Tauri 桌面应用

安装完整的 Windows/Tauri 前置工具链后运行：

```powershell
cd frontend
npm install
npm run tauri dev
```

可使用以下命令检查本机环境：

```powershell
npm run tauri info
```

## M1 一键验证

要求 Windows、Python 3.12.13、Node.js/npm、Rust stable MSVC toolchain、Visual Studio C++ Build Tools、Windows SDK 和 WebView2 Runtime：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
```

脚本严格执行 Python 环境与测试、Sidecar 打包、全部 Rust 测试、`npm ci`/Web 多页构建和 `tauri info`。完整手工验收见 [`docs/testing/m1-acceptance.md`](docs/testing/m1-acceptance.md)。

## Python Sidecar

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m harness_shell_sidecar
```

Sidecar 只通过 stdin/stdout 传输 `Content-Length` 帧；协议版本为 `1`，header 上限 `8192` bytes，payload 上限 `1048576` bytes，heartbeat 为 5 秒、超时为 15 秒。正常 stdout 不允许混入日志，stderr 会限制长度并对已知 secret 片段做脱敏。

桌面启动时，Rust Core 从当前 Windows 用户 DPAPI Vault 获取两个 32-byte 运行时密钥，启动打包 Sidecar，完成 `sidecar.ready`/`initialize` 握手后才发布 `READY`。Sidecar 异常退出或 heartbeat 超时会进入 `PAUSED`；M1 不自动重启，避免隐式重放操作。

PyInstaller 产物由 `backend/scripts/build_sidecar.ps1` 生成到 `backend/dist/`，并复制为 Tauri target-triple external binary。两个位置的 `.exe` 都是生成物，不提交到 Git；构建环境必须匹配 `backend/build-requirements.lock`。

本地应用数据目录包含 `vault.sqlite3`（DPAPI 密文）和 `runtime.sqlite3`（Audit、Trace 与 AES-GCM 记录），以及 SQLite 可能创建的 `-wal`/`-shm` 文件。WebView 只能读取脱敏的 `RuntimeStatus`，不能访问 Vault、raw frame、stderr 或 shell。

M1 限制：没有 SSH/SFTP、Provider 调用、Agent Workflow、真实 approval token、自动恢复、远程部署或迁移验收。

## 设计文档

总体方案见 [`docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md`](docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md)。
