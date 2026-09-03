# M3 Experimental ReAct Shell Agent Acceptance（历史快照）

> 下文是切换到 Python CredentialRepository 与 schema v6 之前的证据，仅用于历史追踪，不能代表当前架构通过。当前 M3 门禁范围以 `docs/agents/testing.md` 为准。

本记录区分自动门禁、真实 Provider、Tauri Desktop 和生产环境证据。任何一层 PASS 都不能推导其他层已经验收。

## 2026-08-31 automated gate

- Tester: Codex local execution（用户已授权实施与验证）
- Checkout: `E:\codeSoftware\code\harness-shell`，保留当前未提交工作区
- Command: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-m3-agent.ps1`
- Result: PASS，exit code `0`
- Final marker: `M3 Agent automated gate passed: local Windows checkout, fake ChatModels, packaged Sidecar, and containerized OpenSSH lab only.`
- Focused Agent/runtime/schema tests: PASS，`111 passed`
- Rust all-target contracts: PASS；包含 Agent command/Protocol、49-command capability、Vault API Key kind、schema v4 ready 和 packaged Sidecar contracts
- Packaged Sidecar: PASS；Python `3.12.13` 与锁定依赖完成 PyInstaller build，并复制到 Tauri sidecar binary 位置
- Bound-session OpenSSH Agent integration: PASS，Direct/ProxyJump 目标用户、stdout/stderr、timeout、cancel 与 channel cleanup 共 `4 passed`
- Regression boundary: nested Manual SFTP gate PASS；Frontend `44` files / `228` tests PASS，production build PASS，Manual SFTP OpenSSH integration `4 passed`
- Cleanup: PASS；本轮 Agent SSH Lab 的 containers 与 networks 在最终成功标记前已移除

自动门禁使用 fake ChatModels。它证明本 checkout 的 schema v4、实验性 Agent backend、加密持久化契约、Protocol/Rust/Vault 边界、packaged Sidecar 和 containerized OpenSSH bound-session command 路径；不证明真实 Provider、Agent UI、Tauri Desktop 或生产 SSH 主机。

## Explicit Provider probes

| API type | Provider | Model | Time | Result |
| --- | --- | --- | --- | --- |
| `CHAT_COMPLETIONS` | not provided | not provided | not run | NOT RUN |
| `RESPONSES` | not provided | not provided | not run | NOT RUN |

未提供测试 credential，因此没有联系任何 Provider。`backend/scripts/probe_agent_provider.py` 的默认禁用保护已验证：未设置 `HARNESS_RUN_AGENT_PROVIDER_PROBE=1` 时以明确错误退出且不发起网络请求。未来执行时只能记录 API type、model、status 和 latency，不得记录 API Key、response content、header 或带 query 的 base URL。

## Unverified layers

- Tauri Desktop Agent UI: NOT RUN；当前 React Agent Workspace 仍为 placeholder，不能宣称用户可以从 UI 运行 Agent。
- Real Provider behavior: NOT RUN；两种 API type 均未使用真实 credential 验证。
- Production SSH hosts: NOT RUN；容器 OpenSSH Lab 不是生产主机。
- Approval/sudo/automatic recovery: NOT IMPLEMENTED OR NOT ACCEPTED；不得从本次自动门禁推导。
- Deployment/migration acceptance: NOT RUN；schema migration 仅由本地自动测试和 packaged Sidecar initialize contract 覆盖。

## Result

- M3 experimental Agent automated gate: PASS for the exact local/fake/packaged/container scope above.
- Real Provider acceptance: NOT RUN.
- Tauri Desktop Agent acceptance: NOT RUN.
- Production/deployment/migration acceptance: NOT RUN.
