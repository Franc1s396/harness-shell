# Harness Shell

Harness Shell 是一款面向 Windows 的本地 AI SSH Agent 桌面应用。M2 已在 M1 的 Tauri Core、私有 stdio 协议、DPAPI Vault、加密存储、Audit/Trace 和 Capability 边界上增加连接管理、显式 Host Key 信任、直连与单层 ProxyJump 和多 tab 人工 PTY。旧的 Sidecar 内部 Agent exec/SFTP/Artifact 运行时已删除；当前 SFTP Activity 是绑定用户进入该 Activity 时显式选中的 connected terminal tab 的手动文件管理器，不向 Agent、Workflow 或 approval window 暴露。用户手动 SFTP 实现已完成，`verify-manual-sftp.ps1` 自动门禁已通过；Tauri Desktop 人工验收仍待用户单独确认，M3 Agent Workflow 与真实审批仍未实现。

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

## M1 / M2 一键验证

要求 Windows、Python 3.12.13、Node.js/npm、Rust stable MSVC toolchain、Visual Studio C++ Build Tools、Windows SDK 和 WebView2 Runtime：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
```

脚本严格执行 Python 环境与测试、Sidecar 打包、全部 Rust 测试、`npm ci`/Web 多页构建和 `tauri info`。完整手工验收见 [`docs/testing/m1-acceptance.md`](docs/testing/m1-acceptance.md)。

M2 还要求 Docker Desktop、Docker Compose v2 和 Windows OpenSSH `ssh-keygen.exe`。它会建立双节点隔离实验室，执行真实 password/key/passphrase/ProxyJump/Host-Key/PTY 测试并扫描 secret marker：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
```

完整桌面验收记录见 [`docs/testing/m2-acceptance.md`](docs/testing/m2-acceptance.md)。容器测试与手工清单均不代表生产主机验收。

用户手动 SFTP 的 fail-fast 自动门禁会回归 M2，并额外运行 focused Python、packaged Sidecar/Rust、Frontend 和 Direct/ProxyJump OpenSSH SFTP/PTY isolation，最后扫描 credential、本地路径与文件内容 marker：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
```

用户手动 SFTP 提供单项文件/目录/symlink 操作、懒加载远程目录树、显式 SHA-256/Properties、原子传输与人工 Recovery Center；不提供批量、拖放、目录合并或递归上传/下载。桌面人工验收必须另行执行 [`docs/testing/manual-sftp-desktop-acceptance.md`](docs/testing/manual-sftp-desktop-acceptance.md)，不能由自动门禁推导。

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

当前限制：没有 Provider 调用、Agent Workflow、真实 approval token、自动恢复、生产部署或迁移验收。用户手动 SFTP 只允许在主窗口 SFTP Activity 中显式操作；旧 Agent exec/SFTP 不再是 Sidecar 内部接口，Agent 与 approval window 都没有对应调用路由。

## 设计文档

总体方案见 [`docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md`](docs/superpowers/specs/2026-08-25-ai-ssh-agent-design.md)。
