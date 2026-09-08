# Harness Shell

简体中文 | [English](README.en.md)

## 项目介绍

Harness Shell 是面向 Windows 的本地 AI SSH Agent 桌面应用，将 SSH 终端、文件传输和 AI 辅助操作整合在同一工作区。

**功能特性**

- SSH 连接管理、Host Key 确认、直连与单层 ProxyJump。
- 多标签终端与手动 SFTP 文件上传、下载。
- AI Agent：在已连接的 SSH 会话中执行命令，支持流式回复和取消；明确只读的命令自动执行，其余需用户审核。
- 支持 Chat Completions、Responses API，以及上下文摘要。
- 简体中文、繁体中文和英文界面。

**技术栈**：React、TypeScript、Vite、Tailwind CSS、xterm.js；Tauri 2、Rust；Python、FastAPI、AsyncSSH、LangGraph；SQLite、SQLAlchemy、Alembic。

## 本地调试 Quick Start

准备 Windows 10/11 x64、Python 3.12、Node.js 22.12+、Rust stable（MSVC）、Visual Studio C++ Build Tools 和 WebView2 Runtime。以下命令使用 PowerShell。

### 1. 获取源码并安装依赖

```powershell
git clone https://github.com/Franc1s396/harness-shell.git
cd harness-shell
python -m venv backend/.venv
backend\.venv\Scripts\python.exe -m pip install -e "./backend[dev]"
npm.cmd --prefix frontend ci
backend\.venv\Scripts\python.exe backend/scripts/prepare_tokenizer.py --output-dir backend/build/tokenizer
```

首次准备 tokenizer 需要联网；资源缺失会阻止后端启动。

### 2. 启动后端

在仓库根目录运行，并保持终端开启：

```powershell
$devDataDir = Join-Path (Get-Location).Path ".runtime\dev"
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve --port 8765 --data-dir "$devDataDir"
```

### 3. 启动桌面 UI

另开一个终端，在仓库根目录运行：

```powershell
npm.cmd --prefix frontend run tauri:dev -- -- -- --backend-url http://127.0.0.1:8765
```

启动后添加 SSH 连接，核对并信任 Host Key。使用 AI Agent 时，再配置模型 Provider 和 API Key；普通 SSH/SFTP 无需模型服务。

开发数据保存在 `.runtime/dev`，数据库可能明文保存凭据与会话内容，请勿上传或提交。

## 贡献指南

- 修改前阅读 [AGENTS.md](AGENTS.md) 及对应领域指南。
- 保持改动聚焦，行为变更补充测试，契约变化同步相关文档。
- 提交 Pull Request 时说明问题、改动和验证结果；测试入口见 [Testing Guide](docs/agents/testing.md)。
- 提交前运行 `git diff --check`，不要提交凭据、运行时数据、依赖目录或构建产物。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 联系方式与问题反馈

通过 [GitHub Issues](https://github.com/Franc1s396/harness-shell/issues) 联系维护者、报告问题或提出建议，也欢迎提交 [Pull Request](https://github.com/Franc1s396/harness-shell/pulls)。反馈请附系统版本、复现步骤、预期与实际结果，以及脱敏后的日志。
