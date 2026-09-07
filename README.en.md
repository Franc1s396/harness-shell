# Harness Shell

[简体中文](README.md) | English

Harness Shell is a local AI SSH Agent desktop application for Windows. It centers on an interactive SSH terminal, with Manual SFTP and an experimental ReAct Shell Agent operating within explicitly selected connections, trusted host keys, and remote sessions.

The current version is `0.2.0` and remains under development and validation. It is intended for developers who manage SSH connections locally, use remote terminals, transfer files manually, and explore AI-assisted operations in connected sessions.

The terminal, file transfers, and Agent share one desktop workspace. SSH/SFTP does not require a model provider; a model service is needed only for the Agent.

## Features

- SSH connection profiles, explicit host key trust, direct connections, and single-hop ProxyJump.
- Multiple interactive PTY tabs, with input and runtime events carried over one Runtime WebSocket.
- User-initiated Manual SFTP, including uploads, downloads, remote temporary files, commit, abort, and recovery.
- An experimental ReAct Shell Agent: each turn freezes the selected provider and connected SSH session, exposing only the strictly validated `execute_command` tool.
- Agent-visible text, including explanations before tool execution, streams through a separate SSE response with full-text updates. It does not reuse the Runtime WebSocket.
- Explicit selection of Chat Completions or Responses API, provider context budgets, rolling summaries, and tool-output prefix clipping.
- Simplified Chinese, Traditional Chinese, and English interfaces.

The following are incomplete or not established by existing automated checks: the full real-provider matrix, the full installed-desktop test matrix, server-side approval, automatic recovery, production SSH, deployment, and legacy-data migration. Passing tests, builds, packaging, or the containerized OpenSSH Lab does not establish acceptance for these scenarios.

## Technology Stack

| Layer | Technologies and purpose |
| --- | --- |
| Frontend | React 19, TypeScript 5.8, Vite 7, Tailwind CSS 4; Zustand for state, i18next for localization, xterm.js for terminals |
| Desktop | Minimal Tauri 2 UI shell; Rust Launcher manages child processes through Windows Jobs and anonymous pipes |
| Backend | Python ≥3.12, FastAPI, Uvicorn, Pydantic, AsyncSSH |
| Agent | LangGraph, langchain-core, OpenAI Python SDK, tiktoken |
| Storage | SQLite STRICT, SQLAlchemy 2.0, Alembic |
| Testing and packaging | Pytest, Vitest, Testing Library, Cargo tests, PyInstaller, NSIS; Docker Compose for the SSH Lab |

See [package.json](frontend/package.json), [pyproject.toml](backend/pyproject.toml), and the Rust `Cargo.toml` files for dependency ranges. Exact locked versions are recorded in their respective lock files.

## Architecture

The packaged desktop startup path is fixed:

```text
harness-shell-launcher.exe
  -> harness-shell-sidecar.exe desktop --port 0
  -> Backend binds a dynamic 127.0.0.1 port and sends a ready frame
  -> harness-shell-ui.exe --backend-url http://127.0.0.1:<port>
  -> React connects directly to Python typed HTTP, Agent SSE, and Runtime WebSocket
```

| Component | Responsibilities |
| --- | --- |
| Launcher | Sole owner of packaged Backend/UI children, Windows Job, dynamic port negotiation, ready/control pipes, and shutdown order |
| Tauri 2 UI shell | Backend bootstrap and permissions to close or destroy the main window |
| React/TypeScript WebView | UI state, typed loopback client, Runtime WebSocket, Agent SSE, local file selection, and Manual SFTP chunk iteration |
| Python FastAPI Backend | SQLite, credentials, SSH/PTY, remote Manual SFTP, Agent, dispatcher, and logging |

The Launcher does not scan ports, reconnect, or respawn processes. Tauri does not proxy business HTTP/WebSocket traffic or own the Backend lifecycle, credential repository, or file transfers.

## Security and Data Boundaries

