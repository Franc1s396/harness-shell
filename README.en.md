# Harness Shell

[简体中文](README.md) | English

## Introduction

Harness Shell is a local AI SSH Agent desktop application for Windows, bringing SSH terminals, file transfers, and AI-assisted operations into one workspace.

**Features**

- SSH connection management, host key confirmation, direct connections, and single-hop ProxyJump.
- Multiple terminal tabs and manual SFTP uploads and downloads.
- An AI Agent that executes commands in connected SSH sessions, with streaming responses and cancellation. Clearly read-only commands run automatically; other commands require user approval.
- Support for Chat Completions, Responses API, and context summaries.
- Simplified Chinese, Traditional Chinese, and English interfaces.

**Technology stack**: React, TypeScript, Vite, Tailwind CSS, xterm.js; Tauri 2, Rust; Python, FastAPI, AsyncSSH, LangGraph; SQLite, SQLAlchemy, Alembic.

## Local Development Quick Start

Install Windows 10/11 x64, Python 3.12, Node.js 22.12+, Rust stable (MSVC), Visual Studio C++ Build Tools, and WebView2 Runtime. The commands below use PowerShell.

### 1. Clone the Repository and Install Dependencies

```powershell
git clone https://github.com/Franc1s396/harness-shell.git
cd harness-shell
python -m venv backend/.venv
backend\.venv\Scripts\python.exe -m pip install -e "./backend[dev]"
npm.cmd --prefix frontend ci
backend\.venv\Scripts\python.exe backend/scripts/prepare_tokenizer.py --output-dir backend/build/tokenizer
```

Initial tokenizer preparation requires internet access. Missing resources prevent the backend from starting.

### 2. Start the Backend

Run from the repository root and keep the terminal open:

```powershell
$devDataDir = Join-Path (Get-Location).Path ".runtime\dev"
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve --port 8765 --data-dir "$devDataDir"
```

### 3. Start the Desktop UI

Open another terminal and run from the repository root:

```powershell
npm.cmd --prefix frontend run tauri:dev -- -- -- --backend-url http://127.0.0.1:8765
```

Add an SSH connection, then verify and trust its host key. To use the AI Agent, configure a model provider and API key. Regular SSH/SFTP does not require a model service.

Development data is stored in `.runtime/dev`. The database may contain plaintext credentials and conversation content; do not upload or commit it.

## Contributing

- Read [AGENTS.md](AGENTS.md) and the relevant domain guides before making changes.
- Keep changes focused, add tests for behavior changes, and update relevant documentation when contracts change.
- Describe the problem, changes, and verification results in your pull request. See the [Testing Guide](docs/agents/testing.md) for test commands.
- Run `git diff --check` before submitting. Do not commit credentials, runtime data, dependency directories, or build artifacts.

## License

This project is licensed under the [MIT License](LICENSE).

## Contact and Feedback

Use [GitHub Issues](https://github.com/Franc1s396/harness-shell/issues) to contact the maintainers, report bugs, or suggest improvements. [Pull requests](https://github.com/Franc1s396/harness-shell/pulls) are also welcome. Include your system version, reproduction steps, expected and actual results, and sanitized logs when reporting a problem.
