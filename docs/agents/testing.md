# Testing Guide

## 证据分层

验证结论必须按层陈述：focused test、subsystem suite、packaged executable、Frontend build、Rust contract、containerized OpenSSH Lab、NSIS build、Desktop 人工验收、真实 Provider、生产 SSH/部署。低层通过不能替代高层验收。

## 真源与命令

- Python tests：`backend/tests/`
- Frontend tests：与 `frontend/src/` 模块相邻的 `*.test.ts(x)`
- Tauri shell tests：`frontend/src-tauri/tests/`
- Launcher tests：`launcher/tests/`
- SSH Lab：`tests/ssh_lab/` 与 `backend/tests/ssh_integration/`
- repository gates：`scripts/verify-*.ps1`

最小完整回归：

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run build
cargo test --manifest-path frontend\src-tauri\Cargo.toml --all-targets --offline
cargo test --manifest-path launcher\Cargo.toml --all-targets --offline
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-installer-entry.ps1
```

仓库门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-manual-sftp.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m3-agent.ps1
```

## 门禁边界

- M1：本地 Windows Python/Frontend/Rust tests、packaged Backend 显式 `serve` loopback smoke、最小 Tauri capabilities 与 Frontend build。
- M2：M1 加 OpenSSH Lab 脚本/真实 SSH integration、plaintext Alembic baseline evidence 与生成物跟踪检查。
- Manual SFTP：M2 加浏览器本地文件/hash/256 KiB raw-chunk contract、Python remote recovery 和真实 OpenSSH SFTP/PTY isolation。
- M3 Agent：Manual SFTP gate 加 Python `CredentialRepository` ownership、fake SDK streams 与 bound-session OpenSSH command。
- `verify-installer-entry.ps1`：只静态证明 NSIS input/shortcut/finish target；不证明安装或进程行为。

Python-only 与 SSH Lab 使用显式 `serve --port <fixed> --data-dir <isolated absolute>`。安装版 Desktop 只能从 Launcher 开始，并从 ready pipe 获得端口；测试和脚本不得扫描端口或直接把 UI/Backend 当用户入口。

## 必测契约

- Alembic 新建与同库重启、整批 DDL/data/revision 回滚、STRICT/外键/索引自检、旧 schema 写入前拒绝、短 Session 与 plaintext record；不得重新出现无读取闭环的 Audit/Trace/Artifact 表。
- credential request envelope、Python repository kind match、Provider key lookup、secret non-logging。
- direct HTTP Problem、request ID、size/media/header/unknown-field failure；HTTP access log 覆盖 route template、实际返回 status、duration、INFO/WARNING/ERROR 分级、raw path 不泄露，以及 `GET /v1/runtime/state` 不打印 access log。
- Agent SSE 必测 strict LF/CRLF framing、UTF-8 chunk boundary、frame/body/terminal reserve、started-first HTTP 200 barrier、durable terminal ordering、capacity 64 背压、terminal 发送前 request ID/capacity ownership、disconnect/shutdown cancellation、secret/tool/command/output non-exposure 与 OpenAPI/fixture drift。
- React Agent 必测 event sequence/correlation/EOF、thinking→provisional text、同 tab 增量滚动、跨 tab isolation，以及 failed/invalid/too-large/interrupted 时丢弃 partial assistant text。
- Runtime WebSocket single owner、ping/pong causation、queue、close、PTY input/event。
- Manual SFTP React picker/handle/hash/chunk loop和 Python remote temp/commit/abort/recovery。
- Launcher ready/control/stderr pipe、Backend 独立日志落盘与 10 MiB/4 归档轮转、handle inheritance、Job cleanup、UI-first/Backend-first exit、无 respawn。
- Tauri production bootstrap 只有 `get_backend_bootstrap`；main capability 只含 bootstrap 与固定 close/destroy 权限。

## 离线 tokenizer 与打包

新增源码环境在启动或测试前显式准备资源（仅此构建步骤允许获取编码数据）：

```powershell
backend\.venv\Scripts\python.exe backend/scripts/prepare_tokenizer.py --output-dir backend/build/tokenizer
backend\.venv\Scripts\python.exe backend/scripts/prepare_tokenizer.py --output-dir backend/build/tokenizer --check
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/build_sidecar.ps1
```

`build-requirements.lock` 固定 `tiktoken==0.12.0` 及依赖，准备脚本生成 `o200k_base` ranks/metadata/固定样本，PyInstaller 纳入编码资源和原生扩展。build 脚本严格串行执行 lock、准备、check、PyInstaller 和实际 exe smoke。Runtime 初始化构造并测试 encoding 后才发布 READY；smoke 子进程使用临时空缓存、关闭端口代理及 loopback NO_PROXY。这证明当前产物不借用用户缓存或外网，不能替代真实断网机器和安装版验收。生成资源留在忽略的 build 目录，不提交。

上下文回归覆盖：Provider round-trip 和表单预算、工具首部及 DB/model 一致、usage/revision、完整轮边界、摘要取消/3 次尝试/输入与候选超预算、UI 文本隔离、旧库拒绝和 tokenizer 缺失/损坏。`verify-m3-agent.ps1` 包含全部 Agent/storage 测试和 Manual SFTP/M2 前置链。

## Desktop 与安装验收

`scripts/build-desktop.ps1` 生成 NSIS 后，必须在 disposable Windows user profile 人工核对：只有一个 Harness Shell 用户入口、Launcher→Backend-ready→UI 顺序、direct HTTP/WebSocket、upload picker、同步 save picker、strict chunks、reload 丢失 local preparation、UI close graceful exit、forced cleanup 和无残留进程。

没有执行这组观察时，只能报告构建或静态检查，不能报告 Desktop/install acceptance。fake Provider 不是真实 Provider；containerized OpenSSH 不是生产 SSH。

## 生成物

不得提交 `.venv/`、`node_modules/`、`target/`、`dist/`、`build/`、`.runtime/`、SQLite、private key、Sidecar/Launcher companion `.exe`、Tauri bundle 或 generated schema。任务结束运行 `git diff --check` 并检查相关 AGENTS 文档影响。


## 数据库迁移验证

`backend/tests/storage/` 验证真实 SQLite 文件的整批 migration/DDL/batch 回滚、版本身份、STRICT 约束、Session 生命周期与包内资源；`backend/tests/agent/test_session_boundaries.py` 验证并行会话的模型/工具边界无活动 Session。测试使用临时数据库，不运行用户旧库迁移。

`backend/scripts/build_sidecar.ps1` 收集 Alembic env.py、versions 和 Mako 模板，随后执行实际 exe smoke：无关 cwd 的全新建库、同库重启、旧 v7/未知 revision 的无 ready、非零退出和内容不变。Desktop 控制读取在线程启动前完成数据库初始化，避免初始化失败时阻塞关闭；失败不能返回成功退出码。测试与打包 smoke 不代表完整安装版 Desktop 或用户旧数据验收。