- The Backend listens only on `127.0.0.1`. Loopback is not a complete authentication boundary against malicious processes in the same user session.
- React encrypts connection passwords, private keys, and provider API keys using the Backend public key in an RSA-OAEP-256 + AES-256-GCM request envelope, submitted with the associated business mutation.
- At startup, Alembic creates the `0001_initial` SQLite baseline or upgrades known revisions. Legacy schema v7, old databases without a valid version identity, and unknown revisions are rejected. There is no legacy import, automatic backup, recovery, or downgrade workflow.
- The database is a plaintext store. Credentials, Agent conversations/messages/output, remote recovery records, and other business payloads may be stored in plaintext. There is no encryption at rest.
- React selects and reads connection private keys and local Manual SFTP files. Python does not receive local absolute paths.
- Logging call sites must not explicitly record secrets, request bodies, model bodies, commands, or remote output. The logging layer does not automatically scan or sanitize supplied content. Review logs manually before reporting an issue.
- The Agent's `execute_command` can modify remote state. The fixed dangerous-command regular expression is not a complete sandbox, and there is no per-command approval workflow. Use it only in explicitly authorized SSH sessions.

## Project Structure

```text
.
├── frontend/
│   ├── src/                         # React UI, typed API, state, and i18n
│   └── src-tauri/                   # Minimal Tauri UI shell, bootstrap, and NSIS configuration
├── launcher/                        # Desktop children, Windows Job, ready/control lifecycle
├── backend/
│   ├── src/harness_shell_sidecar/   # FastAPI, SSH, PTY, Manual SFTP, Agent, and storage
│   ├── tests/                       # Python unit, integration, and SSH tests
│   └── scripts/                     # Sidecar packaging and smoke tests
├── scripts/                         # Build scripts and M1/M2/Manual SFTP/M3 verification gates
├── tests/ssh_lab/                   # Isolated two-node OpenSSH container lab
├── docs/protocol/http/              # HTTP, WebSocket, and SSE contracts and fixtures
├── docs/testing/                    # Automated checks and manual acceptance records
├── docs/agents/                     # Architecture and domain maintenance guides
└── docs/superpowers/                # Local specifications and plans, excluded from Git
```

## Requirements

- Development and packaging target Windows x64, intended for Windows 10/11. The minimum OS version and full compatibility matrix require confirmation.
- Node.js 22.12+ and npm 10 are suggested. Vite 7 requires Node.js `^20.19.0 || >=22.12.0`; the project does not separately pin npm.
- Python 3.12 or later for source development. Reproducible Sidecar packaging and repository verification gates strictly require Python `3.12.14`.
- Rust stable MSVC toolchain with host `x86_64-pc-windows-msvc`.
- Microsoft C++ Build Tools, Windows SDK, and WebView2 Runtime.
- Docker Desktop, Docker Compose v2 (scripts require `docker-compose.exe`), and Windows OpenSSH `ssh-keygen.exe` for M2, Manual SFTP, and M3 SSH Lab gates only.

No separate database service or Redis is required. SQLite resides in the local data directory. SSH features require a reachable remote SSH service; the Agent also requires a reachable model provider supporting the selected API type. Dependency installation, initial tokenizer preparation, and initial build dependency downloads require network access.

## Installation

Run from the repository root:

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd ..\frontend
npm.cmd ci
cd ..
```

`py -3.12` does not guarantee a specific patch version. For packaging or repository gates, confirm that the virtual environment uses Python `3.12.14`, then install the locked dependencies from the repository root:

```powershell
backend\.venv\Scripts\python.exe --version
backend\.venv\Scripts\python.exe -m pip install -r backend\build-requirements.lock
```

Before the first startup or test run, explicitly prepare the offline tokenizer resources from the repository root:

```powershell
backend\.venv\Scripts\python.exe backend\scripts\prepare_tokenizer.py --output-dir backend/build/tokenizer
backend\.venv\Scripts\python.exe backend\scripts\prepare_tokenizer.py --output-dir backend/build/tokenizer --check
```

The runtime does not download tokenizer resources or reuse user caches. Missing or corrupted resources prevent the Backend from becoming ready.

## Configuration

There is no shared `.env.example` or Backend `.env` loading entry point. The Backend uses CLI arguments. Connection and provider settings are entered through the UI and stored in SQLite.

| Setting | Required | Description |
| --- | --- | --- |
| `serve --port` | Yes | Development port in `1..65535`; listens only on `127.0.0.1` |
| `serve --data-dir` | Yes | Absolute path containing `runtime.sqlite3` and `logs/` |
| UI `--backend-url` | Required for the Tauri path | `http://127.0.0.1:<port>`; injected by the Launcher in packaged builds |
| `VITE_BACKEND_URL` | Required for browser development; optional for Tauri development | Read only in development mode and takes precedence over Tauri bootstrap; never put secrets here |
| `LOCALAPPDATA` | System variable required by the installed application | Launcher uses `%LOCALAPPDATA%\com.harnessshell.app` as its data directory |
| SSH settings | Required for SSH | Host, port, username, and authentication details; optional single-hop ProxyJump; host key trust is required before connecting |
| Provider settings | Required for the Agent | `display_name`, `api_type`, `base_url`, `model`, and API key; `api_type` is `CHAT_COMPLETIONS` or `RESPONSES` |
| Provider budgets | Optional | `context_window_size=128000`, `context_compaction_threshold_ratio=0.75`, `max_output_tokens=8192`; `enabled` defaults to `true` |

