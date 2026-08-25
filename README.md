# Harness Shell

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用。当前仓库只包含可运行的最小工程骨架，尚未实现 SSH、Agent Workflow、审批、存储、加密或 Sidecar 通信协议。

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

## Python Sidecar

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m harness_shell_sidecar
```

当前 Sidecar 入口会正常退出且不写 stdout/stderr。stdin/stdout 版本化帧协议将在后续 M1 工作中实现。

## 设计文档

总体方案见 [`docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md`](docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md)。
