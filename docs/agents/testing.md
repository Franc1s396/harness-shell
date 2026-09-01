# Testing Guide

## 何时必须读取

出现以下任一情况时必须读取本文档：

- 新增或修改 Frontend、Rust、Python 测试；
- 修改 Sidecar 打包、Tauri build、SSH Lab 或验证脚本；
- 准备报告“通过”“完成”“已修复”或某个里程碑验收；
- 改变生成物、测试证据、桌面人工验收或生产验收边界。

涉及 SSH Lab 时还必须读取 `tests/ssh_lab/AGENTS.md`；涉及协议或 secret marker 时读取 [Protocol & Security Guide](protocol-security.md)。

## 范围与职责

本仓库将验证分为十个证据层级：

1. Focused unit/contract test：证明一个小范围行为或契约。
2. Subsystem suite：Python、Frontend 或 Rust 某一子系统回归。
3. Packaged Sidecar/Rust contract：证明实际 `.exe` 与 Rust 启动、Vault、Protocol、Supervisor 契约。
4. M1 automated gate：证明本地桌面基础设施的自动化范围。
5. M2 automated gate：证明当前 Windows checkout 加 containerized OpenSSH Lab 的自动化范围。
6. Manual SFTP automated gate：证明用户手动 SFTP 的跨层契约、packaged Sidecar 和 containerized OpenSSH 行为。
7. M3 Agent automated gate：证明 fake ChatModels、packaged Sidecar、Agent Protocol/Rust 边界和 bound-session containerized OpenSSH command 行为。
8. Explicit Provider probe：只证明一次显式选择的 API type/model/provider 路径可用；不属于默认自动门禁。
9. Tauri desktop manual acceptance：证明真实窗口、输入、焦点、Runtime 投影和进程清理等观察行为。
10. Production host/deployment/migration acceptance：必须在目标环境单独执行，不由前九层自动推导。

结论只能覆盖实际执行且保存证据的层级。

## 当前源码真源

- Frontend 测试与脚本：[frontend/package.json](../../frontend/package.json)、[frontend/src/](../../frontend/src/)
- Rust contract tests：[frontend/src-tauri/tests/](../../frontend/src-tauri/tests/)
- Python 测试配置：[backend/pyproject.toml](../../backend/pyproject.toml)
- Python tests：[backend/tests/](../../backend/tests/)
- 日志 focused tests：[backend/tests/test_main.py](../../backend/tests/test_main.py)、[backend/tests/runtime/test_logging.py](../../backend/tests/runtime/test_logging.py)、[backend/tests/runtime/test_service_dispatch.py](../../backend/tests/runtime/test_service_dispatch.py)、[backend/tests/agent/](../../backend/tests/agent/)
- Sidecar build：[backend/scripts/build_sidecar.ps1](../../backend/scripts/build_sidecar.ps1)
- M1 gate：[scripts/verify-m1.ps1](../../scripts/verify-m1.ps1)
- M2 gate：[scripts/verify-m2.ps1](../../scripts/verify-m2.ps1)
- Manual SFTP gate：[scripts/verify-manual-sftp.ps1](../../scripts/verify-manual-sftp.ps1)
- M3 Agent gate：[scripts/verify-m3-agent.ps1](../../scripts/verify-m3-agent.ps1)
- Explicit Provider probe：[backend/scripts/probe_agent_provider.py](../../backend/scripts/probe_agent_provider.py)
- SSH Lab：[tests/ssh_lab/](../../tests/ssh_lab/)
- M1 acceptance：[docs/testing/m1-acceptance.md](../testing/m1-acceptance.md)
- M2 acceptance：[docs/testing/m2-acceptance.md](../testing/m2-acceptance.md)
- M3 Agent acceptance：[docs/testing/m3-agent-acceptance.md](../testing/m3-agent-acceptance.md)
- M3 Agent frontend acceptance：[docs/testing/m3-agent-frontend-acceptance.md](../testing/m3-agent-frontend-acceptance.md)
- 生成物排除：[.gitignore](../../.gitignore)

## 项目结构规范

- Frontend test 与被测模块相邻，使用 `*.test.ts` / `*.test.tsx`。
- Python test 按领域位于 `backend/tests/<domain>/`；真实 OpenSSH 流程集中在 `ssh_integration/`。
- Rust 跨模块契约位于 `frontend/src-tauri/tests/`，文件名清楚标识被验证边界。
- 用户手动 SFTP coordinator 契约位于 `frontend/src-tauri/tests/manual_sftp_coordinator_contract.rs`；覆盖 actor/lifecycle/gate、mutation/recovery、真实 Broker frame 的多 chunk upload/download、typed progress、取消边界、future-drop detached cleanup 必须阻塞 shutdown drain，以及 journal/local-commit fail-closed matrix。
- SSH Lab 的 Dockerfile、Compose、入口、配置和检查脚本位于 `tests/ssh_lab/`；运行凭据与证据只在 `.runtime/`。
- 仓库级门禁脚本位于 `scripts/`，不得在文档中复制实现逻辑形成第二真源。
- 人工验收记录位于 `docs/testing/`，必须记录环境、时间、构建、观察证据、未执行项和边界。