Configure context and output budgets for the selected model. Defaults are not discovered model capabilities. Budgets must satisfy `1 <= floor(context_window_size * context_compaction_threshold_ratio) <= context_window_size - max_output_tokens`.

Do not put API keys in `VITE_*` variables. The UI wraps API keys, SSH passwords, and private keys in credential envelopes encrypted for the current Backend public key. This protects submission, not database contents at rest.

Build configuration is in [tauri.conf.json](frontend/src-tauri/tauri.conf.json), the [Vite configuration](frontend/vite.config.ts), and the [packaging dependency lock](backend/build-requirements.lock).

## Usage

### Start the Development Environment

Source development requires separate Python Backend and Tauri UI processes. The Backend needs a fixed loopback port and an absolute data directory.

Terminal 1, from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve `
  --port 8765 `
  --data-dir E:\absolute\harness-shell-dev
```

Terminal 2, from the repository root:

```powershell
npm.cmd --prefix frontend run tauri:dev -- -- -- --backend-url http://127.0.0.1:8765
```

The two Tauri `--` separators delimit runner arguments and application arguments. Do not invoke the Backend's `desktop` subcommand directly; only the Launcher can start it with inherited handles.

To run only the Vite development server:

```powershell
$env:VITE_BACKEND_URL = "http://127.0.0.1:8765"
npm.cmd --prefix frontend run dev
```

This is not the full Tauri desktop path. It requires a valid loopback Backend address in `VITE_BACKEND_URL` to initialize the business client.

### Everyday Operations

1. Create an SSH profile in connection management, enter authentication details, and select a jump host if needed.
2. Inspect and explicitly trust the host key, establish an SSH session, and use the terminal tabs.
3. Explicitly select uploads or downloads in the file workspace. Reloading the page loses local file preparation state; resumable downloads are not guaranteed.
4. Before using the Agent, add and enable a provider, select a connected SSH session, and send a message. The provider and SSH session remain fixed for that turn.

### Common Commands

Run from the repository root:

```powershell
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\build_sidecar.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-launcher.ps1
```

The frontend build produces frontend assets only; see below for the full desktop build. There is no unified lint, format, or cloud deployment command.

## API and CLI

### Local API

The API is for local UI integration, not a public remote service. Requests require a UUID `X-Request-ID`. JSON requests use `Content-Type: application/json`; failures return Problem Details. Refer to the [OpenAPI document](docs/protocol/http/openapi-v1.json), [HTTP routes](backend/src/harness_shell_sidecar/web/routes), and strict request models for complete fields, status codes, and limits.

| Method and path | Main parameters | Result |
| --- | --- | --- |
| `GET /v1/health/live` | `X-Request-ID` header | `request_id`, `live` |
| `GET /v1/health/ready` | `X-Request-ID` header | `request_id`, `ready`, `state`; 503 when not ready |
| `GET /v1/runtime/state` | `X-Request-ID` header | `request_id`, `state` |
| `GET /v1/runtime/credential-encryption-key` | `X-Request-ID` header | Credential encryption public key information for the current process |
| `GET /v1/connections` | `X-Request-ID` header | Connection profile list |
| `POST /v1/connections` | `display_name`, `host`, `username`, `auth_kind`, `credential_envelope`; optional fields such as `port` | Created connection profile; 201 |
| `PATCH /v1/connections/{connection_id}` | Connection ID, complete non-secret connection fields, optional replacement credentials | Updated connection profile |
| `POST /v1/host-key-inspections` | `connection_id` | Host key inspection result; confirmation and replacement use their respective typed routes |
| `POST /v1/ssh/sessions` | `connection_id` | `request_id`, `status`; 201 |
| `DELETE /v1/ssh/sessions/{ssh_session_id}` | SSH session ID | `status` after closing |
| `POST /v1/pty/sessions` | `ssh_session_id`, `cols`, `rows` | `request_id`, `pty_session`; 201 |
| `POST /v1/pty/sessions/{pty_session_id}/resize` | PTY ID, `cols`, `rows` | Updated `pty_session` |
| `GET /v1/agent/api-configs` | `X-Request-ID` header | `request_id`, `configs`; no plaintext API key |
| `POST /v1/agent/api-configs` | Provider fields, `api_key_envelope` | `request_id`, `config`; 201 |
| `POST /v1/agent/turns` | `ssh_session_id`, `api_config_id`, `user_message`; optional `conversation_id` | 200 SSE; requires `Accept: text/event-stream` |
| `PUT /v1/sftp/uploads/{operation_id}/chunks/{sequence}` | Operation ID, sequence, `X-Chunk-Offset`, raw bytes | Chunk acceptance result; create an upload operation first |
| `GET /v1/sftp/downloads/{operation_id}/chunks/{sequence}` | Operation ID, sequence | Binary chunk; create a download operation first |

SFTP also provides context, directory listing, metadata, SHA-256, upload/download start and finish, abort, rename, deletion, and recovery endpoints. Chunks use `application/octet-stream` with a maximum size of 256 KiB. Do not send local absolute paths to the Backend.

The Runtime WebSocket at `/v1/runtime/events` carries heartbeat, PTY input, and SSH/PTY/SFTP events, with only one active owner. Agent SSE independently returns `started → (text_delta | text_replace)* → completed | failed`, without automatic reconnection or replay. See the [WebSocket schema](docs/protocol/http/runtime-websocket-v1.schema.json) and [streaming contract](docs/protocol/http/README.md).

A read-only request after starting the Backend:

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8765/v1/health/live" `
  -Headers @{ "X-Request-ID" = [guid]::NewGuid().ToString() }
