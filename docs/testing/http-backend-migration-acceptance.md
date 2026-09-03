# Launcher + React Direct Backend Migration Acceptance

## 当前证据状态

本文件记录 2026-09-02 架构切换及 2026-09-03 Python 清理后的验收边界。源码实现、focused suites、仓库门禁、NSIS build 和 disposable-user Desktop 人工观察必须分别填写；一层通过不能推导其他层通过。

| 证据层 | 状态 | 证明范围 |
| --- | --- | --- |
| Python focused/full suite | PASS | 392 passed、13 skipped；覆盖 schema v6、credentials、47 个 HTTP operations、WebSocket、SSH/PTY、remote SFTP、Agent 本地源码行为 |
| Frontend test/build | PASS | 61 files、330 tests passed；TypeScript/Vite production build passed，存在 >500 kB chunk warning |
| Tauri shell tests | PASS | 覆盖 production bootstrap 与单窗口最小 capability |
| Launcher tests | PASS | 9 tests passed；覆盖 ready/control pipe、Job、child ordering、bounded cleanup 的本地自动化行为 |
| Installer static check | PASS | bundle inputs 与 Start Menu/Desktop/finish action 均只指向 Launcher |
| M1/M2/Manual SFTP/M3 gates | NOT RUN | schema v6 清理后未重跑完整 gate；2026-09-02 旧 schema 证据不能替代本轮验收 |
| NSIS build | NOT RUN | schema v6 与打包依赖清理后未重新生成安装包 |
| Disposable-user Desktop matrix | NOT RUN | 安装、可见入口、真实进程顺序、picker、退出清理 |
| Real Provider / production SSH / deployment / old-data migration | NOT RUN | 必须独立验收；当前旧库策略为明确拒绝而非迁移 |

Python 与 Frontend 数字来自 2026-09-03 本轮实际输出；Tauri、Launcher 与 Installer static check 仍是 2026-09-02 证据。`jsdom` 的 xterm canvas 路径打印已知 `getContext` stderr，但测试通过；Vite 仅报告 chunk size warning。

## 必须满足的架构事实

- NSIS Start Menu、Desktop shortcut、finish action 和 silent `/R` 只启动 `harness-shell-launcher.exe`。
- Launcher 创建 Job，先启动 Backend `desktop --port 0`，从 inherited ready pipe 获取端口，再启动 `harness-shell-ui.exe --backend-url ...`；不得扫描端口。
- Tauri 只暴露 `get_backend_bootstrap`；不存在独立 approval window 或 approval capability。
- React 通过 typed HTTP 与 Runtime WebSocket 直连 Python。
- Python ASGI lifespan 自主初始化资源，只接受全新 plaintext schema v6；没有旧库 migration 或存储加密保证，也不保存无读取闭环的 SQLite Audit/Trace。
- React 独占 Manual SFTP 本地 picker、handle、hash 与 262,144-byte chunk loop；Python 独占 remote temp、commit、abort 和 recovery。
- Provider key 由 Python `CredentialRepository` 按 kind 解析，不经 Rust 业务 command。

## Disposable-user Desktop matrix

在一次性 Windows 用户配置文件中安装本轮 NSIS 后逐项记录：

- [ ] Start Menu/Apps 中只有一个可见 Harness Shell 入口，目标为 Launcher。
- [ ] 进程顺序为 Launcher → Backend ready → UI；Backend port 来自 ready evidence。
- [ ] direct HTTP 与 Runtime WebSocket 可用，Backend 提前退出不会 respawn。
- [ ] Upload 使用浏览器 file picker；Download 在网络读取前同步取得 save handle。
- [ ] upload/download 只使用 strict raw chunks；页面 reload 后 local preparation 消失。
- [ ] UI 正常关闭触发 Backend graceful exit；故意挂起时有界超时后 Job cleanup。
- [ ] 最终无 Launcher、UI、Backend 或 SSH 残留进程。

未执行上述矩阵时，最终报告必须明确写“未完成安装版 Desktop 人工验收”。