## 代码规范

- 测试名描述条件与结果，不写 `test_happy_path` 等失去业务含义的名称。
- 回归测试先证明原问题会失败，再验证修复后的最小正确行为。
- 失败路径断言 error code、状态和副作用不存在；不得只断言“抛了异常”。
- 时间、并发和生命周期测试使用可控 clock/event/channel，避免依赖长时间 `sleep` 和竞态碰运气。
- Secret test 使用唯一 marker 并扫描所有规定介质；不得在测试输出中打印实际 credential。
- Fake/Stub 只模拟明确契约，不因实现方便绕过真实边界；测试辅助 Python 遵循 [Python Style Guide](python-style.md)。
- 验证脚本遇到缺失依赖、端口、证据或阶段失败时直接停止，不跳过后继续报告总门禁成功。

## 长期约束

- `scripts/verify-m1.ps1` 当前执行工具链检查、Python tests、Sidecar package、Rust tests、Web build 和 `tauri info`。
- `scripts/verify-m2.ps1` 先运行 M1，再运行 SSH Lab topology/keygen/startup-readiness/shell-line-ending contracts、Python unit/contract、真实 SSH integration、cleanup 和 runtime evidence 检查。Startup readiness 对 Compose 成功但 `ps -q` 暂时无输出的状态使用同一个 90 秒 deadline 条件等待；Compose 查询失败或 deadline 耗尽仍立即显式失败。Linux 容器入口脚本由根 `.gitattributes` 固定为 LF，并由字节级契约测试阻止 CRLF shebang。M2 仍要求历史 `artifact_metadata` 与通用 `encrypted_records` schema 存在，但已删除的 Agent/Artifact runtime 不再产生业务行，因此 M2 行证据只要求 audit、trace 与 Vault；人工 SFTP 门禁另行要求 encrypted operation 行证据。
- `scripts/verify-manual-sftp.ps1` 先回归 M2，再运行 focused manual-SFTP Python、packaged Sidecar/Rust all-target、Frontend test/build、Direct/ProxyJump OpenSSH manual-SFTP 与 PTY isolation，并扫描 container log、typed event、SQLite/evidence 中的 credential/local-path/file-content marker；任何阶段失败都不得打印总成功标志。
- `scripts/verify-m3-agent.ps1` 先回归完整 Manual SFTP gate，再运行 focused Agent/runtime/schema Python tests、packaged Sidecar/Rust all-target、Sidecar build 和 Direct/ProxyJump bound-session Agent command integration；它使用 fake ChatModels，不联系 Provider，并在成功或失败时清理自己启动的 SSH Lab。
- 旧 Agent exec/SFTP/Artifact 单元与 SSH integration 测试已经随运行时删除；用户手动 SFTP 由独立 manual-SFTP gate 和桌面清单验收，不构成 Agent SFTP 证据。
- 私有 manual SFTP runtime/coordinator 的 Rust/Python contract、command/capability contract、typed frontend/controller 与 workspace 交互测试已覆盖本地实现、wire 行为、51-command 注册、main-only permission、进入 Activity 时固定 Session binding、Rust canonical snapshot/TxF、本地 download-part recovery、disconnect/exit lifecycle、单项 action matrix、lazy tree、非持久化状态、确认/键盘/焦点和 960×640 响应式边界。实现和自动门禁完成仍不等于 Tauri Desktop 或真实 host 验收。
- Agent tests 必须覆盖两个显式 API type、5 次 timeout retry、128 次 Tool 循环、多 Tool Call 拒绝、五轮 context、中断闭合、canonical SystemMessage 的只读/secret/状态变更确认边界与高危操作双确认、无 checkpointer、认证加密原始 message/output、Vault secret non-exposure、六个 Protocol method、七个 main-window Tauri commands、1 MiB response 拒绝以及 Direct/ProxyJump Session 绑定。
- 日志回归必须覆盖普通 message、任意结构化字段、完整 exception traceback、HTTP response body、成功/失败 Run、七个 graph node、route、Provider failure、malformed/foreign stderr 与超过旧 16 KiB 阈值的长行原样转发；另用 marker 验证业务调用点没有主动记录 runtime/API key。packaged Sidecar/Rust all-target 证据必须使用本次构建的 `HARNESS_SIDECAR_EXE`。
- Runtime schema 回归必须覆盖 v2→v3→v4 migration、schema v4 Agent 表的 STRICT、列类型/nullability、外键、唯一索引与关键 CHECK 自检，以及连接 profile 在固定时钟下连续更新仍逐次 `+1`、`2^53-1` 耗尽不写入、目标/ProxyJump 陈旧版本在网络 I/O 前失败和 Rust 只发送数字 `profile_version` 的跨语言契约。
- M2 成功标志必须是脚本最终明确输出：`M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.`
- Manual SFTP gate 的唯一总成功标志必须在 cleanup 与 evidence scan 完成后输出：`Manual SFTP automated gate passed: local Windows checkout plus containerized OpenSSH lab only.`
- M3 Agent gate 的唯一总成功标志必须在自己的 SSH Lab cleanup 后输出：`M3 Agent automated gate passed: local Windows checkout, fake ChatModels, packaged Sidecar, and containerized OpenSSH lab only.`
- Provider probe 必须要求 `HARNESS_RUN_AGENT_PROVIDER_PROBE=1` 及完整 Provider 环境变量；`CHAT_COMPLETIONS` 与 `RESPONSES` 必须分别显式运行，禁止自动切换。输出只允许 API type、model、status 和 latency。
- SSH Lab 的 `jump` 同时连接 `ssh_ingress` 与内部 `ssh_lab`，只将 `127.0.0.1:2222` 暴露到 host；`target` 仅连接 internal `ssh_lab`。
- Manual SFTP 容器 integration 直接经过 Python runtime，不证明凭据经过桌面 Core/Vault。M3 Agent 容器 integration 证明 bound-session command executor 的 Direct/ProxyJump 目标用户、stdout/stderr、timeout、cancel 与 channel cleanup 行为，但 fake ChatModel 不证明 Provider；两者都不证明 Agent UI、审批、sudo、生产主机或部署。
- 自动测试不能替代 Tauri Desktop 的真实焦点、窗口、xterm 输入、Runtime 刷新和进程清理验收。
- Tauri Desktop 日志验收单独核对：启动终端 INFO 记录、固定 `LogDir` active file、Settings Diagnostics 路径、Explorer 打开同一路径、fake/local Agent 的 Run/node/route/terminal 事件、异常 traceback/HTTP body、重启 append 行为，以及测试 seam 下目录打开失败显示结构化错误；不得为人工验收强制生成 10 MiB 日志。
- 当前 M2 手工记录完成只证明该 checkout 与选定 local/container hosts，仍不是 production-host acceptance。