```

The response contains the request's `request_id` and `live: true`. Liveness is not resource readiness; also check `/v1/health/ready`.

### Backend CLI

```powershell
backend\.venv\Scripts\python.exe -m harness_shell_sidecar --help
backend\.venv\Scripts\python.exe -m harness_shell_sidecar serve --help
```

The two required `serve` parameters are listed in the configuration table. `desktop` is reserved for the Launcher and requires `--port 0`, an absolute `--data-dir`, `--control-read-handle`, and `--ready-write-handle`. Do not fabricate handles to launch it manually. After installing the Python package, the virtual environment also provides the `harness-shell-sidecar` command.

## Testing

Install dependencies and prepare tokenizer resources first.

Source regression and contract checks, from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets
cargo test --manifest-path launcher\Cargo.toml --all-targets
backend\.venv\Scripts\python.exe backend\scripts\export_http_contract.py --check
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
```

Pytest covers unit, storage, and HTTP/WebSocket/SSE integration tests. Vitest uses jsdom/Testing Library; Cargo tests cover the UI shell and Launcher. Pytest skips SSH integration by default, so zero failures does not mean real SSH tests ran.

To run SSH integration separately, with Docker Desktop, Compose v2, and `ssh-keygen.exe` available:

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

Repository verification gates:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

Interpret each type of evidence separately:

- Python, frontend, and Rust tests validate their respective source behavior and static contracts.
- Packaged smoke tests validate local loopback behavior of the Backend executable built in that run.
- The OpenSSH Lab validates direct connections, ProxyJump, SFTP, or Agent behavior in its container environment.
- An NSIS build and static installer checks do not establish successful installation or manual desktop acceptance.
- A fake ChatModel is not a real provider; containerized OpenSSH is not production SSH.

## Build and Deployment

### Windows Installer

The Launcher build script uses `--offline --locked`. Before the first build, fetch the locked dependencies for both Rust projects from the repository root, then build:

```powershell
cargo fetch --locked --manifest-path launcher\Cargo.toml
cargo fetch --locked --manifest-path frontend\src-tauri\Cargo.toml
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
```

The script runs Backend → Launcher → Frontend → NSIS and stops on failure. Generated Sidecar/Launcher executables, `dist/`, `target/`, and installers are local build artifacts and must not be committed to Git.

Installers are written to `frontend/src-tauri/target/release/bundle/nsis/`. After installing the generated NSIS `.exe`, start the application only through the shortcut or installation-completion action targeting `harness-shell-launcher.exe`. Neither the UI nor Backend executable is a standalone user entry point.

