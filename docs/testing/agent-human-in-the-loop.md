# Agent Human-in-the-Loop 验证记录

日期：2026-09-08。当前工作区实施，无 commit/push、分支或 worktree 操作。保留原有 `frontend/src-tauri/Cargo.toml` 工作。

## 实现范围

明确只读命令自动执行；其余命令按每次调用审核，原危险规则继续硬阻断。对话内气泡显示冻结 SSH 目标和原命令，拒绝/通过按钮不会自动触发；无审核超时。LangGraph interrupt + 每 Run InMemorySaver 由原 SSE worker 恢复，拒绝 ToolMessage 交回模型。审核 HTTP 只提交决定，不产生第二个 Run 或执行 worker。首次风险弹窗和黄色实验警告框已删除，系统提示词已调整。

## 自动证据

| 检查 | 结果与范围 |
| --- | --- |
| `python -m pytest backend/tests/agent backend/tests/runtime backend/tests/web backend/tests/ssh backend/tests/connections -q --tb=short --show-capture=no` | 565 passed，退出 0；随后新增测试另行验证 |
| `npm.cmd --prefix frontend run test` | 最终全量 434 passed，退出 0；之后新增 INVALIDATED 共享 fixture，API 50 tests 通过 |
| `npm.cmd --prefix frontend run build` | TypeScript 与 Vite 构建退出 0；存在超过 500 kB 的 chunk 提示 |
| `python backend/scripts/export_http_contract.py --write` / `--check` | 退出 0；48 个真实 HTTP operations，增加决定请求和两个 SSE event |
| 真实 ASGI 原 SSE + 独立决定 HTTP | 通过/拒绝两种序列通过，原 Run/sequence 保持一致；HTTP 成功只表示已记录 |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-m3-agent.ps1` | 第二次完整退出 0；含 M1/M2/Manual SFTP、Backend 打包冒烟、Tauri、fake Provider 与 OpenSSH Lab |
| OpenSSH `test_agent_command.py` | 6 passed；包括两个 HITL marker case：等待/拒绝时 marker 不存在，通过后内容恰为一个 x；容器和网络已清理 |
| 审核/资源/取消补充用例 | 覆盖一次消费、幂等决定、错身份、多调用、后续重新审核、拒绝结果进入下一模型请求、取消、目标/跳板断连、局部 waiter 取消不关闭共享 SSH、SSE 发布失败、清理异常保留首个取消、普通 16 槽满载仍可使用 1 个控制槽 |
| UI 补充用例 | 气泡纯文本，无图片执行；提交禁用、审批消息保留、无 spinner、旧未知审批不影响新 Run、原按钮取消、重复点击只发一次、SSE 先于 HTTP、矛盾结果显式报错、未知字段/错关联/非法顺序拒绝 |

第一次 M3 停在 npm ci，日志为 registry 下载 EACCES 并最终 `Exit handler never called`。已在现有 package-lock 下重新安装成功，未升级依赖或改变锁文件；第二次全门禁通过。门禁后增加了少量时序与清理防护，最终代码以完整 Python/Frontend 回归和新测试验证；先前打包产物不代表最后改动的安装验收。

## Checkpoint 资源观察

使用真实产品图与固定 fake SDK 运行 `test_129th_tool_call_is_paired_but_never_executed`。只测量测试进程，不接触实际 Provider/SSH。测量点在 `InMemorySaver.delete_thread()` 前后。

- 默认短输出、128 次执行：776 个 checkpoint；storage/writes/blobs 中序列化 bytes 合计 83,091,805（约 79.2 MiB）。删除后合计 0。
- 较大输出：每次 stdout 为 `log ` 重复 700 次（2,800 chars），128 次工具执行，context_window_size=128000、compaction ratio=0.90，触发第 129 次的业务上限；测试通过。序列化 bytes 为 268,923,004（约 256.5 MiB），删除后 0。
- 相同较大输出负载用 Windows `GetProcessMemoryInfo` 读取本测试进程：PeakWorkingSetSize=533,164,032 bytes（约 508.5 MiB），删除前 WorkingSetSize=533,159,936，删除后=247,783,424（约 236.3 MiB）；saver 的三个容器均为空。进程数值包含 Python、测试工具、tokenizer、日志捕获和其他对象，不能当成 checkpoint 单独占用。

结论：快照不会跨 Run 累积，但单个长轮次的内存明显随历史增长；本次没有修改历史保留规则或宣称常量内存。该测量不覆盖所有最大长度输出和 16 个并发 Run 的峰值。首个临时测量配置 ratio=0.95 被现有预算交叉校验正确拒绝，之后改用合法 0.90；没有修改预算校验。

## 尚未执行的人工/外部验收

以下均为 **NOT RUN**，不阻止代码和自动测试交付，也不代表通过：

- 安装版 WebView：通过/拒绝、后台 tab、取消、断连、长命令内部滚动与键盘焦点、关闭窗口、无首次警告/黄色框、不限时等待。
- 真实 Provider 四情境：明确执行、范围不明、只分析、拒绝后替代。fake SDK 不能证明真实模型遵守提示词。
- 生产 SSH、sudo、部署与用户真实旧数据迁移。

## 文档同步

已更新根 `AGENTS.md`、Backend `AGENTS.md`、SSH Lab `AGENTS.md`、`docs/agents/{architecture,frontend,python-sidecar,protocol-security,testing}.md` 和 HTTP 契约说明。规格/计划记录实现落点与验证边界。最终 `git diff --check`、导出器 `--check` 均退出 0。

最终 `backend\.venv\Scripts\python.exe -m pytest backend -q --tb=short --show-capture=no`：721 passed、15 skipped、退出 0。15 个 SSH integration 在普通测试模式跳过，其中 Agent 的 6 项已由完整 M3 独立启用并通过。追加的路由/fixture 聚焦测试 20 passed；最终前端构建退出 0。