生成物和本地状态不得提交：

- Python `.venv/`、`__pycache__/`、`.pytest_cache/`、coverage、`backend/build/`、`backend/dist/`
- Frontend `node_modules/`、`dist/`、TypeScript build info
- Rust `target/`
- `frontend/src-tauri/binaries/*.exe` 与 Tauri generated schemas
- `tests/ssh_lab/.runtime/`
- `.env*`（保留显式允许的 `.env.example`）、日志和临时文件

## 项目命令

Python subsystem，从仓库根运行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest
```

Frontend subsystem：

```powershell
cd ..\frontend
npm run test
npm run build
```

Rust 与仓库门禁，从 `frontend/` 返回仓库根：

```powershell
cd ..
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

M2、Manual SFTP 和 M3 Agent gate 会创建并清理容器、生成临时 credential/evidence，要求 Docker Desktop、Docker Compose v2、Python 3.12.13、Node/npm、Rust MSVC 工具链、WebView2 和 Windows OpenSSH `ssh-keygen.exe`。M3 默认不需要 Provider credential。

## 验证要求

- 选择最小足够测试开始，再依据变更风险逐层扩大；不得用无关大门禁掩盖缺失的 focused regression。
- 报告每条实际运行的命令、退出码和关键计数；未运行的阶段明确标为未验证。
- Sidecar package 只有 build script 和 smoke test 真正成功后才能报告已打包。
- `verify-m1.ps1` 通过只说明其六阶段自动范围；人工桌面安全检查未完成时不得宣称 M1 桌面验收完成。
- `verify-m2.ps1` 通过只说明 local Windows checkout + containerized OpenSSH Lab；人工 Desktop matrix 另行记录。
- `verify-m3-agent.ps1` 通过只说明其 fake ChatModel、packaged Sidecar、Rust contracts 与 containerized OpenSSH 范围；Provider、Agent UI、Tauri Desktop 和生产 SSH 主机必须分别验收。
- `m3-agent-frontend-acceptance.md` 单独记录 React Agent 的 Frontend suite/build、复跑 M3 gate、局部 Tauri Desktop 观察以及明确未执行的真实 Provider/SSH 项；自动测试和局部桌面观察不得合并成完整 Desktop matrix。
- UI 行为由用户截图、复现和“验证通过”可作为对应桌面范围证据，但必须保存具体观察与未覆盖项。
- 完成前运行 `git diff --check`，审查 `git status --short`，确认未误改或纳入生成物；未获授权不执行 Git 写操作。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- 新增、删除或重命名 test suite、acceptance checklist 或验证脚本；
- M1/M2 阶段、成功标志、依赖、命令或证据范围改变；
- SSH Lab 拓扑、端口、credential/evidence 位置或 cleanup 改变；
- Sidecar/Frontend/Rust 构建产物或 `.gitignore` 边界改变；
- 引入 CI、lint、formatter、type checker、生产验收或迁移门禁。

单个测试用例内部重构未改变测试入口和证据边界时无需更新，但必须执行 AGENTS 影响检查。