### Docker, Cloud Platforms, and Production Use

The repository's [Dockerfile](tests/ssh_lab/Dockerfile) and [Compose file](tests/ssh_lab/docker-compose.yml) build only the two-node OpenSSH test lab: jump binds to `127.0.0.1:2222`, while target is reachable only on the internal network. They do not deploy Harness Shell.

There is currently no application Docker image, cloud deployment configuration, remote Backend, TLS, HTTP user authentication, or Windows Service support. No repository CI/CD workflow was found. Do not expose the loopback API publicly through a reverse proxy. The installed Windows Launcher is the production entry point; release download locations, code signing, and the full installation compatibility matrix require confirmation.

Before upgrading, fully exit the application and back up its data directory. Startup upgrades known Alembic revisions; migration failure prevents startup. Do not edit version identifiers or delete old data to bypass validation. Users must protect plaintext credentials and session data appropriately.

## FAQ and Known Limitations

- **Backend reports a data-directory error:** `serve --data-dir` requires an absolute path. Replace the example directory with your own development directory.
- **`CONTEXT_TOKENIZER_UNAVAILABLE`:** Run the preparation and `--check` commands from the installation section and verify resource integrity. The runtime will not download missing resources.
- **UI reports `BACKEND_BOOTSTRAP_INVALID` or cannot initialize:** Confirm the Backend is ready. Use `http://127.0.0.1:<port>` with an explicit port. Tauri uses CLI arguments; browser development uses `VITE_BACKEND_URL`.
- **Packaging reports a Python or dependency mismatch:** Check Python 3.12.14 in `backend/.venv` and `backend/build-requirements.lock`. The source requirement `>=3.12` does not allow arbitrary versions for packaging.
- **Offline Launcher build cannot find dependencies:** Run the documented `cargo fetch --locked` commands first; retain the script's offline checks.
- **An old database cannot be opened:** Legacy schema v7 is not adopted, and unknown versions prevent startup. A legacy-data migration solution requires confirmation. Only known Alembic revision upgrades are currently implemented.
- **SSH tests are skipped:** Start the SSH Lab and set `HARNESS_RUN_SSH_INTEGRATION=1`, or run the relevant repository gate.
- **Transfer state is incomplete after reloading:** Local browser file preparation is lost. Remote recovery requires an explicit user action; it does not automatically connect or replay mutations.
- **Is the Agent read-only?** No. It can execute commands that modify remote state, without a complete sandbox or per-command approval. Tool output retains only a prefix. Clipping happens after output collection and does not establish a memory bound on remote reads.

Real providers, the fully installed desktop application, production SSH, and user legacy-data migration still require separate acceptance testing. Some existing protocol documents retain historical schema descriptions; current storage code and the [Backend guide](docs/agents/python-sidecar.md) define database behavior.

## Contributing

1. Read [AGENTS.md](AGENTS.md), then the relevant domain guides and local rules.
2. Keep changes focused. Do not commit secrets, databases, logs, `.runtime/`, dependency directories, or build artifacts.
3. Add tests for behavior changes. Update implementation, tests, and the authoritative documentation together when changing protocols, storage, or cross-layer contracts.
4. In pull requests, describe the concrete problem, behavior changes, verification commands, and results. Distinguish automated tests from manual acceptance.

For documentation changes, check relative links, headings, placeholders, and whitespace differences:

```powershell
git diff --check
```

No standalone `CONTRIBUTING.md` was found. Existing AGENTS documents define contribution requirements. `docs/superpowers/` contains local specifications and plans and is excluded from Git.

## Further Reading

The following detailed guides are currently in Chinese:

- [Architecture and process ownership](docs/agents/architecture.md)
- [Protocol and security boundaries](docs/agents/protocol-security.md)
- [Frontend guide](docs/agents/frontend.md)
- [Python Backend guide](docs/agents/python-sidecar.md)
- [Launcher and Tauri guide](docs/agents/rust-core.md)
- [Testing and acceptance layers](docs/agents/testing.md)

## License

This project is licensed under the [MIT License](LICENSE).

## Contact and Issue Reporting

Submit an Issue or Pull Request to the [GitHub repository](https://github.com/Franc1s396/harness-shell) identified by the current Git remote. Include OS and toolchain versions, reproduction steps, expected and actual results, and manually sanitized logs. A maintainer email and private security-reporting channel require confirmation. Do not upload credentials or runtime databases to public issues.
